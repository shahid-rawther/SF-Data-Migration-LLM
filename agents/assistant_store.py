"""Persistence for diagnoses, the assistant conversation, and proposals.

A proposal is a set of already-validated resolutions waiting for a person to
approve them. Storing it, rather than holding it in the model's reply, is what
lets Apply be a deterministic operation that never calls the model.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from services.external.db_client import db_client

PROPOSAL_PENDING = "pending"
PROPOSAL_APPLIED = "applied"
PROPOSAL_REJECTED = "rejected"
PROPOSAL_FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(text: str | None, fallback):
    try:
        return json.loads(text) if text else fallback
    except (TypeError, ValueError):
        return fallback


# ── diagnoses ──────────────────────────────────────────────────────────────────

_DIAGNOSIS_COLUMNS = (
    "id", "session_id", "mapping_name", "digest", "analysis", "evidence",
    "suggestions", "created_at",
)


def save_diagnosis(
    session_id: str,
    mapping_name: str,
    digest: dict,
    analysis: str,
    evidence: list[str],
    suggestions: list[dict],
) -> int:
    return db_client.insert(
        """
        INSERT INTO agent_diagnoses
            (session_id, mapping_name, digest, analysis, evidence, suggestions, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            mapping_name,
            json.dumps(digest, default=str),
            analysis,
            json.dumps(evidence, default=str),
            json.dumps(suggestions, default=str),
            _now(),
        ),
    )


def _diagnosis_row(row: tuple) -> dict:
    entry = dict(zip(_DIAGNOSIS_COLUMNS, row))
    entry["digest"] = _loads(entry["digest"], {})
    entry["evidence"] = _loads(entry["evidence"], [])
    entry["suggestions"] = _loads(entry["suggestions"], [])
    return entry


def get_diagnosis(diagnosis_id: int) -> dict | None:
    row = db_client.fetch_one(
        f"SELECT {', '.join(_DIAGNOSIS_COLUMNS)} FROM agent_diagnoses WHERE id = ?",
        (diagnosis_id,),
    )
    return _diagnosis_row(row) if row else None


def list_diagnoses(session_id: str, since: str = "", latest_only: bool = True) -> list[dict]:
    """Newest first. With ``latest_only`` one per mapping — the most recent."""
    clauses = ["session_id = ?"]
    params: list[object] = [session_id]
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    rows = db_client.fetch_all(
        f"SELECT {', '.join(_DIAGNOSIS_COLUMNS)} FROM agent_diagnoses "
        f"WHERE {' AND '.join(clauses)} ORDER BY id DESC",
        tuple(params),
    )
    entries = [_diagnosis_row(row) for row in rows]
    if not latest_only:
        return entries
    seen: set[str] = set()
    latest: list[dict] = []
    for entry in entries:
        if entry["mapping_name"] in seen:
            continue
        seen.add(entry["mapping_name"])
        latest.append(entry)
    return latest


# ── conversation ───────────────────────────────────────────────────────────────

_MESSAGE_COLUMNS = ("id", "session_id", "role", "content", "proposal_id", "created_at")


def add_message(session_id: str, role: str, content: str, proposal_id: int | None = None) -> int:
    return db_client.insert(
        """
        INSERT INTO agent_messages (session_id, role, content, proposal_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (session_id, role, content, proposal_id, _now()),
    )


def list_messages(session_id: str, limit: int | None = None) -> list[dict]:
    """Oldest first; ``limit`` keeps the most recent N."""
    rows = db_client.fetch_all(
        f"SELECT {', '.join(_MESSAGE_COLUMNS)} FROM agent_messages "
        "WHERE session_id = ? ORDER BY id DESC"
        + (" LIMIT ?" if limit else ""),
        (session_id, limit) if limit else (session_id,),
    )
    return [dict(zip(_MESSAGE_COLUMNS, row)) for row in reversed(rows)]


def clear_messages(session_id: str) -> None:
    db_client.execute("DELETE FROM agent_messages WHERE session_id = ?", (session_id,))


# ── proposals ──────────────────────────────────────────────────────────────────

_PROPOSAL_COLUMNS = (
    "id", "session_id", "mapping_name", "summary", "resolutions", "preview",
    "rerun", "status", "run_id", "error", "created_at", "decided_at",
    "origin", "diagnosis_id",
)

ORIGIN_DIAGNOSIS = "diagnosis"
ORIGIN_CHAT = "chat"


def save_proposal(
    session_id: str,
    mapping_name: str,
    summary: str,
    resolutions: list[dict],
    preview: list[dict],
    rerun: list[str],
    origin: str = "",
    diagnosis_id: int | None = None,
) -> int:
    return db_client.insert(
        """
        INSERT INTO agent_proposals
            (session_id, mapping_name, summary, resolutions, preview, rerun, status,
             created_at, origin, diagnosis_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            mapping_name,
            summary,
            json.dumps(resolutions, default=str),
            json.dumps(preview, default=str),
            json.dumps(rerun),
            PROPOSAL_PENDING,
            _now(),
            origin,
            diagnosis_id,
        ),
    )


def _proposal_row(row: tuple) -> dict:
    entry = dict(zip(_PROPOSAL_COLUMNS, row))
    entry["resolutions"] = _loads(entry["resolutions"], [])
    entry["preview"] = _loads(entry["preview"], [])
    entry["rerun"] = _loads(entry["rerun"], [])
    return entry


def get_proposal(proposal_id: int) -> dict | None:
    row = db_client.fetch_one(
        f"SELECT {', '.join(_PROPOSAL_COLUMNS)} FROM agent_proposals WHERE id = ?",
        (proposal_id,),
    )
    return _proposal_row(row) if row else None


def list_proposals(session_id: str) -> list[dict]:
    rows = db_client.fetch_all(
        f"SELECT {', '.join(_PROPOSAL_COLUMNS)} FROM agent_proposals "
        "WHERE session_id = ? ORDER BY id DESC",
        (session_id,),
    )
    return [_proposal_row(row) for row in rows]


def applied_proposals_by_run(session_id: str) -> dict[str, dict]:
    """Applied proposals of a session, keyed by the audit run that wrote them."""
    rows = db_client.fetch_all(
        f"SELECT {', '.join(_PROPOSAL_COLUMNS)} FROM agent_proposals "
        "WHERE session_id = ? AND status = ? AND run_id != ''",
        (session_id, PROPOSAL_APPLIED),
    )
    return {entry["run_id"]: entry for entry in (_proposal_row(row) for row in rows)}


def decide_proposal(proposal_id: int, status: str, run_id: str = "", error: str = "") -> None:
    db_client.execute(
        "UPDATE agent_proposals SET status = ?, run_id = ?, error = ?, decided_at = ? "
        "WHERE id = ?",
        (status, run_id, error, _now(), proposal_id),
    )
