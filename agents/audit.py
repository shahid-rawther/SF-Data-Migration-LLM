"""Journal of agent-applied configuration changes, and per-item revert.

Every write an agent makes to a mapping config is recorded with the value it
replaced, which is what makes a change reversible one item at a time rather
than only as a whole run.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from common.logger import logger
from services.external.db_client import db_client

TARGET_FIELD = "field"
TARGET_CONFIG = "config"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_BLOCKER = "blocker"

_COLUMNS = (
    "id",
    "run_id",
    "session_id",
    "agent",
    "kind",
    "mapping_name",
    "target",
    "field",
    "previous_value",
    "new_value",
    "rationale",
    "severity",
    "applied",
    "applied_at",
    "reverted_at",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ChangeRecord:
    """One pending or applied change to a mapping config."""

    agent: str
    kind: str
    mapping_name: str
    field_name: str = ""
    target: str = TARGET_FIELD
    previous_value: object = None
    new_value: object = None
    rationale: str = ""
    severity: str = SEVERITY_INFO
    applied: bool = True

    def describe(self) -> str:
        where = f"{self.mapping_name}.{self.field_name}" if self.field_name else self.mapping_name
        return f"{self.kind} on {where}"

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "kind": self.kind,
            "mapping": self.mapping_name,
            "target": self.target,
            "field": self.field_name,
            "previous_value": self.previous_value,
            "new_value": self.new_value,
            "rationale": self.rationale,
            "severity": self.severity,
            "applied": self.applied,
        }


@dataclass
class Notice:
    """Something the expert should see, whether or not anything was changed."""

    kind: str
    mapping_name: str
    field_name: str = ""
    severity: str = SEVERITY_INFO
    detail: str = ""
    resolution: str = ""
    references: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "mapping": self.mapping_name,
            "field": self.field_name,
            "severity": self.severity,
            "detail": self.detail,
            "resolution": self.resolution,
            "references": self.references,
        }


def record_changes(
    run_id: str,
    changes: list[ChangeRecord],
    session_id: str = "",
) -> list[int]:
    """Persist applied changes and return their journal ids."""
    ids: list[int] = []
    now = _now()
    for change in changes:
        change_id = db_client.insert(
            """
            INSERT INTO agent_changes
                (run_id, session_id, agent, kind, mapping_name, target, field,
                 previous_value, new_value, rationale, severity, applied, applied_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                session_id,
                change.agent,
                change.kind,
                change.mapping_name,
                change.target,
                change.field_name,
                json.dumps(change.previous_value, default=str),
                json.dumps(change.new_value, default=str),
                change.rationale,
                change.severity,
                1 if change.applied else 0,
                now,
            ),
        )
        ids.append(change_id)
    if ids:
        logger.info("[agent] journalled %d change(s) for run %s", len(ids), run_id)
    return ids


def _row_to_dict(row: tuple) -> dict:
    entry = dict(zip(_COLUMNS, row))
    for key in ("previous_value", "new_value"):
        try:
            entry[key] = json.loads(entry[key])
        except (TypeError, ValueError):
            entry[key] = None
    entry["applied"] = bool(entry["applied"])
    entry["reverted"] = entry["reverted_at"] is not None
    return entry


def list_changes(
    run_id: str | None = None,
    session_id: str | None = None,
    mapping_name: str | None = None,
    include_reverted: bool = True,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if mapping_name:
        clauses.append("mapping_name = ?")
        params.append(mapping_name)
    if not include_reverted:
        clauses.append("reverted_at IS NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db_client.fetch_all(
        f"SELECT {', '.join(_COLUMNS)} FROM agent_changes {where} ORDER BY id DESC",
        tuple(params),
    )
    return [_row_to_dict(row) for row in rows]


def get_change(change_id: int) -> dict | None:
    row = db_client.fetch_one(
        f"SELECT {', '.join(_COLUMNS)} FROM agent_changes WHERE id = ?",
        (change_id,),
    )
    return _row_to_dict(row) if row else None


def revert_change(change_id: int) -> dict:
    """Restore the value one journalled change replaced.

    Reverting is itself deterministic: the stored previous value is written back
    verbatim, so a revert never depends on the model being available.
    """
    from agents import wiring

    change = get_change(change_id)
    if not change:
        raise KeyError(f"No agent change with id {change_id}")
    if change["reverted"]:
        return {"id": change_id, "reverted": True, "already": True}
    if not change["applied"]:
        raise ValueError(f"Change {change_id} was never applied, nothing to revert")

    wiring.restore_value(
        mapping_name=change["mapping_name"],
        target=change["target"],
        field_name=change["field"],
        value=change["previous_value"],
    )
    db_client.execute(
        "UPDATE agent_changes SET reverted_at = ? WHERE id = ?",
        (_now(), change_id),
    )
    logger.info(
        "[agent] reverted change %d (%s on %s.%s)",
        change_id,
        change["kind"],
        change["mapping_name"],
        change["field"],
    )
    return {"id": change_id, "reverted": True, "already": False}


def revert_run(run_id: str) -> dict:
    """Revert every outstanding change from one agent run, newest first."""
    outstanding = [
        change
        for change in list_changes(run_id=run_id, include_reverted=False)
        if change["applied"]
    ]
    reverted: list[int] = []
    for change in outstanding:
        revert_change(change["id"])
        reverted.append(change["id"])
    return {"run_id": run_id, "reverted": reverted, "count": len(reverted)}
