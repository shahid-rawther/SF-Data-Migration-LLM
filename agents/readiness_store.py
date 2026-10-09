"""Persistence for readiness checks and the Salesforce writes they made.

A check is stored whole, suggestions included, so Apply works from exactly
what the person was shown and never has to ask the model again.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from services.external.db_client import db_client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(text: str | None, fallback):
    try:
        return json.loads(text) if text else fallback
    except (TypeError, ValueError):
        return fallback


_READINESS_COLUMNS = (
    "id", "source_type", "mapping_names", "report", "summary", "gaps", "applied",
    "model_invoked", "error", "usage", "created_at",
)


def save_readiness(
    *,
    source_type: str,
    mapping_names: list[str],
    report: dict,
    summary: str,
    gaps: list[dict],
    model_invoked: bool,
    error: str,
    usage: dict,
) -> int:
    return db_client.insert(
        """
        INSERT INTO agent_readiness
            (source_type, mapping_names, report, summary, gaps, applied,
             model_invoked, error, usage, created_at)
        VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)
        """,
        (
            source_type,
            json.dumps(mapping_names),
            json.dumps(report, default=str),
            summary,
            json.dumps(gaps, default=str),
            1 if model_invoked else 0,
            error,
            json.dumps(usage, default=str),
            _now(),
        ),
    )


def get_readiness(readiness_id: int) -> dict | None:
    row = db_client.fetch_one(
        f"SELECT {', '.join(_READINESS_COLUMNS)} FROM agent_readiness WHERE id = ?",
        (readiness_id,),
    )
    if not row:
        return None
    entry = dict(zip(_READINESS_COLUMNS, row))
    entry["mapping_names"] = _loads(entry["mapping_names"], [])
    entry["report"] = _loads(entry["report"], {})
    entry["gaps"] = _loads(entry["gaps"], [])
    entry["applied"] = _loads(entry["applied"], {})
    entry["usage"] = _loads(entry["usage"], {})
    entry["model_invoked"] = bool(entry["model_invoked"])
    entry["sf_changes"] = list_sf_changes(readiness_id)
    entry["source_changes"] = list_source_changes(readiness_id)
    _mark_reverted(entry)
    return entry


def _mark_reverted(entry: dict) -> None:
    """Flag applied suggestions whose every change has since been undone.

    Reverting goes through the ordinary change journal, which knows nothing of
    readiness checks, so an applied suggestion would otherwise read as still in
    force after its mapping change and records were rolled back.
    """
    from agents import audit

    sf_reverted = {c["id"]: c["reverted"] for c in entry["sf_changes"]}
    source_reverted = {c["id"]: c["reverted"] for c in entry.get("source_changes") or []}
    for outcome in entry["applied"].values():
        run_id = outcome.get("run_id")
        mapping_changes = audit.list_changes(run_id=run_id) if run_id else []
        wanted = set(outcome.get("change_ids") or [])
        states = [c.get("reverted_at") is not None for c in mapping_changes if c["id"] in wanted]
        states += [sf_reverted.get(i, False) for i in outcome.get("sf_change_ids") or []]
        states += [source_reverted.get(i, False) for i in outcome.get("source_change_ids") or []]
        outcome["reverted"] = bool(states) and all(states)


def mark_applied(readiness_id: int, gap_index: int, suggestion_index: int, outcome: dict) -> None:
    current = get_readiness(readiness_id)
    if not current:
        return
    applied = current["applied"]
    applied[f"{gap_index}:{suggestion_index}"] = {**outcome, "applied_at": _now()}
    db_client.execute(
        "UPDATE agent_readiness SET applied = ? WHERE id = ?",
        (json.dumps(applied, default=str), readiness_id),
    )


# ── Salesforce writes ──────────────────────────────────────────────────────────

_SF_COLUMNS = (
    "id", "readiness_id", "run_id", "kind", "object_name", "record_id", "field_name",
    "payload", "rationale", "revertible", "created_at", "reverted_at",
)


def record_sf_change(
    *,
    readiness_id: int,
    run_id: str,
    kind: str,
    object_name: str,
    record_id: str = "",
    field_name: str = "",
    payload: dict | None = None,
    rationale: str = "",
    revertible: bool = False,
) -> int:
    return db_client.insert(
        """
        INSERT INTO agent_sf_changes
            (readiness_id, run_id, kind, object_name, record_id, field_name,
             payload, rationale, revertible, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            readiness_id,
            run_id,
            kind,
            object_name,
            record_id,
            field_name,
            json.dumps(payload or {}, default=str),
            rationale,
            1 if revertible else 0,
            _now(),
        ),
    )


def _sf_row(row: tuple) -> dict:
    entry = dict(zip(_SF_COLUMNS, row))
    entry["payload"] = _loads(entry["payload"], {})
    entry["revertible"] = bool(entry["revertible"])
    entry["reverted"] = entry["reverted_at"] is not None
    return entry


def list_sf_changes(readiness_id: int | None = None) -> list[dict]:
    where, params = ("WHERE readiness_id = ?", (readiness_id,)) if readiness_id else ("", ())
    rows = db_client.fetch_all(
        f"SELECT {', '.join(_SF_COLUMNS)} FROM agent_sf_changes {where} ORDER BY id DESC",
        params,
    )
    return [_sf_row(row) for row in rows]


def get_sf_change(change_id: int) -> dict | None:
    row = db_client.fetch_one(
        f"SELECT {', '.join(_SF_COLUMNS)} FROM agent_sf_changes WHERE id = ?",
        (change_id,),
    )
    return _sf_row(row) if row else None


def mark_sf_reverted(change_id: int) -> None:
    db_client.execute(
        "UPDATE agent_sf_changes SET reverted_at = ? WHERE id = ?", (_now(), change_id)
    )


# ── source writes ──────────────────────────────────────────────────────────────

_SOURCE_COLUMNS = (
    "id", "readiness_id", "run_id", "source_type", "kind", "table_name", "field_name",
    "value", "record_ids", "rationale", "created_at", "reverted_at",
)


def record_source_change(
    *,
    readiness_id: int,
    run_id: str,
    source_type: str,
    kind: str,
    table_name: str,
    field_name: str,
    value: str,
    record_ids: list[str],
    rationale: str = "",
) -> int:
    return db_client.insert(
        """
        INSERT INTO agent_source_changes
            (readiness_id, run_id, source_type, kind, table_name, field_name,
             value, record_ids, rationale, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            readiness_id,
            run_id,
            source_type,
            kind,
            table_name,
            field_name,
            value,
            json.dumps(record_ids),
            rationale,
            _now(),
        ),
    )


def _source_row(row: tuple) -> dict:
    entry = dict(zip(_SOURCE_COLUMNS, row))
    entry["record_ids"] = _loads(entry["record_ids"], [])
    entry["record_count"] = len(entry["record_ids"])
    entry["reverted"] = entry["reverted_at"] is not None
    return entry


def list_source_changes(readiness_id: int | None = None) -> list[dict]:
    where, params = ("WHERE readiness_id = ?", (readiness_id,)) if readiness_id else ("", ())
    rows = db_client.fetch_all(
        f"SELECT {', '.join(_SOURCE_COLUMNS)} FROM agent_source_changes {where} ORDER BY id DESC",
        params,
    )
    return [_source_row(row) for row in rows]


def get_source_change(change_id: int) -> dict | None:
    row = db_client.fetch_one(
        f"SELECT {', '.join(_SOURCE_COLUMNS)} FROM agent_source_changes WHERE id = ?",
        (change_id,),
    )
    return _source_row(row) if row else None


def mark_source_reverted(change_id: int) -> None:
    db_client.execute(
        "UPDATE agent_source_changes SET reverted_at = ? WHERE id = ?", (_now(), change_id)
    )
