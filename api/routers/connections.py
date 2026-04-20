"""Saved connections router — CRUD for reusable source / Salesforce connections."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api import connections_db
from api import sources as source_registry

router = APIRouter(prefix="/connections", tags=["connections"])


def _valid_connection_types() -> set[str]:
    """All registered source IDs plus 'salesforce' (the fixed destination)."""
    return {p.source_id for p in source_registry.get_all()} | {"salesforce"}


class CreateConnectionRequest(BaseModel):
    name: str
    type: str
    credentials: dict


class UpdateConnectionRequest(BaseModel):
    name: str | None = None
    credentials: dict | None = None


@router.get("/")
def list_connections(type: str | None = None):
    return connections_db.list_connections(conn_type=type)


@router.get("/{conn_id}")
def get_connection(conn_id: str):
    conn = connections_db.get_connection(conn_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    return conn


@router.post("/", status_code=201)
def create_connection(body: CreateConnectionRequest):
    valid_types = _valid_connection_types()
    if body.type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Type must be one of: {', '.join(sorted(valid_types))}",
        )
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Name is required")
    try:
        return connections_db.create_connection(body.name.strip(), body.type, body.credentials)
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(status_code=409, detail=f"A connection named '{body.name}' already exists")
        raise


@router.put("/{conn_id}")
def update_connection(conn_id: str, body: UpdateConnectionRequest):
    result = connections_db.update_connection(conn_id, name=body.name, credentials=body.credentials)
    if not result:
        raise HTTPException(status_code=404, detail="Connection not found")
    return result


@router.delete("/{conn_id}")
def delete_connection(conn_id: str):
    connections_db.delete_connection(conn_id)
    return {"ok": True}
