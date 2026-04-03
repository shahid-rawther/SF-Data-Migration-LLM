"""Database helpers for migration_sessions table."""
import json
import sqlite3
from datetime import datetime, timezone

from services.external.db_client import db_client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_session(
    session_id: str,
    source_type: str,
    selected_objects: list[str],
    config: dict,
) -> None:
    sql = """
        INSERT INTO migration_sessions
            (id, source_type, selected_objects, config, status, updated_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
    """
    db_client.insert(
        sql,
        (
            session_id,
            source_type,
            json.dumps(selected_objects),
            json.dumps(config),
            _now(),
        ),
    )


def get_session(session_id: str) -> dict | None:
    row = db_client.fetch_one(
        "SELECT id, source_type, selected_objects, config, status, phase, "
        "started_at, updated_at, completed_at FROM migration_sessions WHERE id = ?",
        (session_id,),
    )
    return _row_to_dict(row) if row else None


def list_sessions() -> list[dict]:
    rows = db_client.fetch_all(
        "SELECT id, source_type, selected_objects, config, status, phase, "
        "started_at, updated_at, completed_at FROM migration_sessions "
        "ORDER BY updated_at DESC"
    )
    return [_row_to_dict(r) for r in rows]


def update_session_status(session_id: str, status: str, phase: str | None = None) -> None:
    if phase is not None:
        db_client.execute(
            "UPDATE migration_sessions SET status=?, phase=?, updated_at=? WHERE id=?",
            (status, phase, _now(), session_id),
        )
    else:
        db_client.execute(
            "UPDATE migration_sessions SET status=?, updated_at=? WHERE id=?",
            (status, _now(), session_id),
        )


def mark_session_started(session_id: str) -> None:
    db_client.execute(
        "UPDATE migration_sessions SET status='running', phase='extraction', "
        "started_at=?, updated_at=? WHERE id=?",
        (_now(), _now(), session_id),
    )


def mark_session_completed(session_id: str) -> None:
    db_client.execute(
        "UPDATE migration_sessions SET status='completed', completed_at=?, updated_at=? WHERE id=?",
        (_now(), _now(), session_id),
    )


def mark_session_failed(session_id: str) -> None:
    db_client.execute(
        "UPDATE migration_sessions SET status='failed', completed_at=?, updated_at=? WHERE id=?",
        (_now(), _now(), session_id),
    )


def _row_to_dict(row: tuple) -> dict:
    keys = [
        "id",
        "source_type",
        "selected_objects",
        "config",
        "status",
        "phase",
        "started_at",
        "updated_at",
        "completed_at",
    ]
    d = dict(zip(keys, row))
    d["selected_objects"] = json.loads(d["selected_objects"] or "[]")
    raw_config = d.get("config") or "{}"
    parsed = json.loads(raw_config)
    # Scrub credentials before returning to clients
    for key in ("password", "client_secret"):
        if key in parsed.get("source_credentials", {}):
            parsed["source_credentials"][key] = "***"
        if key in parsed.get("destination_credentials", {}):
            parsed["destination_credentials"][key] = "***"
    d["config"] = parsed
    return d
