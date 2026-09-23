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


def add_selected_objects(session_id: str, objects: list[str]) -> dict:
    """Extend a session's object selection, returning what was actually added.

    The selection is held twice — in its own column, which the status and
    checkpoint code reads, and inside the config JSON, which is what a run
    actually obeys — so both are written here. Updating only one leaves a session
    whose reported contents and executed contents disagree.

    Adding is the only supported edit. Dropping an object that has already loaded
    would leave records in the destination that the session no longer accounts
    for, and would narrow the id map a later object's lookups resolve through.
    """
    row = db_client.fetch_one(
        "SELECT selected_objects, config FROM migration_sessions WHERE id = ?",
        (session_id,),
    )
    if not row:
        raise KeyError(f"No migration session with id {session_id}")

    current: list[str] = json.loads(row[0] or "[]")
    config: dict = json.loads(row[1] or "{}")
    added = [name for name in dict.fromkeys(objects) if name and name not in current]
    selection = current + added

    config["selected_objects"] = selection
    db_client.execute(
        "UPDATE migration_sessions SET selected_objects=?, config=?, updated_at=? "
        "WHERE id=?",
        (json.dumps(selection), json.dumps(config), _now(), session_id),
    )
    return {"selected_objects": selection, "added": added}


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


def mark_session_completed(session_id: str, had_errors: bool = False) -> None:
    # When some records failed to load, distinguish the session from a clean run
    # so the UI/reporting doesn't show a misleading "success".
    status = "completed_with_errors" if had_errors else "completed"
    db_client.execute(
        "UPDATE migration_sessions SET status=?, completed_at=?, updated_at=? WHERE id=?",
        (status, _now(), _now(), session_id),
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
