"""Migrations router — CRUD for migration sessions and lifecycle control."""
import asyncio
import json
import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api import session_db
from api.migration_manager import migration_manager

router = APIRouter(prefix="/migrations", tags=["migrations"])


# ── Pydantic models ────────────────────────────────────────────────────────────

class MigrationOptions(BaseModel):
    start_date: str = ""
    end_date: str = ""
    service_now_batch_size: int = 5000
    salesforce_batch_size: int = 1000
    disable_load: bool = False
    # Left unset, the agent settings decide. Set explicitly to run this migration
    # with or without the agent passes regardless of the deployment default.
    agent_autowire: bool | None = None
    agent_repair: bool | None = None

class CreateMigrationRequest(BaseModel):
    source_type: str = "servicenow"
    source_credentials: dict = {}
    destination_credentials: dict = {}
    selected_objects: list[str] = []
    options: MigrationOptions = MigrationOptions()


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/", status_code=201)
def create_migration(body: CreateMigrationRequest):
    session_id = str(uuid.uuid4())
    config = {
        "source_type": body.source_type,
        "source_credentials": body.source_credentials,
        "destination_credentials": body.destination_credentials,
        "selected_objects": body.selected_objects,
        "options": body.options.model_dump(),
    }
    session_db.create_session(
        session_id=session_id,
        source_type=body.source_type,
        selected_objects=body.selected_objects,
        config=config,
    )
    return {"session_id": session_id, "status": "pending"}


@router.get("/")
def list_migrations():
    return session_db.list_sessions()


@router.get("/{session_id}")
def get_migration(session_id: str):
    session = session_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    live_status = migration_manager.get_status(session_id)
    if live_status:
        session["live_progress"] = live_status
    return session


@router.post("/{session_id}/start")
def start_migration(session_id: str):
    session = session_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session["status"] in ("running", "completed"):
        raise HTTPException(status_code=409, detail=f"Session is already {session['status']}")

    # Reload raw config (with real credentials) directly from DB
    from services.external.db_client import db_client
    raw = db_client.fetch_one(
        "SELECT config FROM migration_sessions WHERE id = ?", (session_id,)
    )
    if not raw:
        raise HTTPException(status_code=404, detail="Session config not found")
    config = json.loads(raw[0])

    migration_manager.start_migration(session_id, config)
    return {"session_id": session_id, "status": "running"}


@router.post("/{session_id}/pause")
def pause_migration(session_id: str):
    _require_active(session_id)
    migration_manager.pause_migration(session_id)
    return {"session_id": session_id, "status": "paused"}


@router.post("/{session_id}/resume")
def resume_migration(session_id: str):
    _require_active(session_id)
    migration_manager.resume_migration(session_id)
    return {"session_id": session_id, "status": "running"}


@router.post("/{session_id}/stop")
def stop_migration(session_id: str):
    _require_active(session_id)
    migration_manager.stop_migration(session_id)
    return {"session_id": session_id, "status": "stopped"}


@router.post("/{session_id}/retry")
def retry_migration(session_id: str):
    """Retry a failed/stopped migration, skipping extraction if it already completed."""
    session = session_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session["status"] == "running":
        raise HTTPException(status_code=409, detail="Session is already running")

    from services.external.db_client import db_client
    raw = db_client.fetch_one(
        "SELECT config FROM migration_sessions WHERE id = ?", (session_id,)
    )
    if not raw:
        raise HTTPException(status_code=404, detail="Session config not found")
    config = json.loads(raw[0])

    last_phase = session.get("phase", "")
    # Skip extraction if it already completed (failed during transform/load)
    if last_phase in ("transformation", "loading"):
        config["skip_extraction"] = True

    # Reset stale job tracking so transformation/loading batches re-run
    config["reset_jobs"] = True

    migration_manager.start_migration(session_id, config)
    return {"session_id": session_id, "status": "running", "skipped_extraction": config.get("skip_extraction", False)}


@router.get("/{session_id}/progress")
async def stream_progress(session_id: str):
    """SSE endpoint — streams real-time progress events to the browser."""
    session = session_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async def event_generator() -> AsyncGenerator[str, None]:
        q = migration_manager.subscribe(session_id)
        try:
            # Send current state immediately so the client has an initial value
            current = migration_manager.get_status(session_id)
            if current:
                yield _sse(current)

            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield _sse(payload)
                    if payload.get("status") in ("completed", "failed", "stopped"):
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            migration_manager.unsubscribe(session_id, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _require_active(session_id: str) -> None:
    if not migration_manager.is_active(session_id):
        session = session_db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if session["status"] not in ("running", "paused"):
            raise HTTPException(
                status_code=409,
                detail=f"Session is not active (status: {session['status']})",
            )


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"
