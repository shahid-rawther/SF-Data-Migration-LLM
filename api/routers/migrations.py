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
from common.logger import logger

router = APIRouter(prefix="/migrations", tags=["migrations"])


# ── Pydantic models ────────────────────────────────────────────────────────────

class MigrationOptions(BaseModel):
    start_date: str = ""
    end_date: str = ""
    # Caps rows extracted per source table, not per migration. Named for the
    # original ServiceNow-only path; it applies to every source.
    service_now_batch_size: int = 5000
    disable_load: bool = False
    # Left unset, the agent settings decide. Set explicitly to run this migration
    # with or without the agent passes regardless of the deployment default.
    agent_autowire: bool | None = None
    agent_repair: bool | None = None

class CreateMigrationRequest(BaseModel):
    source_type: str = "servicenow"
    source_credentials: dict = {}
    destination_credentials: dict = {}
    # The saved connections the credentials came from, when they came from one.
    # Recorded so that later work on this session — adding an object, describing
    # a Salesforce object, mapping its fields — reaches the same source and
    # destination without anyone re-entering or re-picking anything.
    source_connection_id: str | None = None
    destination_connection_id: str | None = None
    selected_objects: list[str] = []
    options: MigrationOptions = MigrationOptions()


class AddObjectsRequest(BaseModel):
    objects: list[str] = []


class LinkConnectionsRequest(BaseModel):
    source_connection_id: str | None = None
    destination_connection_id: str | None = None


class RunRemainingRequest(BaseModel):
    """Left empty, the objects that have not migrated yet are run."""

    objects: list[str] = []


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/", status_code=201)
def create_migration(body: CreateMigrationRequest):
    session_id = str(uuid.uuid4())
    config = {
        "source_type": body.source_type,
        "source_credentials": body.source_credentials,
        "destination_credentials": body.destination_credentials,
        "source_connection_id": body.source_connection_id or "",
        "destination_connection_id": body.destination_connection_id or "",
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


# ── Checkpoints ────────────────────────────────────────────────────────────────
# Declared before /{session_id} so "checkpoints" is not captured as a session id.


@router.get("/checkpoints/status")
def checkpoint_status():
    """Whether resumable sessions are configured, and whether the bucket works."""
    from services.internal import checkpoint_service

    return checkpoint_service.status()


@router.get("/checkpoints/health")
def checkpoint_health():
    """Round-trip an object to prove the bucket grants read, write and delete."""
    from services.external.s3_client import S3Client, S3Error

    try:
        return S3Client().check_access()
    except S3Error as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/checkpoints")
def list_checkpoints():
    """Every session the bucket knows about, including ones absent locally.

    This is what makes the bucket, rather than one instance, the record of what
    has been migrated: a replacement machine can list and restore prior runs.
    """
    from services.external.s3_client import S3Error
    from services.internal import checkpoint_service

    try:
        return checkpoint_service.list_saved()
    except S3Error as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── Deferred references (backfill) ─────────────────────────────────────────────
# A lookup caught in a reference cycle is loaded empty, because no order could
# have resolved it, and attached afterwards. That follow-up writes to records
# already in the destination, so none of it happens without an explicit decision:
# reading a plan never runs anything, approving only marks intent, and running
# refuses any unit that has not been approved.


class BackfillDecision(BaseModel):
    # False records a deliberate decision to leave the references unattached.
    approve: bool = True
    # Why, kept alongside the unit for whoever reads the run later.
    note: str = ""


@router.get("/backfill")
def list_backfill(session_id: str | None = None, status: str | None = None):
    """Deferred references and their state, re-checked against the id map.

    The resolvable count is recomputed on read rather than stored, because
    loading another object is exactly what changes it.
    """
    from services.internal import backfill_service

    units = backfill_service.list_units(
        session_id=session_id, status=status, refresh=True
    )
    return {
        "units": units,
        "pending": [u for u in units if u["status"] == "pending"],
        "ready": [
            u
            for u in units
            if u["status"] in ("pending", "approved") and u["rows_resolvable"] > 0
        ],
    }


@router.post("/backfill/{unit_id}/decision")
def decide_backfill(unit_id: int, body: BackfillDecision):
    """Approve or decline one deferred reference. Approving does not run it."""
    from services.internal import backfill_service

    try:
        return backfill_service.decide(unit_id, body.approve, body.note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/backfill/{unit_id}/run")
def run_backfill(unit_id: int):
    """Fill one approved reference, as an update pass over the affected records."""
    from services.internal import backfill_service

    try:
        return backfill_service.execute(unit_id, job_number=_backfill_job_number())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a 502
        logger.exception("Backfill unit %s failed", unit_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/{session_id}/backfill/run-approved")
def run_approved_backfill(session_id: str):
    """Fill every reference approved for this session. Pending ones are left."""
    from services.internal import backfill_service

    if not session_db.get_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        results = backfill_service.run_approved(session_id, _backfill_job_number())
    except Exception as exc:  # noqa: BLE001
        logger.exception("Backfill for session %s failed", session_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"session_id": session_id, "ran": results, "count": len(results)}


def _backfill_job_number() -> int:
    """A job number for the backfill's transform and load records."""
    from config.settings import LOADING_JOB_NUMBER

    return LOADING_JOB_NUMBER


# ── Filling a column after the fact ────────────────────────────────────────────
# Writing one column on records a session already loaded, because it needs a
# value it was never mapped to receive or the value it got was wrong.
#
# Unlike a backfill, nothing here is owed by the loader, so there is no approval
# gate. The preview is the check instead: it runs the same transform the update
# would run and reports what it would write, so the intent is visible before
# anything reaches the destination.


class ColumnFillSpecRequest(BaseModel):
    """A fill definition, plus what the preview expected it to touch.

    The spec is taken as a plain object because its rules nest and one of its
    keys is ``from``, which cannot be a field name here. The service validates it
    and owns the messages, so there is one account of what a valid fill is rather
    than two that can drift apart.
    """

    spec: dict = {}
    # Carried from the preview so the outcome can be read against the intent.
    rows_targeted: int = 0


@router.get("/column-fill")
def list_column_fills(session_id: str | None = None, status: str | None = None):
    """Fills requested for a session, most recent first."""
    from services.internal import column_fill_service

    jobs = column_fill_service.list_jobs(session_id=session_id or "", status=status or "")
    return {"jobs": jobs, "count": len(jobs)}


@router.post("/column-fill/{job_id}/run")
def run_column_fill(job_id: int):
    """Write the column, as an update pass over the records already loaded."""
    from services.internal import column_fill_service

    try:
        return column_fill_service.execute(job_id, job_number=_backfill_job_number())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a 502
        logger.exception("Column fill %s failed", job_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/column-fill/{job_id}/cancel")
def cancel_column_fill(job_id: int):
    """Abandon a draft, keeping it readable as a record of what was considered."""
    from services.internal import column_fill_service

    try:
        return column_fill_service.cancel(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{session_id}/column-fill/targets")
def column_fill_targets(session_id: str):
    """The objects in this session that have records a fill could update.

    Only objects with loaded records: a fill addresses destination ids, so an
    object that never loaded has nothing to write to.
    """
    from services.internal import column_fill_service

    session = session_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": session_id, "targets": column_fill_service.targets(session)}


@router.post("/{session_id}/column-fill/preview")
def preview_column_fill(session_id: str, body: ColumnFillSpecRequest):
    """What the fill would write, without writing any of it."""
    from services.internal import column_fill_service

    if not session_db.get_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        return column_fill_service.preview(session_id, body.spec)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # The spec itself is wrong, which is the caller's to fix.
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{session_id}/column-fill")
def create_column_fill(session_id: str, body: ColumnFillSpecRequest):
    """Record a fill as a draft. Nothing is written until it is run."""
    from services.internal import column_fill_service

    if not session_db.get_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        return column_fill_service.create(session_id, body.spec, body.rows_targeted)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{session_id}/checkpoint")
def get_checkpoint(session_id: str):
    """The saved resume state for one session."""
    from services.external.s3_client import S3Error
    from services.internal import checkpoint_service

    local = checkpoint_service.get_index(session_id)
    try:
        checkpoint = checkpoint_service.load(session_id)
    except S3Error as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not checkpoint:
        raise HTTPException(
            status_code=404, detail=f"No checkpoint saved for session {session_id}"
        )
    return {**checkpoint.summary(), "local_index": local}


@router.post("/{session_id}/checkpoint")
def save_checkpoint(session_id: str):
    """Force a save now, rather than waiting for the next phase boundary."""
    from services.external.s3_client import S3Error
    from services.internal import checkpoint_service

    session = session_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    live = migration_manager.get_status(session_id) or {}
    try:
        return checkpoint_service.save(
            session_id,
            progress={
                "phase": session.get("phase") or live.get("phase") or "",
                "records_fetched": live.get("records_fetched", 0),
                "records_processed": live.get("records_processed", 0),
                "records_failed": live.get("records_failed", 0),
            },
            records_by_object=live.get("records_by_object") or {},
            load_errors=live.get("load_errors") or {},
        )
    except (S3Error, checkpoint_service.CheckpointError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{session_id}/restore")
def restore_checkpoint(session_id: str):
    """Rehydrate the local database from the bucket without starting a run.

    Reports whether the local staged extract can be reused, so the caller knows
    if resuming will re-query the source.
    """
    from services.external.s3_client import S3Error
    from services.internal import checkpoint_service

    if migration_manager.is_active(session_id):
        raise HTTPException(
            status_code=409,
            detail="Session is running; stop it before restoring a checkpoint",
        )
    try:
        return checkpoint_service.restore(session_id)
    except (S3Error, checkpoint_service.CheckpointError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{session_id}/resume-from-checkpoint")
def resume_from_checkpoint(session_id: str):
    """Restore the saved state and continue the migration from where it stopped.

    Objects the checkpoint records as finished are skipped outright, and the rows
    that already loaded for the remaining objects are skipped by the transform,
    which reads ``id_mappings``. Extraction is re-run only when the local staged
    extract no longer matches the one the checkpoint was taken against.
    """
    from services.external.s3_client import S3Error
    from services.internal import checkpoint_service

    if migration_manager.is_active(session_id):
        raise HTTPException(status_code=409, detail="Session is already running")

    try:
        restored = checkpoint_service.restore(session_id)
    except (S3Error, checkpoint_service.CheckpointError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    config = _raw_config(session_id)
    if _credentials_missing(config):
        raise HTTPException(
            status_code=409,
            detail=(
                "This checkpoint was restored without credentials, which are "
                "never written to the bucket. Re-select the source and "
                "destination connections for this session, then resume."
            ),
        )

    config["skip_extraction"] = bool(restored.get("stage_reusable"))
    config["completed_objects"] = restored.get("completed_objects") or []
    config["reset_jobs"] = True

    migration_manager.start_migration(session_id, config)
    return {
        "session_id": session_id,
        "status": "running",
        "skipped_extraction": config["skip_extraction"],
        "skipped_objects": config["completed_objects"],
        "remaining_objects": restored.get("remaining_objects") or [],
        "id_mappings_restored": restored.get("id_mappings_restored", 0),
    }


@router.delete("/{session_id}/checkpoint")
def delete_checkpoint(session_id: str):
    """Remove a session's checkpoint from the bucket."""
    from services.external.s3_client import S3Error
    from services.internal import checkpoint_service

    try:
        return checkpoint_service.delete(session_id)
    except S3Error as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _status_for(session: dict, include_checkpoint: bool = True) -> dict:
    """Per-object status for one session, from the migrated ids and staged extract.

    The numbers are the same whether the session is running, paused or long
    finished. The saved checkpoint is consulted only to fill in per-object
    failures and, for a session whose staged extract has been cleared, the source
    row totals — and never at the cost of the response, so an unreachable bucket
    is simply absent from it.
    """
    from services.internal import session_status_service

    live = migration_manager.get_status(session["id"])
    summary: dict | None = None
    local_index: dict | None = None
    if include_checkpoint:
        from services.internal import checkpoint_service

        try:
            local_index = checkpoint_service.get_index(session["id"])
            if local_index:
                checkpoint = checkpoint_service.load(session["id"])
                if checkpoint:
                    summary = {**checkpoint.summary(), "stage": checkpoint.stage}
        except Exception as exc:  # noqa: BLE001 — the bucket is not required here
            logger.warning(
                "[status] checkpoint unavailable for session %s: %s", session["id"], exc
            )

    return session_status_service.build(
        session, live=live, checkpoint_summary=summary, local_index=local_index
    )


@router.get("/{session_id}/status")
def session_status(session_id: str, include_checkpoint: bool = True):
    """What this session has migrated, object by object."""
    session = session_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return _status_for(session, include_checkpoint)


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

    config = _raw_config(session_id)
    migration_manager.start_migration(session_id, config)
    return {"session_id": session_id, "status": "running"}


@router.post("/{session_id}/objects")
def add_session_objects(session_id: str, body: AddObjectsRequest):
    """Add objects to an existing session, without running anything.

    Only adding is supported. Dropping an object that has already loaded would
    leave records in the destination the session no longer accounts for, and would
    narrow the id map that a later object's lookups resolve through.
    """
    session = session_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session["status"] == "running":
        raise HTTPException(
            status_code=409,
            detail="Cannot change the selection while the session is running",
        )

    from services.internal.mappings_service import get_mapping_config

    unknown = [name for name in body.objects if not get_mapping_config(name)]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"No mapping exists for: {', '.join(sorted(unknown))}",
        )

    result = session_db.add_selected_objects(session_id, body.objects)
    logger.info(
        "[session %s] selection extended by %d object(s): %s",
        session_id,
        len(result["added"]),
        ", ".join(result["added"]) or "nothing new",
    )
    return result


# ── Connections ────────────────────────────────────────────────────────────────
# A session's credentials are masked on the way out, so the browser cannot use
# them to describe a schema. A saved connection can be named safely, and the
# server resolves it — which is what lets everything the wizard offers while
# choosing objects keep working once the session exists.


@router.get("/{session_id}/connections")
def session_connections(session_id: str):
    """The connections this session is linked to, and what it could link to.

    Sessions created before the link was recorded, and sessions restored from a
    checkpoint, have nothing to resolve. Rather than leave that to be guessed
    at, each saved connection of the right type is returned with whether it
    reaches the same place the session was created against, so the obvious
    answer can be offered instead of asked for.
    """
    session = session_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    from api import connections_db

    config = _raw_config(session_id)

    def side(conn_type: str, creds_key: str, id_key: str) -> dict:
        stored = config.get(creds_key) or {}
        candidates = [
            {
                "id": conn["id"],
                "name": conn["name"],
                "type": conn["type"],
                "matches_session": _same_endpoint(stored, conn.get("credentials") or {}),
            }
            for conn in connections_db.list_connections(conn_type)
        ]
        linked = config.get(id_key) or ""
        # A link to a connection that has since been deleted is not a link.
        if linked and not any(c["id"] == linked for c in candidates):
            linked = ""
        return {"connection_id": linked, "candidates": candidates}

    return {
        "session_id": session_id,
        "source_type": session["source_type"],
        "source": side(session["source_type"], "source_credentials", "source_connection_id"),
        "destination": side(
            "salesforce", "destination_credentials", "destination_connection_id"
        ),
    }


@router.post("/{session_id}/connections")
def link_session_connections(session_id: str, body: LinkConnectionsRequest):
    """Point the session at saved connections and adopt their credentials."""
    session = session_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session["status"] == "running":
        raise HTTPException(
            status_code=409,
            detail="Cannot change connections while the session is running",
        )

    from api import connections_db

    def resolve(conn_id: str | None, expected_type: str) -> dict | None:
        if not conn_id:
            return None
        conn = connections_db.get_connection(conn_id)
        if not conn:
            raise HTTPException(status_code=404, detail=f"No connection with id {conn_id}")
        if conn["type"] != expected_type:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Connection '{conn['name']}' is a {conn['type']} connection, "
                    f"but this session needs a {expected_type} one"
                ),
            )
        return conn

    source = resolve(body.source_connection_id, session["source_type"])
    destination = resolve(body.destination_connection_id, "salesforce")
    if not source and not destination:
        raise HTTPException(status_code=400, detail="No connection given to link")

    result = session_db.set_connection_ids(
        session_id,
        source_connection_id=source["id"] if source else None,
        destination_connection_id=destination["id"] if destination else None,
        source_credentials=source["credentials"] if source else None,
        destination_credentials=destination["credentials"] if destination else None,
    )
    logger.info(
        "[session %s] linked connections — source=%s destination=%s",
        session_id,
        source["name"] if source else "unchanged",
        destination["name"] if destination else "unchanged",
    )
    return result


@router.get("/{session_id}/run-plan")
def session_run_plan(session_id: str):
    """Which objects a run would work on, and which it would leave alone.

    Offered before launching rather than decided silently, because the difference
    between the two lists is the difference between adding to a session and
    migrating it all over again.
    """
    session = session_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    from services.internal.session_status_service import STATE_COMPLETED

    status = _status_for(session)
    pending: list[dict] = []
    settled: list[dict] = []
    for entry in status["objects"]:
        summary = {
            "object": entry["object"],
            "destination_object": entry["destination_object"],
            "state": entry["state"],
            "loaded": entry["loaded"],
            "source_rows": entry["source_rows"],
        }
        (settled if entry["state"] == STATE_COMPLETED else pending).append(summary)

    return {
        "session_id": session_id,
        "status": session["status"],
        # Default rather than decision: the caller may send its own list.
        "will_run": [entry["object"] for entry in pending],
        "will_skip": [entry["object"] for entry in settled],
        "pending": pending,
        "skipped": settled,
    }


@router.post("/{session_id}/run-remaining")
def run_remaining(session_id: str, body: RunRemainingRequest = RunRemainingRequest()):
    """Run only the objects that still need it, leaving the migrated ones alone.

    This is how an object added later joins a session that has already run. The
    objects being skipped are passed to the pipeline as already complete, which is
    what keeps their staged extract and transform output — and so the record of
    what they left deferred — from being cleared by this run.

    Ordering still considers the whole selection, so an object added later is
    sequenced behind the parents it depends on even when those already loaded.
    """
    session = session_db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session["status"] == "running":
        raise HTTPException(status_code=409, detail="Session is already running")

    selection: list[str] = session["selected_objects"]
    plan = session_run_plan(session_id)
    requested = body.objects or plan["will_run"]

    outside = [name for name in requested if name not in selection]
    if outside:
        raise HTTPException(
            status_code=400,
            detail=(
                "Not in this session's selection: "
                f"{', '.join(sorted(outside))}. Add them first."
            ),
        )
    if not requested:
        raise HTTPException(
            status_code=409,
            detail="Every object in this session has already migrated. "
            "Add objects to the session, or use retry to run it again.",
        )

    config = _raw_config(session_id)
    # The pipeline reads this to drop names from the run while still ordering
    # against the full selection.
    config["completed_objects"] = [
        name for name in selection if name not in requested
    ]
    config.pop("skip_extraction", None)

    migration_manager.start_migration(session_id, config)
    logger.info(
        "[session %s] incremental run of %d object(s): %s",
        session_id,
        len(requested),
        ", ".join(requested),
    )
    return {
        "session_id": session_id,
        "status": "running",
        "running": requested,
        "skipped": config["completed_objects"],
    }


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

    config = _raw_config(session_id)
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

def _raw_config(session_id: str) -> dict:
    """The stored config with real credentials, as the runner needs it.

    ``session_db.get_session`` masks secrets for the API response, so anything
    that starts a run has to read the row directly.
    """
    from services.external.db_client import db_client

    raw = db_client.fetch_one(
        "SELECT config FROM migration_sessions WHERE id = ?", (session_id,)
    )
    if not raw:
        raise HTTPException(status_code=404, detail="Session config not found")
    return json.loads(raw[0])


_SECRET_KEYS = frozenset(
    {"password", "client_secret", "secret", "token", "api_key", "private_key"}
)


def _same_endpoint(stored: dict, candidate: dict) -> bool:
    """Whether two credential sets address the same account on the same host.

    Only the identifying fields are compared. Secrets are excluded because the
    stored copy may be masked or may simply have been rotated since, neither of
    which changes which instance the session was created against — and a match
    here only proposes a connection, it never selects one on its own.
    """
    if not stored or not candidate:
        return False
    keys = {
        key
        for key in (*stored, *candidate)
        if key.lower() not in _SECRET_KEYS
    }
    if not keys:
        return False

    def normalise(value: object) -> str:
        return str(value or "").strip().rstrip("/").lower()

    return all(normalise(stored.get(key)) == normalise(candidate.get(key)) for key in keys)


def _credentials_missing(config: dict) -> bool:
    """Whether this config still carries the placeholders a restore leaves behind."""
    for side in ("source_credentials", "destination_credentials"):
        values = config.get(side) or {}
        if not values:
            return True
        if any(value == "***" for value in values.values()):
            return True
    return False


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
