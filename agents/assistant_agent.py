"""The conversational repair assistant.

A person asks about a session's failures, or tells the agent what to change,
and the agent investigates with the same read-only and data tools the diagnosis
agent uses. It never writes a mapping itself: when a change is needed it calls
``propose_changes``, the resolutions are validated and applied to a draft to
show a before/after, and the result is stored as a pending proposal.

Applying a proposal is deterministic and does not involve the model. It is
re-validated against the mapping as it is now and refused if the mapping has
moved on since the preview, so what is applied is exactly what was shown.
"""
from __future__ import annotations

import json
import uuid

from common.logger import logger

from agents import audit, assistant_store, resolutions as resolution_rules, wiring
from agents.bedrock_client import AgentError, build_client, run_tool_loop
from agents.config import PURPOSE_RELATIONSHIP, AgentSettings, load_settings
from agents.context import MigrationContext
from agents.tools import ToolDispatcher, build_assistant_tools

HISTORY_LIMIT = 20

SYSTEM_PROMPT = """\
You are the migration assistant for one data-migration session (source system \
to Salesforce). The person you are talking to is a migration engineer. They ask \
why records failed to load and ask you to change field mappings to fix it.

How to work:

- Investigate before answering. get_diagnosis shows what has already been \
concluded; get_error_digest and sample_failed_rows show the actual rejected \
rows and the source rows behind them; profile_source_field sizes a data \
problem; describe_mapping and describe_destination_object show how a field is \
produced and what the destination accepts.
- Explain plainly: name the fields and values, say whether it is a data, \
mapping or destination-rule problem, and give options with trade-offs when \
there is more than one sensible fix.
- To change a mapping, call propose_changes. Nothing is written until the \
person clicks Apply. Read the result: fix any rejected resolution and propose \
again if needed. Offer the affected mapping in rerun so it can be re-run after \
applying. Never claim a change was made — say it is proposed.
- Resolution patterns: generic_rules (kind type_coercion) sets or clears a \
field from other source columns. Its transform_params are {"inputs": [...], \
"rules": [{"all": [{"field", "op", "value"}], "then": {"value": ...} or \
{"transform": "generic_default_if_empty", "from": <input>, "params": {}}}], \
"otherwise": {"value": ""}}; ops are equals, not_equals, in, not_in, \
contains, is_empty, is_not_empty, gt, lt, matches. source_field must list \
every input comma-separated, in the same order as inputs. \
generic_default_if_empty with several source fields picks the first \
non-empty one. value_map maps source values onto picklist values. \
remove_field stops sending a field. lookup_reference resolves ids from a \
parent mapping.
- If the fix is outside the mapping (a destination validation rule, bad \
source data, permissions), say so and say who needs to do what.
- Keep replies short and concrete. Use plain text and short lists.\
"""


# ── session context ────────────────────────────────────────────────────────────


def _session_config(session_id: str) -> dict:
    from services.external.db_client import db_client

    row = db_client.fetch_one(
        "SELECT config, source_type FROM migration_sessions WHERE id = ?", (session_id,)
    )
    if not row:
        raise KeyError(f"Session '{session_id}' not found")
    config = json.loads(row[0] or "{}")
    config.setdefault("source_type", row[1] or "servicenow")
    return config


def _credentials(config: dict, side: str) -> dict:
    """A linked saved connection wins over the credentials copied at creation."""
    from api import connections_db

    connection_id = config.get(f"{side}_connection_id")
    if connection_id:
        connection = connections_db.get_connection(connection_id)
        if connection and connection.get("credentials"):
            return connection["credentials"]
    return config.get(f"{side}_credentials") or {}


def load_session_context(session_id: str) -> MigrationContext:
    config = _session_config(session_id)
    return MigrationContext.load(
        source_type=config.get("source_type") or "servicenow",
        mapping_names=list(config.get("selected_objects") or []),
        source_credentials=_credentials(config, "source"),
        destination_credentials=_credentials(config, "destination"),
    )


def _session_status(session_id: str) -> str:
    from api import session_db

    session = session_db.get_session(session_id) or {}
    return session.get("status") or ""


# ── proposals ──────────────────────────────────────────────────────────────────


def _validate_items(
    context: MigrationContext, raw: list
) -> tuple[list[dict], list[dict]]:
    """Split raw resolution dicts into admissible ones and rejections."""
    accepted: list[dict] = []
    rejected: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        outcome = resolution_rules.validate(context, [item])
        if outcome.accepted:
            accepted.append(item)
        else:
            rejected.extend(outcome.rejected)
    return accepted, rejected


def _preview(context: MigrationContext, items: list[dict]):
    """Apply to a draft only. Returns (preview rows, unapplied, change records)."""
    outcome = resolution_rules.validate(context, items)
    draft = context.draft()
    changes, unapplied = wiring.apply_resolutions(
        draft, outcome.accepted, agent=wiring.AGENT_ASSISTANT
    )
    rows = [
        {
            "mapping": change.mapping_name,
            "field": change.field_name,
            "target": change.target,
            "kind": change.kind,
            "rationale": change.rationale,
            "applied": change.applied,
            "before": change.previous_value,
            "after": change.new_value,
        }
        for change in changes
    ]
    return rows, unapplied, changes, draft


def build_proposal(
    context: MigrationContext,
    session_id: str,
    summary: str,
    raw_resolutions: list,
    rerun: list | None = None,
    require_changes: bool = False,
    origin: str = "",
    diagnosis_id: int | None = None,
) -> dict:
    """Validate, preview and store a proposal. Nothing is written to a mapping.

    ``require_changes`` refuses a proposal that would only re-run: a fix whose
    every change was rejected must not be offered as though it were a fix."""
    accepted, rejected = _validate_items(context, raw_resolutions)
    rerun_names = [name for name in (rerun or []) if name in context.mapping_names]
    rerun_only_ok = bool(rerun_names) and not require_changes
    if not accepted and not rerun_only_ok:
        return {
            "proposal_id": None,
            "accepted": 0,
            "rejected": rejected,
            "note": "Nothing admissible to propose; fix the rejected resolutions.",
        }

    preview, unapplied, _, _ = _preview(context, accepted) if accepted else ([], [], [], {})
    if unapplied:
        failed = {
            (item.get("mapping", ""), item.get("field", ""), item.get("kind", ""))
            for item in unapplied
        }
        rejected.extend({"resolution": item, "reason": item.get("reason", "")} for item in unapplied)
        accepted = [
            item for item in accepted
            if (item.get("mapping", ""), item.get("field", "") or "", item.get("kind", ""))
            not in failed
        ]
        if accepted:
            preview, _, _, _ = _preview(context, accepted)
        elif not rerun_only_ok:
            return {
                "proposal_id": None,
                "accepted": 0,
                "rejected": rejected,
                "note": "Nothing could be applied; fix the rejected resolutions.",
            }
        else:
            preview = []
    mappings = sorted({str(item.get("mapping") or "") for item in accepted if item.get("mapping")})
    proposal_id = assistant_store.save_proposal(
        session_id=session_id,
        mapping_name=", ".join(mappings) or ", ".join(rerun_names),
        summary=summary,
        resolutions=accepted,
        preview=preview,
        rerun=rerun_names,
        origin=origin,
        diagnosis_id=diagnosis_id,
    )
    return {
        "proposal_id": proposal_id,
        "accepted": len(accepted),
        "rejected": rejected,
        "preview": preview,
        "rerun": rerun_names,
        "note": "Stored as a pending proposal. The person must click Apply.",
    }


def propose_from_diagnosis(diagnosis_id: int, suggestion_index: int) -> dict:
    """Turn one of a diagnosis's suggestions into a pending proposal."""
    diagnosis = assistant_store.get_diagnosis(diagnosis_id)
    if not diagnosis:
        raise KeyError(f"No diagnosis with id {diagnosis_id}")
    suggestions = diagnosis["suggestions"]
    if not 0 <= suggestion_index < len(suggestions):
        raise ValueError(f"Diagnosis {diagnosis_id} has no suggestion {suggestion_index}")
    suggestion = suggestions[suggestion_index]
    if not suggestion.get("resolutions"):
        raise ValueError("This suggestion needs a person to act on it; it has no mapping change")

    session_id = diagnosis["session_id"]
    context = load_session_context(session_id)
    result = build_proposal(
        context,
        session_id,
        summary=suggestion.get("title", ""),
        raw_resolutions=suggestion["resolutions"],
        rerun=[diagnosis["mapping_name"]],
        require_changes=True,
        origin=assistant_store.ORIGIN_DIAGNOSIS,
        diagnosis_id=diagnosis_id,
    )
    if result.get("proposal_id"):
        assistant_store.add_message(
            session_id,
            "assistant",
            f"Proposed: {suggestion.get('title', '')}. Review the change below and "
            "click Apply to write it to the mapping.",
            proposal_id=result["proposal_id"],
        )
    return result


def apply_proposal(proposal_id: int) -> dict:
    """Write a pending proposal to its mappings, journalled and revertible."""
    proposal = assistant_store.get_proposal(proposal_id)
    if not proposal:
        raise KeyError(f"No proposal with id {proposal_id}")
    if proposal["status"] != assistant_store.PROPOSAL_PENDING:
        raise ValueError(f"Proposal {proposal_id} is already {proposal['status']}")
    session_id = proposal["session_id"]
    if _session_status(session_id) == "running":
        raise ValueError("The session is running; apply this once the run has settled")

    run_id = uuid.uuid4().hex
    changes: list = []
    change_ids: list[int] = []
    if proposal["resolutions"]:
        context = load_session_context(session_id)
        accepted, rejected = _validate_items(context, proposal["resolutions"])
        if rejected:
            reason = "; ".join(item.get("reason", "") for item in rejected)
            assistant_store.decide_proposal(
                proposal_id, assistant_store.PROPOSAL_FAILED, error=reason
            )
            raise ValueError(f"The proposal no longer validates: {reason}")

        preview, unapplied, changes, draft = _preview(context, accepted)
        if unapplied:
            reason = "; ".join(item.get("reason", "") for item in unapplied)
            assistant_store.decide_proposal(
                proposal_id, assistant_store.PROPOSAL_FAILED, error=reason
            )
            raise ValueError(f"The proposal could not be applied: {reason}")
        if [row["before"] for row in preview] != [row["before"] for row in proposal["preview"]]:
            reason = (
                "the mapping has changed since this was proposed; ask the assistant "
                "to propose it again"
            )
            assistant_store.decide_proposal(
                proposal_id, assistant_store.PROPOSAL_FAILED, error=reason
            )
            raise ValueError(f"Refused: {reason}")

        writes = [change for change in changes if change.applied]
        touched = sorted({change.mapping_name for change in writes})
        if touched:
            wiring.persist(draft, touched)
        change_ids = audit.record_changes(run_id, changes, session_id=session_id)
        logger.info(
            "[agent] applied assistant proposal %d: %d change(s) on %s",
            proposal_id,
            len(writes),
            ", ".join(touched) or "no mapping",
        )

    assistant_store.decide_proposal(proposal_id, assistant_store.PROPOSAL_APPLIED, run_id=run_id)
    fields = ", ".join(
        f"{change.mapping_name}.{change.field_name}" for change in changes if change.field_name
    )
    assistant_store.add_message(
        session_id,
        "assistant",
        (f"Applied the change to {fields}. " if fields else "Nothing to write. ")
        + (
            f"Re-run {', '.join(proposal['rerun'])} to load the rejected rows again."
            if proposal["rerun"]
            else ""
        ),
    )
    return {
        "proposal_id": proposal_id,
        "run_id": run_id,
        "changes": [change.to_dict() for change in changes],
        "change_ids": change_ids,
        "rerun": proposal["rerun"],
    }


def reject_proposal(proposal_id: int) -> dict:
    proposal = assistant_store.get_proposal(proposal_id)
    if not proposal:
        raise KeyError(f"No proposal with id {proposal_id}")
    if proposal["status"] != assistant_store.PROPOSAL_PENDING:
        raise ValueError(f"Proposal {proposal_id} is already {proposal['status']}")
    assistant_store.decide_proposal(proposal_id, assistant_store.PROPOSAL_REJECTED)
    return {"proposal_id": proposal_id, "status": assistant_store.PROPOSAL_REJECTED}


# ── conversation ───────────────────────────────────────────────────────────────


def _digests(context: MigrationContext) -> dict:
    """The current load errors of every mapping in the session that has any."""
    from agents.repair_agent import build_digest

    digests: dict[str, dict] = {}
    for name in context.mapping_names:
        try:
            digest = build_digest(context, name)
        except Exception as exc:  # noqa: BLE001 — one unreadable object must not end the turn
            logger.warning("[agent] could not read errors for %s: %s", name, exc)
            continue
        if not digest.is_empty:
            digests[name] = digest.to_dict()
    return digests


def _brief_diagnoses(session_id: str) -> list[dict]:
    return [
        {
            "id": entry["id"],
            "mapping": entry["mapping_name"],
            "analysis": entry["analysis"],
            "evidence": entry["evidence"],
            "suggestions": entry["suggestions"],
            "created_at": entry["created_at"],
        }
        for entry in assistant_store.list_diagnoses(session_id)
    ]


def chat(session_id: str, message: str, settings: AgentSettings | None = None) -> dict:
    """One conversational turn. Returns the reply and any proposal it stored."""
    message = (message or "").strip()
    if not message:
        raise ValueError("message is required")
    settings = settings or load_settings()

    history = [
        {"role": entry["role"], "content": entry["content"]}
        for entry in assistant_store.list_messages(session_id, limit=HISTORY_LIMIT)
    ]
    assistant_store.add_message(session_id, "user", message)

    context = load_session_context(session_id)
    proposals: list[dict] = []

    def on_propose_changes(arguments: dict) -> dict:
        result = build_proposal(
            context,
            session_id,
            summary=str(arguments.get("summary") or ""),
            raw_resolutions=arguments.get("resolutions") or [],
            rerun=arguments.get("rerun") or [],
            origin=assistant_store.ORIGIN_CHAT,
        )
        if result.get("proposal_id"):
            proposals.append(result)
        return result

    def on_propose_rerun(arguments: dict) -> dict:
        result = build_proposal(
            context,
            session_id,
            summary=str(arguments.get("reason") or "Re-run"),
            raw_resolutions=[],
            rerun=arguments.get("mappings") or [],
            origin=assistant_store.ORIGIN_CHAT,
        )
        if result.get("proposal_id"):
            proposals.append(result)
        return result

    dispatcher = ToolDispatcher(
        context,
        digest=_digests(context),
        diagnoses=_brief_diagnoses(session_id),
        on_propose_changes=on_propose_changes,
        on_propose_rerun=on_propose_rerun,
    )
    session_brief = {
        "session_id": session_id,
        "source_type": context.source_type,
        "status": _session_status(session_id),
        "mappings": {
            name: {
                "destination_object": context.destination_object(name),
                "source_table": context.driving_table(name),
            }
            for name in context.mapping_names
        },
    }
    prompt = f"Session:\n{json.dumps(session_brief, indent=2)}\n\nThe person says:\n{message}"

    error = ""
    usage: dict = {}
    try:
        loop = run_tool_loop(
            build_client(PURPOSE_RELATIONSHIP, settings),
            system=SYSTEM_PROMPT,
            prompt=prompt,
            tools=build_assistant_tools(),
            dispatch=dispatcher,
            history=history,
        )
        usage = loop.usage
        reply = loop.final_text.strip()
        if not reply:
            reply = (
                "I proposed the change below." if proposals
                else "I could not reach a conclusion in the steps allowed. Try a narrower question."
            )
    except AgentError as exc:
        logger.warning("[agent] assistant turn failed: %s", exc)
        error = str(exc)
        reply = f"The agent could not be reached: {exc}"

    proposal_id = proposals[-1]["proposal_id"] if proposals else None
    message_id = assistant_store.add_message(session_id, "assistant", reply, proposal_id=proposal_id)
    return {
        "message_id": message_id,
        "reply": reply,
        "proposal": assistant_store.get_proposal(proposal_id) if proposal_id else None,
        "error": error,
        "usage": usage,
    }


def conversation(session_id: str) -> dict:
    """Everything the panel shows: messages, the proposals they refer to, diagnoses."""
    messages = assistant_store.list_messages(session_id)
    proposals = {entry["id"]: entry for entry in assistant_store.list_proposals(session_id)}
    return {
        "messages": messages,
        "proposals": [proposals[key] for key in sorted(proposals)],
        "diagnoses": assistant_store.list_diagnoses(session_id),
    }


# ── resolution history ─────────────────────────────────────────────────────────

SOURCE_AUTO_REPAIR = "auto_repair"
SOURCE_PROPOSAL = "proposal"


def resolution_history(session_id: str) -> list[dict]:
    """Every mapping change an agent wrote in a session, one group per agent run.

    Auto-repair and the repair agent share a run id when both act on the same
    failure, so a group is a run *and* an agent. A proposal's changes are
    journalled under the run id the proposal records, which is how an applied
    proposal is told apart from a change an agent made on its own.
    """
    proposals = assistant_store.applied_proposals_by_run(session_id)
    groups: dict[tuple[str, str], dict] = {}
    for change in audit.list_changes(session_id=session_id, include_reverted=True):
        if change.get("kind") == wiring.KIND_NOTICE or not change.get("applied"):
            continue
        run_id = change.get("run_id") or ""
        agent = change.get("agent") or ""
        group = groups.get((run_id, agent))
        if group is None:
            proposal = proposals.get(run_id) if agent == wiring.AGENT_ASSISTANT else None
            group = groups[(run_id, agent)] = {
                "run_id": run_id,
                "source": SOURCE_PROPOSAL if agent == wiring.AGENT_ASSISTANT else agent,
                "origin": (proposal or {}).get("origin") or "",
                "proposal": (
                    {
                        "id": proposal["id"],
                        "summary": proposal["summary"],
                        "rerun": proposal["rerun"],
                        "diagnosis_id": proposal.get("diagnosis_id"),
                    }
                    if proposal else None
                ),
                "applied_at": "",
                "mappings": [],
                "changes": [],
            }
        group["changes"].append(change)
        mapping = change.get("mapping_name") or ""
        if mapping and mapping not in group["mappings"]:
            group["mappings"].append(mapping)
        applied_at = change.get("applied_at") or ""
        if applied_at > group["applied_at"]:
            group["applied_at"] = applied_at
    return sorted(groups.values(), key=lambda group: group["applied_at"], reverse=True)
