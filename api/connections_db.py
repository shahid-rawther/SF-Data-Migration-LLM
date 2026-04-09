"""Database helpers for saved_connections table."""
import json
import uuid
from datetime import datetime, timezone

from services.external.db_client import db_client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_connection(name: str, conn_type: str, credentials: dict) -> dict:
    conn_id = str(uuid.uuid4())
    sql = """
        INSERT INTO saved_connections (id, name, type, credentials, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    now = _now()
    db_client.insert(sql, (conn_id, name, conn_type, json.dumps(credentials), now, now))
    return {"id": conn_id, "name": name, "type": conn_type, "created_at": now}


def list_connections(conn_type: str | None = None) -> list[dict]:
    if conn_type:
        rows = db_client.fetch_all(
            "SELECT id, name, type, credentials, created_at, updated_at "
            "FROM saved_connections WHERE type = ? ORDER BY name",
            (conn_type,),
        )
    else:
        rows = db_client.fetch_all(
            "SELECT id, name, type, credentials, created_at, updated_at "
            "FROM saved_connections ORDER BY name"
        )
    return [_row_to_dict(r) for r in rows]


def get_connection(conn_id: str) -> dict | None:
    row = db_client.fetch_one(
        "SELECT id, name, type, credentials, created_at, updated_at "
        "FROM saved_connections WHERE id = ?",
        (conn_id,),
    )
    return _row_to_dict(row) if row else None


def delete_connection(conn_id: str) -> bool:
    db_client.execute("DELETE FROM saved_connections WHERE id = ?", (conn_id,))
    return True


def update_connection(conn_id: str, name: str | None = None, credentials: dict | None = None) -> dict | None:
    existing = get_connection(conn_id)
    if not existing:
        return None
    new_name = name if name is not None else existing["name"]
    new_creds = credentials if credentials is not None else existing["credentials"]
    db_client.execute(
        "UPDATE saved_connections SET name=?, credentials=?, updated_at=? WHERE id=?",
        (new_name, json.dumps(new_creds), _now(), conn_id),
    )
    return get_connection(conn_id)


def _row_to_dict(row: tuple) -> dict:
    keys = ["id", "name", "type", "credentials", "created_at", "updated_at"]
    d = dict(zip(keys, row))
    d["credentials"] = json.loads(d["credentials"] or "{}")
    return d
