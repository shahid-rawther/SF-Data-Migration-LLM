"""Cached access to source-table and destination-object field metadata.

A describe is expensive on both sides — ServiceNow spends one request per level
of the table inheritance chain plus one on ``sys_dictionary``, and every
Salesforce describe is preceded by an OAuth token request — and walking a
prerequisite chain describes a dozen objects at a time. Everything here exists
so that cost is paid once per object per environment rather than once per
request.

Entries are keyed by the environment they came from, so pointing the tool at a
different instance or org never serves fields from the previous one.
"""
from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from common.logger import logger
from integrations.base import SourceField
from services.external.db_client import db_client

DEFAULT_TTL_HOURS = 24
MAX_PARALLEL_DESCRIBES = 6

SCOPE_SOURCE = "source"
SCOPE_DESTINATION = "destination"


def _ttl() -> timedelta:
    try:
        hours = float(os.getenv("SCHEMA_CACHE_TTL_HOURS", DEFAULT_TTL_HOURS))
    except ValueError:
        hours = DEFAULT_TTL_HOURS
    return timedelta(hours=max(hours, 0.0))


def connection_key(credentials: dict) -> str:
    """Fingerprint the environment a describe came from.

    Hashed rather than stored raw: the host and principal are enough to separate
    environments, and neither belongs in a cache table in clear text.
    """
    host = (
        credentials.get("base_url")
        or credentials.get("instance_url")
        or credentials.get("domain")
        or ""
    ).rstrip("/")
    principal = (
        credentials.get("username")
        or credentials.get("email")
        or credentials.get("client_id")
        or ""
    )
    return hashlib.sha256(f"{host}|{principal}".encode()).hexdigest()[:32]


def _read(scope: str, system: str, key: str, name: str) -> list | None:
    try:
        row = db_client.fetch_one(
            "SELECT fields, fetched_at FROM schema_cache "
            "WHERE scope = ? AND system = ? AND connection_key = ? AND object_name = ?",
            (scope, system, key, name),
        )
    except Exception:
        return None
    if not row:
        return None
    try:
        fetched_at = datetime.fromisoformat(row[1])
    except (TypeError, ValueError):
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - fetched_at > _ttl():
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def _write(scope: str, system: str, key: str, name: str, payload: list) -> None:
    try:
        db_client.execute(
            "INSERT OR REPLACE INTO schema_cache "
            "(scope, system, connection_key, object_name, fields, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                scope,
                system,
                key,
                name,
                json.dumps(payload),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    except Exception:
        logger.debug("Could not cache %s schema for %s", scope, name, exc_info=True)


# ── Source tables ──────────────────────────────────────────────────────────────


def _source_field_to_dict(field: SourceField) -> dict:
    return {
        "name": field.name,
        "label": field.label,
        "field_type": field.field_type,
        "required": field.required,
        "example": field.example,
        "reference_table": getattr(field, "reference_table", ""),
    }


def _source_field_from_dict(raw: dict) -> SourceField:
    return SourceField(
        name=raw.get("name", ""),
        label=raw.get("label", ""),
        field_type=raw.get("field_type", ""),
        required=bool(raw.get("required", False)),
        example=raw.get("example", ""),
        reference_table=raw.get("reference_table", ""),
    )


def get_source_table_fields(
    source_type: str,
    table: str,
    credentials: dict,
    *,
    refresh: bool = False,
) -> list[SourceField]:
    """Return a source table's fields, describing it only on a cache miss.

    An empty describe is not cached: providers swallow their errors and return
    [], and storing that would hide a transient outage for the whole TTL.
    """
    if not table:
        return []
    from api import sources as source_registry

    key = connection_key(credentials)
    if not refresh:
        cached = _read(SCOPE_SOURCE, source_type, key, table)
        if cached is not None:
            return [_source_field_from_dict(f) for f in cached]

    provider = source_registry.get(source_type)
    if not provider:
        return []
    try:
        fields = provider.get_table_fields(table, credentials)
    except Exception:
        logger.warning("Describe failed for %s.%s", source_type, table, exc_info=True)
        return []

    if fields:
        _write(
            SCOPE_SOURCE, source_type, key, table, [_source_field_to_dict(f) for f in fields]
        )
    return fields


# ── Destination objects ────────────────────────────────────────────────────────


def get_destination_object_fields(
    sf_object: str,
    credentials: dict,
    record_type_id: str = "",
    *,
    refresh: bool = False,
) -> list[dict]:
    """Return a Salesforce object's field metadata, describing on a cache miss.

    A record type is part of the cache identity, not just an argument: it
    changes which picklist values come back, so a scoped describe served from
    an unscoped entry would report choices the load then rejects.
    """
    if not sf_object:
        return []
    from api.compat_engine import get_destination_fields

    key = connection_key(credentials)
    entry = f"{sf_object}@{record_type_id}" if record_type_id else sf_object
    if not refresh:
        cached = _read(SCOPE_DESTINATION, "salesforce", key, entry)
        if cached is not None:
            return cached

    try:
        fields = get_destination_fields(sf_object, credentials, record_type_id)
    except Exception:
        logger.warning("Describe failed for Salesforce %s", sf_object, exc_info=True)
        return []

    if fields:
        _write(SCOPE_DESTINATION, "salesforce", key, entry, fields)
    return fields


def get_many_destination_object_fields(
    objects: list[str],
    credentials: dict,
) -> dict[str, list[dict]]:
    """Describe several Salesforce objects at once, in parallel where needed.

    Traversal proceeds one level at a time and the objects in a level are
    independent, so describing them concurrently turns a level into roughly one
    round trip instead of one per object.
    """
    wanted = [o for o in dict.fromkeys(objects) if o]
    if not wanted:
        return {}

    key = connection_key(credentials)
    resolved: dict[str, list[dict]] = {}
    misses: list[str] = []
    for name in wanted:
        cached = _read(SCOPE_DESTINATION, "salesforce", key, name)
        if cached is not None:
            resolved[name] = cached
        else:
            misses.append(name)

    if misses:
        workers = min(len(misses), MAX_PARALLEL_DESCRIBES)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            resolved.update(
                dict(
                    pool.map(
                        lambda o: (o, get_destination_object_fields(o, credentials)), misses
                    )
                )
            )
    return resolved


def invalidate(scope: str, system: str, credentials: dict, name: str | None = None) -> int:
    """Drop cached schema for one object, or for the whole environment."""
    key = connection_key(credentials)
    try:
        if name:
            cursor = db_client.execute(
                "DELETE FROM schema_cache WHERE scope = ? AND system = ? "
                "AND connection_key = ? AND object_name = ?",
                (scope, system, key, name),
            )
        else:
            cursor = db_client.execute(
                "DELETE FROM schema_cache WHERE scope = ? AND system = ? AND connection_key = ?",
                (scope, system, key),
            )
        return getattr(cursor, "rowcount", 0) or 0
    except Exception:
        return 0
