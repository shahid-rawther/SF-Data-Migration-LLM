"""Agent router — relationship resolution, post-load repair and the change log.

Every write the agents make is reachable and reversible from here: the change log
lists what was applied, with the value it replaced, and a single change or a whole
run can be reverted without the model being involved.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api import connections_db

router = APIRouter(prefix="/mapping-agent", tags=["agents"])


def _resolve_credentials(credentials: dict | None, connection_id: str | None) -> dict:
    if connection_id:
        connection = connections_db.get_connection(connection_id)
        if not connection:
            raise HTTPException(status_code=404, detail="Connection not found")
        return connection.get("credentials", {})
    return credentials or {}


class AnalyzeRequest(BaseModel):
    source_type: str = "servicenow"
    selected_objects: list[str] = []
    source_credentials: dict = {}
    source_connection_id: str | None = None
    destination_credentials: dict = {}
    destination_connection_id: str | None = None
    session_id: str = ""
    # "preview" computes and returns the resolution without writing it.
    autonomy: str | None = None


class RepairRequest(BaseModel):
    source_type: str = "servicenow"
    mapping_name: str
    error_messages: list[str] = []
    related_mappings: list[str] = []
    source_credentials: dict = {}
    source_connection_id: str | None = None
    destination_credentials: dict = {}
    destination_connection_id: str | None = None
    session_id: str = ""
    autonomy: str | None = None


class PreflightRequest(BaseModel):
    source_type: str = "servicenow"
    selected_objects: list[str] = []
    source_credentials: dict = {}
    source_connection_id: str | None = None
    destination_credentials: dict = {}
    destination_connection_id: str | None = None


@router.get("/status")
def status(probe: bool = False):
    """Configuration of the agent layer, optionally with a live model round trip."""
    from agents.bedrock_client import AgentError, build_client
    from agents.config import load_settings

    settings = load_settings()
    payload = {"settings": settings.redacted()}
    if probe:
        try:
            payload["probe"] = build_client(settings=settings).probe()
        except AgentError as exc:
            payload["probe"] = {"ok": False, "error": str(exc)}
    return payload


@router.post("/analyze-relationships")
def analyze_relationships(body: AnalyzeRequest):
    """Resolve relationship edge cases and, unless previewing, wire them in."""
    from agents import relationship_agent

    if not body.selected_objects:
        raise HTTPException(status_code=400, detail="selected_objects is required")

    report = relationship_agent.analyze(
        source_type=body.source_type,
        mapping_names=body.selected_objects,
        source_credentials=_resolve_credentials(
            body.source_credentials, body.source_connection_id
        ),
        destination_credentials=_resolve_credentials(
            body.destination_credentials, body.destination_connection_id
        ),
        session_id=body.session_id,
        autonomy=body.autonomy,
    )
    return report.to_dict()


@router.post("/plan")
def plan(body: AnalyzeRequest):
    """The same resolution, computed but never written. Safe to call repeatedly."""
    from agents import relationship_agent
    from agents.config import AUTONOMY_PREVIEW

    if not body.selected_objects:
        raise HTTPException(status_code=400, detail="selected_objects is required")

    report = relationship_agent.analyze(
        source_type=body.source_type,
        mapping_names=body.selected_objects,
        source_credentials=_resolve_credentials(
            body.source_credentials, body.source_connection_id
        ),
        destination_credentials=_resolve_credentials(
            body.destination_credentials, body.destination_connection_id
        ),
        session_id=body.session_id,
        autonomy=AUTONOMY_PREVIEW,
    )
    return report.to_dict()


@router.post("/preflight")
def preflight_check(body: PreflightRequest):
    """The deterministic launch gate, without starting a migration."""
    from agents import preflight

    if not body.selected_objects:
        raise HTTPException(status_code=400, detail="selected_objects is required")

    result = preflight.run(
        source_type=body.source_type,
        mapping_names=body.selected_objects,
        source_credentials=_resolve_credentials(
            body.source_credentials, body.source_connection_id
        ),
        destination_credentials=_resolve_credentials(
            body.destination_credentials, body.destination_connection_id
        ),
    )
    return result.to_dict()


@router.post("/repair")
def repair(body: RepairRequest):
    """Diagnose one object's load failures and patch the field that caused them.

    Retrying the object is the pipeline's job; the response says whether a retry
    is worth running.
    """
    from agents import repair_agent

    if not body.mapping_name:
        raise HTTPException(status_code=400, detail="mapping_name is required")

    report = repair_agent.repair(
        source_type=body.source_type,
        mapping_name=body.mapping_name,
        error_messages=body.error_messages,
        source_credentials=_resolve_credentials(
            body.source_credentials, body.source_connection_id
        ),
        destination_credentials=_resolve_credentials(
            body.destination_credentials, body.destination_connection_id
        ),
        session_id=body.session_id,
        related_mappings=body.related_mappings,
        autonomy=body.autonomy,
    )
    return report.to_dict()


@router.get("/changes")
def list_changes(
    run_id: str | None = None,
    session_id: str | None = None,
    mapping_name: str | None = None,
    include_reverted: bool = True,
):
    """Every mapping change an agent made, newest first."""
    from agents import audit

    return audit.list_changes(
        run_id=run_id,
        session_id=session_id,
        mapping_name=mapping_name,
        include_reverted=include_reverted,
    )


@router.post("/changes/{change_id}/revert")
def revert_change(change_id: int):
    from agents import audit

    try:
        return audit.revert_change(change_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/revert")
def revert_run(run_id: str):
    from agents import audit

    result = audit.revert_run(run_id)
    if not result["count"]:
        raise HTTPException(
            status_code=404,
            detail=f"No outstanding changes for run '{run_id}'",
        )
    return result
