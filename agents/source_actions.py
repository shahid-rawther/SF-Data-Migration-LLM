"""The source-system changes a readiness suggestion may make, and nothing else.

Some gaps are best closed where the data lives: a required value that is
missing on a few hundred incidents belongs on those incidents, not only in a
default the migration invents. A readiness suggestion may therefore propose
one kind of write against the source:

* ``fill_source_values`` — set one column to one value on the records of a
  source table where that column is empty, optionally narrowed by an extra
  filter. Records that already have a value are never touched.

It is validated against the live source before a person sees it, and again at
apply time. On Apply the exact record ids are read first and journalled, so
an undo empties the column again on those records only — and only where it
still holds the value written, so a later edit by someone else is kept.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from common.logger import logger

from agents.context import MigrationContext

ACTION_FILL_SOURCE_VALUES = "fill_source_values"
ACTION_KINDS = frozenset({ACTION_FILL_SOURCE_VALUES})

_SYS_ID = re.compile(r"^[0-9a-f]{32}$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_VALUE_LENGTH = 255
_REVERT_CHUNK = 100


def max_rows() -> int:
    """The most records one action may write; beyond it the fix belongs in the mapping."""
    try:
        return max(1, int(os.getenv("READINESS_SOURCE_MAX_ROWS", "2000")))
    except ValueError:
        return 2000


class SourceWriteError(Exception):
    """The source system refused or failed a write."""


@dataclass
class ActionOutcome:
    accepted: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    def reject(self, payload: dict, reason: str) -> None:
        self.rejected.append({"source_action": payload, "reason": reason})
        logger.warning("[readiness] rejected source action %s: %s", payload.get("kind"), reason)


def normalise(item: dict) -> dict:
    return {
        "kind": str(item.get("kind") or ""),
        "table": str(item.get("table") or "").strip(),
        "field": str(item.get("field") or "").strip().split(".")[-1],
        "value": "" if item.get("value") is None else str(item.get("value")),
        "filter": str(item.get("filter") or "").strip().lstrip("^"),
        "rationale": str(item.get("rationale") or ""),
    }


def selection_query(action: dict) -> str:
    """The records an action writes: its filter, and only where the column is empty."""
    empty = f"{action['field']}ISEMPTY"
    return f"{action['filter']}^{empty}" if action["filter"] else empty


def _provider(context: MigrationContext):
    from api import sources as source_registry

    return source_registry.get(context.source_type)


def _migrated_tables(context: MigrationContext) -> set[str]:
    from services.internal.mappings_service import cfg_source_tables

    return {t for config in context.configs.values() for t in cfg_source_tables(config) if t}


def validate(context: MigrationContext, raw_actions: list) -> ActionOutcome:
    outcome = ActionOutcome()
    for item in raw_actions or []:
        if not isinstance(item, dict):
            continue
        action = normalise(item)
        error = _validate_one(context, action)
        if error:
            outcome.reject(action, error)
        else:
            outcome.accepted.append(action)
    return outcome


def _validate_one(context: MigrationContext, action: dict) -> str:
    if action["kind"] not in ACTION_KINDS:
        return f"Unsupported source action '{action['kind']}'"
    if not action["rationale"]:
        return "A rationale is required for every source action"
    provider = _provider(context)
    if provider is None or type(provider).update_record is _base_update():
        return f"{context.source_type or 'The source'} cannot be written to from here"
    table, column, value = action["table"], action["field"], action["value"]
    if not _IDENTIFIER.match(table) or not _IDENTIFIER.match(column):
        return "A source action needs a table and a column name"
    if table not in _migrated_tables(context):
        return f"{table} is not a table any mapping in this migration reads"
    if column.startswith("sys_"):
        return f"{column} is a system column and is not written"
    fields = {entry["name"].split(".")[-1]: entry for entry in context.source_fields(table)}
    if not fields:
        return f"{table} could not be described, so the change cannot be checked"
    meta = fields.get(column)
    if not meta:
        return f"'{column}' is not a column on {table}"
    if not value.strip():
        return "A value to write is required"
    if len(value) > _MAX_VALUE_LENGTH:
        return f"The value is longer than {_MAX_VALUE_LENGTH} characters"
    if "^" in value:
        # Undo finds the written records by value in an encoded query.
        return "The value may not contain '^'"
    # An encoded-query OR block would widen the selection past the empty rows.
    if "^NQ" in action["filter"] or action["filter"].upper().startswith("OR"):
        return "The filter may only narrow the records, not add an OR block"

    reference = str(meta.get("reference_table") or "")
    if reference:
        if not _SYS_ID.match(value):
            return f"{column} is a reference to {reference} and needs a sys_id, not '{value[:40]}'"
        if provider.count_rows(reference, context.source_credentials, f"sys_id={value}") != 1:
            return f"No {reference} record has sys_id {value}"

    affected = provider.count_rows(table, context.source_credentials, selection_query(action))
    if affected < 0:
        return f"Could not count the {table} records this would change"
    if affected == 0:
        return f"No {table} record matching it has an empty {column}"
    if affected > max_rows():
        return (
            f"It would change {affected} {table} records, more than the {max_rows()} "
            "allowed in one step; use a mapping fallback instead"
        )
    action["affected"] = affected
    action["reference_table"] = reference
    return ""


def _base_update():
    from integrations.base import SourcePlugin

    return SourcePlugin.update_record


# ── execution ──────────────────────────────────────────────────────────────────


def execute(actions: list[dict], context: MigrationContext) -> list[dict]:
    """Write each approved action and return the records it changed.

    If a write fails, the records already written by this call are emptied
    again before the error is raised.
    """
    provider = _provider(context)
    credentials = context.source_credentials
    results: list[dict] = []
    try:
        for index, action in enumerate(actions):
            ids = provider.find_record_ids(
                action["table"], credentials, selection_query(action), max_rows()
            )
            result = {
                "index": index,
                "kind": action["kind"],
                "table": action["table"],
                "field": action["field"],
                "value": action["value"],
                "record_ids": [],
            }
            results.append(result)
            for record_id in ids:
                provider.update_record(
                    action["table"], record_id, {action["field"]: action["value"]}, credentials
                )
                result["record_ids"].append(record_id)
            logger.info(
                "[readiness] set %s.%s on %d record(s)",
                action["table"],
                action["field"],
                len(result["record_ids"]),
            )
    except Exception as exc:
        for result in reversed(results):
            try:
                restore(result, context.source_type, credentials)
            except Exception:  # noqa: BLE001 — reported by the caller's error
                logger.exception("[readiness] could not roll back %s.%s", result["table"], result["field"])
        raise SourceWriteError(f"Writing to the source failed and was rolled back: {exc}") from exc
    return results


def restore(change: dict, source_type: str, credentials: dict) -> dict:
    """Empty the column again where it still holds the value that was written."""
    from api import sources as source_registry

    provider = source_registry.get(source_type)
    if provider is None:
        raise ValueError(f"No source provider for '{source_type}'")
    ids = list(change.get("record_ids") or [])
    table, column, value = change["table"], change["field"], change["value"]
    restored = 0
    try:
        for start in range(0, len(ids), _REVERT_CHUNK):
            chunk = ids[start:start + _REVERT_CHUNK]
            unchanged = provider.find_record_ids(
                table, credentials, f"sys_idIN{','.join(chunk)}^{column}={value}", len(chunk)
            )
            for record_id in unchanged:
                provider.update_record(table, record_id, {column: ""}, credentials)
                restored += 1
    except Exception as exc:
        raise SourceWriteError(
            f"Undo stopped after {restored} of {len(ids)} {table} record(s): {exc}"
        ) from exc
    return {"restored": restored, "kept": len(ids) - restored}
