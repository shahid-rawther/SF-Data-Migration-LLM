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


# ── diagnosis, assistant conversation and proposals ───────────────────────────


class ChatRequest(BaseModel):
    session_id: str
    message: str


class DiagnoseRequest(BaseModel):
    session_id: str
    mapping_name: str


def _agent_errors(call):
    """Translate the assistant's domain errors into HTTP ones."""
    try:
        return call()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\"")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/diagnoses")
def list_diagnoses(session_id: str, scope: str = "latest"):
    """The newest diagnosis per failing object. ``scope=latest`` limits it to
    the current migration attempt."""
    from agents import assistant_store

    since = _attempt_started_at(session_id) if scope == "latest" else ""
    return assistant_store.list_diagnoses(session_id, since=since)


@router.post("/diagnoses")
def run_diagnosis(body: DiagnoseRequest):
    """Diagnose one object's current failures on demand."""
    from agents import assistant_agent, diagnosis_agent, repair_agent

    def call():
        context = assistant_agent.load_session_context(body.session_id)
        if body.mapping_name not in context.mapping_names:
            raise KeyError(f"'{body.mapping_name}' is not in this session")
        digest = repair_agent.build_digest(context, body.mapping_name)
        if digest.is_empty:
            raise ValueError(f"No load errors could be read for {body.mapping_name}")
        return diagnosis_agent.diagnose(
            context, body.mapping_name, digest, session_id=body.session_id
        ).to_dict()

    return _agent_errors(call)


@router.post("/diagnoses/{diagnosis_id}/propose")
def propose_from_diagnosis(diagnosis_id: int, suggestion: int = 0):
    """Turn one suggested solution into a pending proposal. No model call."""
    from agents import assistant_agent

    return _agent_errors(
        lambda: assistant_agent.propose_from_diagnosis(diagnosis_id, suggestion)
    )


@router.get("/chat")
def chat_history(session_id: str):
    """The conversation, the proposals it produced, and the diagnoses."""
    from agents import assistant_agent

    return assistant_agent.conversation(session_id)


@router.post("/chat")
def chat(body: ChatRequest):
    """One turn with the assistant. It may store a proposal; it never applies one."""
    from agents import assistant_agent

    return _agent_errors(lambda: assistant_agent.chat(body.session_id, body.message))


@router.delete("/chat")
def clear_chat(session_id: str):
    from agents import assistant_store

    assistant_store.clear_messages(session_id)
    return {"session_id": session_id, "cleared": True}


@router.post("/proposals/{proposal_id}/apply")
def apply_proposal(proposal_id: int):
    """Write a proposal to its mappings. Journalled, so each change can be reverted."""
    from agents import assistant_agent

    return _agent_errors(lambda: assistant_agent.apply_proposal(proposal_id))


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int):
    from agents import assistant_agent

    return _agent_errors(lambda: assistant_agent.reject_proposal(proposal_id))


@router.get("/resolutions")
def resolution_history(session_id: str):
    """Every mapping change an agent applied in the session, grouped by run and
    tagged by where it came from. Notices are left out: they change nothing."""
    from agents import assistant_agent

    return assistant_agent.resolution_history(session_id)


# ── data readiness ─────────────────────────────────────────────────────────────


class ReadinessRequest(BaseModel):
    source_type: str = "servicenow"
    selected_objects: list[str] = []
    source_credentials: dict = {}
    source_connection_id: str | None = None
    destination_credentials: dict = {}
    destination_connection_id: str | None = None


class ReadinessApplyRequest(BaseModel):
    source_credentials: dict = {}
    source_connection_id: str | None = None
    destination_credentials: dict = {}
    destination_connection_id: str | None = None


@router.post("/readiness")
def run_readiness(body: ReadinessRequest):
    """Check the source can fill every required destination field and parent.

    Stores the report with validated suggestions. Nothing is written to a
    mapping or to Salesforce until a suggestion is applied.
    """
    from agents import readiness_agent

    if not body.selected_objects:
        raise HTTPException(status_code=400, detail="selected_objects is required")
    result = readiness_agent.check(
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


@router.get("/readiness/{readiness_id}")
def get_readiness(readiness_id: int):
    from agents import readiness_store

    stored = readiness_store.get_readiness(readiness_id)
    if not stored:
        raise HTTPException(status_code=404, detail=f"No readiness check {readiness_id}")
    return stored


@router.post("/readiness/{readiness_id}/apply")
def apply_readiness_suggestion(
    readiness_id: int, body: ReadinessApplyRequest, gap: int = 0, suggestion: int = 0
):
    """Apply one suggestion a person approved: source writes, Salesforce writes, then mappings."""
    from agents import readiness_agent
    from agents.source_actions import SourceWriteError
    from services.internal.sf_inventory import SalesforceWriteError

    def call():
        return readiness_agent.apply_suggestion(
            readiness_id,
            gap,
            suggestion,
            _resolve_credentials(body.source_credentials, body.source_connection_id),
            _resolve_credentials(
                body.destination_credentials, body.destination_connection_id
            ),
        )

    try:
        return _agent_errors(call)
    except (SalesforceWriteError, SourceWriteError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/readiness/source-changes/{change_id}/revert")
def revert_readiness_source_change(change_id: int, body: ReadinessApplyRequest):
    """Empty again the source values a readiness suggestion wrote."""
    from agents import readiness_agent
    from agents.source_actions import SourceWriteError

    try:
        return _agent_errors(
            lambda: readiness_agent.revert_source_change(
                change_id,
                _resolve_credentials(body.source_credentials, body.source_connection_id),
            )
        )
    except SourceWriteError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/readiness/sf-changes/{change_id}/revert")
def revert_readiness_sf_change(change_id: int, body: ReadinessApplyRequest):
    """Delete a record a readiness suggestion created in Salesforce."""
    from agents import readiness_agent
    from services.internal.sf_inventory import SalesforceWriteError

    try:
        return _agent_errors(
            lambda: readiness_agent.revert_sf_change(
                change_id,
                _resolve_credentials(
                    body.destination_credentials, body.destination_connection_id
                ),
            )
        )
    except SalesforceWriteError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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
