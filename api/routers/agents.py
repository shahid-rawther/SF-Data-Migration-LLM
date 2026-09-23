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


class SuggestOrderRequest(BaseModel):
    # Salesforce objects the migration will write into.
    selected_objects: list[str] = []
    destination_credentials: dict = {}
    destination_connection_id: str | None = None
    # Narrows the mappings consulted to one source pack; blank means all.
    source_type: str | None = None
    max_depth: int = 4
    max_nodes: int = 60


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


@router.post("/suggest-order")
def suggest_order(body: SuggestOrderRequest):
    """Advise on the order to migrate a selection of Salesforce objects in.

    The graph and its parent-first order are computed deterministically; the
    model reads them and returns a sequence with its reasoning, which is checked
    back against the graph. Both orders are returned, so a suggestion that
    conflicts with the dependencies is visible as such rather than followed.
    """
    from agents import order_advisor
    from services.internal import hierarchy_service

    if not body.selected_objects:
        raise HTTPException(status_code=400, detail="selected_objects is required")

    graph = hierarchy_service.build_hierarchy(
        body.selected_objects,
        _resolve_credentials(body.destination_credentials, body.destination_connection_id),
        source_type=body.source_type,
        max_depth=body.max_depth,
        max_nodes=body.max_nodes,
    )
    return {
        "graph": graph,
        "suggestion": order_advisor.suggest_order(graph).to_dict(),
    }


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


def _attempt_started_at(session_id: str) -> str:
    """When the session's most recent migration attempt began.

    ``started_at`` is restamped every time a migration is launched, so it is the
    boundary between what the agents said about this attempt and what they said
    about a mapping that has since been changed. Blank for a session that has
    never run, which callers read as "no boundary, show everything".
    """
    from api import session_db

    session = session_db.get_session(session_id)
    return (session or {}).get("started_at") or ""


@router.get("/changes")
def list_changes(
    run_id: str | None = None,
    session_id: str | None = None,
    mapping_name: str | None = None,
    include_reverted: bool = True,
    scope: str = "all",
):
    """Every mapping change an agent made, newest first.

    ``scope=latest`` narrows to the current migration attempt. Without it a
    session accumulates the notices of every attempt in one undated list, where
    a finding already fixed reads as though it were still true.
    """
    from agents import audit

    since = (
        _attempt_started_at(session_id)
        if scope == "latest" and session_id
        else ""
    )
    return audit.list_changes(
        run_id=run_id,
        session_id=session_id,
        mapping_name=mapping_name,
        include_reverted=include_reverted,
        since=since or None,
    )


@router.delete("/changes/notices")
def discard_superseded_notices(session_id: str):
    """Drop the session's notices from attempts before the current one."""
    from agents import audit

    started_at = _attempt_started_at(session_id)
    if not started_at:
        return {"session_id": session_id, "discarded": 0}
    return {
        "session_id": session_id,
        "discarded": audit.discard_notices(session_id, started_at),
    }


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
