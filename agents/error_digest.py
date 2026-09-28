"""Turning a failed load into a compact, aggregated error digest.

A failed Bulk load can produce tens of thousands of rows that say the same thing
about the same field. The digest collapses them to one entry per status code and
field with a count, which is what makes a repair pass cost a predictable and
small number of tokens no matter how large the failure was.

On data exposure: a digest carries a small number of truncated sample values,
because the offending value is usually the only way to tell a wrong date format
from a wrong timezone. Sample count and length are capped, and setting
``AGENT_DIGEST_SAMPLES=0`` removes them entirely at the cost of a less
well-informed repair.
"""
from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from common.logger import logger
from config.settings import PATH_TO_RESULTS

# Salesforce status codes are upper snake case and lead the message.
_STATUS_CODE = re.compile(r"^([A-Z][A-Z0-9_]{3,}):")
_BRACKETED_FIELDS = re.compile(r"\[([^\]]+)\]")

_MAX_SAMPLE_LENGTH = 40
_MAX_ENTRIES = 25


def _sample_limit() -> int:
    try:
        return max(0, int(os.getenv("AGENT_DIGEST_SAMPLES") or 2))
    except ValueError:
        return 2


@dataclass
class ErrorEntry:
    code: str
    message: str
    fields: list[str] = field(default_factory=list)
    count: int = 0
    samples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        entry = {
            "code": self.code,
            "message": self.message,
            "fields": self.fields,
            "row_count": self.count,
        }
        if self.samples:
            entry["sample_values"] = self.samples
        return entry


@dataclass
class ErrorDigest:
    mapping: str
    destination_object: str = ""
    failed_rows: int = 0
    entries: list[ErrorEntry] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.entries

    @property
    def implicated_fields(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries:
            for name in entry.fields:
                if name not in seen:
                    seen.append(name)
        return seen

    def to_dict(self) -> dict:
        return {
            "mapping": self.mapping,
            "destination_object": self.destination_object,
            "failed_rows": self.failed_rows,
            "errors": [entry.to_dict() for entry in self.entries],
        }


def parse_error(message: str) -> tuple[str, str, list[str]]:
    """Split one ``sf__Error`` value into status code, text and field names.

    Bulk API errors carry the offending fields both in a bracketed list and as a
    trailing colon-separated segment, and which one appears depends on the error.
    Both are read, and anything that does not look like a field name is dropped.
    """
    raw = (message or "").strip()
    if not raw:
        return "", "", []

    code = ""
    match = _STATUS_CODE.match(raw)
    if match:
        code = match.group(1)
        raw = raw[len(code) + 1 :].strip()

    raw = raw.rstrip("-").strip()

    fields: list[str] = []
    for group in _BRACKETED_FIELDS.findall(raw):
        for name in group.split(","):
            candidate = name.strip()
            if candidate and _looks_like_field(candidate):
                fields.append(candidate)

    # A trailing ":FieldName" segment is the other place Bulk reports the field.
    if ":" in raw:
        tail = raw.rsplit(":", 1)[-1].strip()
        for candidate in tail.split(","):
            candidate = candidate.strip()
            if candidate and _looks_like_field(candidate) and candidate not in fields:
                fields.append(candidate)

    return code, raw, fields


def _looks_like_field(value: str) -> bool:
    if " " in value or len(value) > 60:
        return False
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(__c|__r)?", value))


def from_messages(
    mapping: str,
    messages: list[str],
    destination_object: str = "",
    failed_rows: int = 0,
) -> ErrorDigest:
    """Build a digest from the unique error strings the loader already collected."""
    digest = ErrorDigest(
        mapping=mapping, destination_object=destination_object, failed_rows=failed_rows
    )
    merged: dict[tuple[str, str], ErrorEntry] = {}
    for message in messages:
        code, text, fields = parse_error(message)
        if not text:
            continue
        key = (code, ",".join(fields) or text[:60])
        entry = merged.get(key)
        if entry is None:
            entry = ErrorEntry(code=code, message=text, fields=fields)
            merged[key] = entry
        entry.count += 1
    digest.entries = list(merged.values())[:_MAX_ENTRIES]
    return digest


def latest_job_dir(root: Path) -> Path | None:
    """The highest-numbered job folder under a results root, if any."""
    numbered = [child for child in root.iterdir() if child.is_dir() and child.name.isdigit()]
    return max(numbered, key=lambda child: int(child.name)) if numbered else None


def from_results(
    mapping: str,
    destination_object: str,
    transformation_name: str = "",
    job_table_id: int | None = None,
) -> ErrorDigest:
    """Build a digest by reading the failed-result CSVs written during the load.

    Richer than :func:`from_messages` because the row is available, so a sample of
    the value the destination rejected can be attached to the field it belongs to.
    """
    folder = transformation_name or destination_object
    digest = ErrorDigest(mapping=mapping, destination_object=destination_object)
    if not folder:
        return digest

    root = PATH_TO_RESULTS / folder
    if not root.exists():
        return digest
    if job_table_id is not None and (root / str(job_table_id)).exists():
        root = root / str(job_table_id)
    else:
        # Every load attempt keeps its own numbered folder. Only the newest says
        # what is failing now: earlier ones hold rows that have since loaded.
        latest = latest_job_dir(root)
        if latest is not None:
            root = latest

    sample_limit = _sample_limit()
    merged: dict[tuple[str, str], ErrorEntry] = {}

    for failed_csv in sorted(root.rglob("failed.csv")):
        try:
            text = failed_csv.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("[agent] could not read %s: %s", failed_csv, exc)
            continue
        if not text.strip():
            continue
        try:
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                code, message, fields = parse_error(row.get("sf__Error", ""))
                if not message:
                    continue
                digest.failed_rows += 1
                key = (code, ",".join(fields) or message[:60])
                entry = merged.get(key)
                if entry is None:
                    entry = ErrorEntry(code=code, message=message, fields=fields)
                    merged[key] = entry
                entry.count += 1
                if sample_limit and len(entry.samples) < sample_limit:
                    for name in fields:
                        value = (row.get(name) or "").strip()
                        if value:
                            entry.samples.append(
                                f"{name}={value[:_MAX_SAMPLE_LENGTH]}"
                            )
                            break
        except csv.Error as exc:
            logger.warning("[agent] malformed failed-results CSV %s: %s", failed_csv, exc)

    digest.entries = sorted(merged.values(), key=lambda e: e.count, reverse=True)[
        :_MAX_ENTRIES
    ]
    return digest


def latest_results_root(destination_object: str, transformation_name: str = "") -> Path:
    """Where the loader writes results for one mapping."""
    return PATH_TO_RESULTS / (transformation_name or destination_object)
