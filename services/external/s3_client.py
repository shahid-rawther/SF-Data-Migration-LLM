"""S3 access for migration checkpoints.

Only durable state crosses this boundary: the record of what a migration has
already done. The staged source extract stays on local disk, because it is
large, reproducible from the source, and only needed by the machine actually
running the transform.

Every method raises rather than returning a falsy value on failure, so a
checkpoint that did not reach the bucket is never mistaken for one that did.
Callers that must not fail a migration over a storage problem catch
:class:`S3Error` themselves.
"""
from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass
from typing import Any, Iterable

from common.logger import logger

DEFAULT_PREFIX = "itsm-migration"
DEFAULT_REGION = "us-east-1"


class S3Error(RuntimeError):
    """An S3 operation failed."""


class S3Unavailable(S3Error):
    """boto3 is missing, or no bucket is configured."""


def _text(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _flag(name: str, default: bool) -> bool:
    raw = _text(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class S3Settings:
    bucket: str
    prefix: str
    region: str
    # Server-side encryption. Empty uses the bucket default (SSE-S3); set a key
    # id or ARN to encrypt with SSE-KMS instead.
    kms_key_id: str
    enabled: bool

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.bucket)

    def redacted(self) -> dict:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "bucket": self.bucket,
            "prefix": self.prefix,
            "region": self.region,
            "encryption": "aws:kms" if self.kms_key_id else "AES256 (bucket default)",
        }


def load_settings() -> S3Settings:
    return S3Settings(
        bucket=_text("MIGRATION_S3_BUCKET"),
        prefix=_text("MIGRATION_S3_PREFIX", DEFAULT_PREFIX).strip("/"),
        region=_text("MIGRATION_S3_REGION")
        or _text("AWS_REGION")
        or _text("BEDROCK_REGION", DEFAULT_REGION),
        kms_key_id=_text("MIGRATION_S3_KMS_KEY_ID"),
        enabled=_flag("MIGRATION_S3_ENABLED", True),
    )


class S3Client:
    """Thin wrapper over the S3 operations the checkpoint store needs."""

    def __init__(self, settings: S3Settings | None = None) -> None:
        self.settings = settings or load_settings()
        if not self.settings.configured:
            raise S3Unavailable(
                "No checkpoint bucket configured. Set MIGRATION_S3_BUCKET (and "
                "leave MIGRATION_S3_ENABLED unset or true)."
            )
        try:
            import boto3
        except ImportError as exc:
            raise S3Unavailable(
                "boto3 is not installed; run `pip install boto3`"
            ) from exc
        self._s3 = boto3.client("s3", region_name=self.settings.region)

    # ── keys ───────────────────────────────────────────────────────────────

    def key(self, *parts: str) -> str:
        """Join path parts under the configured prefix."""
        pieces = [self.settings.prefix, *[str(p).strip("/") for p in parts if p]]
        return "/".join(p for p in pieces if p)

    # ── writes ─────────────────────────────────────────────────────────────

    def _encryption_args(self) -> dict:
        if self.settings.kms_key_id:
            return {
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": self.settings.kms_key_id,
            }
        return {"ServerSideEncryption": "AES256"}

    def put_bytes(self, key: str, body: bytes, content_type: str) -> dict:
        try:
            response = self._s3.put_object(
                Bucket=self.settings.bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                **self._encryption_args(),
            )
        except Exception as exc:  # noqa: BLE001 — botocore raises many shapes
            raise S3Error(f"Could not write s3://{self.settings.bucket}/{key}: {exc}") from exc
        logger.info(
            "[s3] wrote s3://%s/%s (%d bytes)", self.settings.bucket, key, len(body)
        )
        return {
            "bucket": self.settings.bucket,
            "key": key,
            "bytes": len(body),
            "version_id": response.get("VersionId", "") or "",
            "etag": (response.get("ETag") or "").strip('"'),
        }

    def put_json(self, key: str, payload: Any) -> dict:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        return self.put_bytes(key, body, "application/json")

    def put_jsonl_gzip(self, key: str, rows: Iterable[dict]) -> dict:
        """Write rows as gzipped JSON Lines.

        Used for the id map, which is the one part of a checkpoint that grows
        with the number of records migrated rather than the number of objects.
        """
        lines = "\n".join(json.dumps(row, default=str) for row in rows)
        body = gzip.compress(lines.encode("utf-8"))
        return self.put_bytes(key, body, "application/gzip")

    # ── reads ──────────────────────────────────────────────────────────────

    def get_bytes(self, key: str) -> bytes | None:
        """Return the object body, or None when the key does not exist."""
        try:
            response = self._s3.get_object(Bucket=self.settings.bucket, Key=key)
            return response["Body"].read()
        except Exception as exc:  # noqa: BLE001
            if self._is_missing(exc):
                return None
            raise S3Error(f"Could not read s3://{self.settings.bucket}/{key}: {exc}") from exc

    def get_json(self, key: str) -> Any | None:
        body = self.get_bytes(key)
        if body is None:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise S3Error(f"s3://{self.settings.bucket}/{key} is not valid JSON: {exc}") from exc

    def get_jsonl_gzip(self, key: str) -> list[dict]:
        body = self.get_bytes(key)
        if body is None:
            return []
        try:
            text = gzip.decompress(body).decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise S3Error(f"s3://{self.settings.bucket}/{key} is not gzipped JSONL: {exc}") from exc
        rows: list[dict] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("[s3] skipping malformed line in %s", key)
        return rows

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.settings.bucket, Key=key)
            return True
        except Exception as exc:  # noqa: BLE001
            if self._is_missing(exc):
                return False
            raise S3Error(f"Could not stat s3://{self.settings.bucket}/{key}: {exc}") from exc

    def list_keys(self, prefix: str) -> list[dict]:
        """List objects under a prefix as ``[{key, size, last_modified}]``."""
        entries: list[dict] = []
        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.settings.bucket, Prefix=prefix):
                for item in page.get("Contents") or []:
                    entries.append(
                        {
                            "key": item.get("Key", ""),
                            "size": int(item.get("Size") or 0),
                            "last_modified": (
                                item["LastModified"].isoformat()
                                if item.get("LastModified")
                                else ""
                            ),
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            raise S3Error(
                f"Could not list s3://{self.settings.bucket}/{prefix}: {exc}"
            ) from exc
        return entries

    def list_common_prefixes(self, prefix: str) -> list[str]:
        """List the immediate 'directories' under a prefix."""
        found: list[str] = []
        base = prefix if prefix.endswith("/") else f"{prefix}/"
        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=self.settings.bucket, Prefix=base, Delimiter="/"
            ):
                for item in page.get("CommonPrefixes") or []:
                    value = (item.get("Prefix") or "").rstrip("/")
                    if value:
                        found.append(value.rsplit("/", 1)[-1])
        except Exception as exc:  # noqa: BLE001
            raise S3Error(
                f"Could not list s3://{self.settings.bucket}/{base}: {exc}"
            ) from exc
        return found

    def delete_prefix(self, prefix: str) -> int:
        keys = [entry["key"] for entry in self.list_keys(prefix)]
        deleted = 0
        for start in range(0, len(keys), 1000):
            chunk = keys[start : start + 1000]
            try:
                self._s3.delete_objects(
                    Bucket=self.settings.bucket,
                    Delete={"Objects": [{"Key": k} for k in chunk]},
                )
            except Exception as exc:  # noqa: BLE001
                raise S3Error(
                    f"Could not delete under s3://{self.settings.bucket}/{prefix}: {exc}"
                ) from exc
            deleted += len(chunk)
        if deleted:
            logger.info("[s3] deleted %d object(s) under %s", deleted, prefix)
        return deleted

    def check_access(self) -> dict:
        """Round-trip a small object to prove the bucket is usable.

        Reports read, write and delete separately, so a too-narrow IAM policy
        produces a specific message instead of a generic failure at the first
        checkpoint.
        """
        probe_key = self.key("_healthcheck", "probe.json")
        result = {
            **self.settings.redacted(),
            "write": False,
            "read": False,
            "delete": False,
            "versioning": "",
            "error": "",
        }
        try:
            self.put_json(probe_key, {"ok": True})
            result["write"] = True
            result["read"] = self.get_json(probe_key) is not None
            self.delete_prefix(self.key("_healthcheck"))
            result["delete"] = True
        except S3Error as exc:
            result["error"] = str(exc)
            return result

        try:
            status = self._s3.get_bucket_versioning(Bucket=self.settings.bucket)
            result["versioning"] = status.get("Status", "Disabled") or "Disabled"
        except Exception:  # noqa: BLE001 — informational only
            result["versioning"] = "unknown (s3:GetBucketVersioning not granted)"
        return result

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _is_missing(exc: Exception) -> bool:
        """Whether the error means 'no such key/bucket' rather than a real fault."""
        code = ""
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = str((response.get("Error") or {}).get("Code") or "")
        return code in ("404", "NoSuchKey", "NoSuchBucket", "NotFound")


def build_client(settings: S3Settings | None = None) -> S3Client:
    return S3Client(settings)
