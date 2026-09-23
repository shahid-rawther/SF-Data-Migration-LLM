"""The migration-order advisor.

The ancestor graph in ``services.internal.hierarchy_service`` already yields a
correct parent-first order: a topological sort is exact, and a model cannot
improve on it. What a sort cannot produce is the reading — why an object sits
where it does, which parents nothing is migrating into, which of two objects in a
cycle to break first, and what a person should do before starting.

So this agent is asked for a sequence and its reasoning, and everything it
returns is checked back against the graph. Every step must name an object that is
actually in the graph, an object placed above one of its own parents is reported
as a conflict, and anything the model left out is appended so the plan still
covers the selection. The result is advice next to the deterministic order, never
in place of it: the sequence a migration runs in stays the one the loader
computes, and which object to migrate when stays a person's decision.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from common.logger import logger

from agents.bedrock_client import (
    AgentClient,
    AgentError,
    ToolSpec,
    build_client,
    run_tool_loop,
)
from agents.config import PURPOSE_RELATIONSHIP, AgentSettings, load_settings

SUBMIT_TOOL = "submit_migration_order"

# Keeps one request's prompt to a predictable size on a wide graph.
MAX_PROMPT_NODES = 80

# The whole answer — a sequence over the entire graph, plus the notes and the
# decisions — arrives in a single call, and a call truncated mid-submission
# returns nothing while still being billed. So this agent is given headroom above
# the general per-call cap rather than being allowed to run out of room.
MIN_OUTPUT_TOKENS = 14000

SYSTEM_PROMPT = """\
You advise on the order to migrate Salesforce objects in.

The destination assigns its own record ids. A lookup field can only be filled \
once the record it points at exists, so an object must be loaded after every \
object it depends on. You are given the dependency graph for the objects a person \
has chosen to migrate, together with the parents above them.

In the graph, `depends_on` lists the objects a record points at. A `required` \
dependency is one Salesforce will not accept a record without, so the parent has \
to exist first. A `mapped` one is a lookup some mapping actually fills, so the \
parent's records have to be loaded for its ids to resolve. A dependency that is \
neither constrains nothing: the load succeeds with the lookup blank.

Three flags matter. `required_chain` marks an object Salesforce will not let the \
load skip. `needed` marks one this migration as configured cannot load the \
selection without — required or mapped. Everything else is merely above the \
selection. `has_mapping` false means nothing is migrating into that object, so \
either its records already exist in the destination — which is usual for \
configuration objects like profiles, record types and price books — or the \
lookups pointing at it stay empty.

Your job:
- Give the order to load the objects in, as a plain list of names. Every parent \
must come before its children.
- Explain only where explaining helps: the objects a person would ask about, the \
ones a required lookup forces, the ones in a cycle. Do not annotate all of them.
- Be clear about how much of this is obligation. If the chosen objects have no \
required parents, say so plainly in the summary rather than implying the whole \
graph has to be migrated.
- Call out what needs a decision before starting: required parents with no \
mapping, groups of objects that reference each other, and lookups that accept \
many objects.

Rules:
- Use object names exactly as they appear in the graph. Never invent one or \
include an object that is not there.
- The order must list every object in the graph exactly once.
- Where two objects are independent of each other, prefer loading the one more \
objects depend on first.
- For a group that references itself, say which member to load first and which \
lookup to leave empty on the first pass and fill afterwards.
- Write for a migration engineer who cannot see this prompt. Be specific and \
brief. Keep the whole submission under 400 words — a long answer is a worse \
answer here.

Call submit_migration_order exactly once.
"""

SUBMIT_TOOL_SPEC = ToolSpec(
    name=SUBMIT_TOOL,
    description="Submit the migration order and its reasoning. Call this exactly once.",
    input_schema={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "Two or three sentences on the shape of this migration: what "
                    "has to go first and what the real constraint is."
                ),
            },
            "order": {
                "type": "array",
                "description": (
                    "Every object in the graph, in load order, as exact names. "
                    "Names only — the reasoning goes in notes."
                ),
                "items": {"type": "string"},
            },
            "notes": {
                "type": "array",
                "description": (
                    "Why particular objects sit where they do. Only the ones "
                    "worth a comment, at most ten."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "object": {
                            "type": "string",
                            "description": "Exact object name from the graph.",
                        },
                        "reason": {
                            "type": "string",
                            "description": (
                                "One short clause: the dependency that forces the "
                                "position, or that it is optional and can be skipped."
                            ),
                        },
                    },
                    "required": ["object", "reason"],
                },
            },
            "decisions": {
                "type": "array",
                "description": "What a person should settle before the run starts.",
                "items": {
                    "type": "object",
                    "properties": {
                        "object": {"type": "string"},
                        "field": {"type": "string"},
                        "issue": {
                            "type": "string",
                            "description": "What is unresolved, in one sentence.",
                        },
                        "recommendation": {
                            "type": "string",
                            "description": "What you would do about it.",
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["blocker", "warning", "info"],
                        },
                    },
                    "required": ["issue", "recommendation"],
                },
            },
        },
        "required": ["order"],
    },
)


@dataclass
class OrderStep:
    object: str
    reason: str = ""
    # Set when the model placed this object above something it depends on.
    conflict: str = ""

    def to_dict(self) -> dict:
        return {
            "object": self.object,
            "reason": self.reason,
            "conflict": self.conflict,
        }


@dataclass
class OrderSuggestion:
    selected_objects: list[str] = field(default_factory=list)
    # The order the loader would compute from the graph, for comparison.
    computed_order: list[str] = field(default_factory=list)
    steps: list[OrderStep] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    summary: str = ""
    # Objects the model omitted, appended to the steps in dependency order.
    appended: list[str] = field(default_factory=list)
    # Steps the model named that are not in the graph, and were dropped.
    dropped: list[dict] = field(default_factory=list)
    # True when the suggested sequence loads every parent before its children.
    valid: bool = True
    # True when it is the same sequence the loader would compute.
    matches_computed: bool = False
    model_invoked: bool = False
    usage: dict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "selected_objects": self.selected_objects,
            "computed_order": self.computed_order,
            "steps": [s.to_dict() for s in self.steps],
            "decisions": self.decisions,
            "summary": self.summary,
            "appended": self.appended,
            "dropped": self.dropped,
            "valid": self.valid,
            "matches_computed": self.matches_computed,
            "model_invoked": self.model_invoked,
            "usage": self.usage,
            "error": self.error,
        }


def _build_prompt(graph: dict) -> str:
    """Render the graph for the model, trimmed to the parts that carry order."""
    nodes = []
    for node in (graph.get("nodes") or [])[:MAX_PROMPT_NODES]:
        entry: dict = {
            "object": node["object"],
            "level": node["level"],
            "selected": node["selected"],
            "has_mapping": node["has_mapping"],
            "depends_on": [
                {
                    "object": d["object"],
                    "field": d["field"],
                    **({"required": True} if d.get("required") else {}),
                    **({"mapped": True} if d.get("mapped") else {}),
                }
                for d in node.get("depends_on") or []
            ],
            "needed_by_count": node.get("needed_by_count", 0),
        }
        if node.get("source_tables"):
            entry["source_tables"] = node["source_tables"]
        if node.get("self_reference_fields"):
            entry["self_reference_fields"] = node["self_reference_fields"]
        if node.get("in_cycle"):
            entry["in_cycle"] = True
        if node.get("required_chain"):
            entry["required_chain"] = True
        if node.get("needed"):
            entry["needed"] = True
        nodes.append(entry)

    payload = {
        "objects_chosen_to_migrate": graph.get("selected_objects") or [],
        "objects_salesforce_will_not_let_the_load_skip": graph.get("required_objects") or [],
        "objects_this_migration_as_configured_needs": graph.get("needed_objects") or [],
        "graph": nodes,
        "groups_that_reference_each_other": graph.get("cycles") or [],
        "required_lookups_with_no_parent_in_scope": graph.get("required_gaps") or [],
        "lookups_that_accept_many_objects": graph.get("polymorphic_choices") or [],
        "objects_with_no_mapping": graph.get("objects_without_mapping") or [],
    }
    if graph.get("truncated"):
        payload["note"] = (
            f"The graph is incomplete — {graph.get('truncation_reason')}. Say so "
            "if it affects your advice."
        )

    return (
        "Dependency graph for this migration. Level 0 is the top: an object at a "
        "higher level depends on objects at lower ones.\n\n"
        f"{json.dumps(payload, indent=1)}\n\n"
        "Give me the order to migrate these in, and what to settle first."
    )


def _validate(
    raw_order: list, graph: dict, raw_notes: list | None = None
) -> tuple[list[OrderStep], list[str], list[dict], bool]:
    """Check the sequence against the graph it was derived from.

    Returns the accepted steps, the objects that had to be appended, the ones
    dropped, and whether the sequence loads every parent before its children.
    """
    known = {node["object"] for node in graph.get("nodes") or []}
    lookup = {name.lower(): name for name in known}

    notes: dict[str, str] = {}
    for item in raw_notes or []:
        if not isinstance(item, dict):
            continue
        match = lookup.get(str(item.get("object") or "").strip().lower())
        reason = str(item.get("reason") or "").strip()
        if match and reason:
            notes[match] = reason

    parents: dict[str, set[str]] = {name: set() for name in known}
    for edge in graph.get("edges") or []:
        child, parent = edge.get("child"), edge.get("parent")
        if child in known and parent in known and child != parent:
            parents[child].add(parent)
    # Members of a cycle cannot all follow their parents, so their mutual edges
    # are not held against the sequence.
    for group in graph.get("cycles") or []:
        members = set(group)
        for member in members:
            parents[member] = parents.get(member, set()) - members

    steps: list[OrderStep] = []
    dropped: list[dict] = []
    placed: set[str] = set()

    for entry in raw_order:
        # A model that ignores the schema and submits objects instead of names
        # is still understood, rather than losing the whole answer to it.
        name = (
            str(entry.get("object") or "").strip()
            if isinstance(entry, dict)
            else str(entry or "").strip()
        )
        match = lookup.get(name.lower(), "")
        if not match:
            dropped.append({"object": name, "reason": "not an object in this graph"})
            continue
        if match in placed:
            dropped.append({"object": match, "reason": "already placed earlier"})
            continue
        if isinstance(entry, dict) and entry.get("reason"):
            notes.setdefault(match, str(entry["reason"]).strip())

        outstanding = sorted(parents.get(match, set()) - placed)
        conflict = ""
        if outstanding:
            conflict = (
                f"{match} depends on {', '.join(outstanding[:4])}, which the "
                "suggestion places later"
            )
        placed.add(match)
        steps.append(
            OrderStep(object=match, reason=notes.get(match, ""), conflict=conflict)
        )

    # Anything the model forgot is added in the graph's own order, so the plan
    # still covers every object the run has to load.
    appended = [name for name in graph.get("order") or [] if name not in placed]
    for name in appended:
        steps.append(OrderStep(object=name, reason=notes.get(name, "")))

    return steps, appended, dropped, not any(s.conflict for s in steps)


def _clean_decisions(raw: list, graph: dict) -> list[dict]:
    known = {node["object"] for node in graph.get("nodes") or []}
    cleaned: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        issue = str(item.get("issue") or "").strip()
        recommendation = str(item.get("recommendation") or "").strip()
        if not (issue and recommendation):
            continue
        name = str(item.get("object") or "").strip()
        severity = str(item.get("severity") or "info").strip().lower()
        cleaned.append(
            {
                "object": name if name in known else "",
                "field": str(item.get("field") or "").strip(),
                "issue": issue,
                "recommendation": recommendation,
                "severity": severity
                if severity in ("blocker", "warning", "info")
                else "info",
            }
        )
    return cleaned


def suggest_order(
    graph: dict,
    *,
    settings: AgentSettings | None = None,
    client: AgentClient | None = None,
) -> OrderSuggestion:
    """Ask the model to sequence an ancestor graph and explain the sequence."""
    settings = settings or load_settings()
    computed = list(graph.get("order") or [])
    suggestion = OrderSuggestion(
        selected_objects=list(graph.get("selected_objects") or []),
        computed_order=computed,
    )

    if not graph.get("nodes"):
        suggestion.error = "There is nothing to order — the graph is empty."
        return suggestion

    if settings.max_tokens_per_call < MIN_OUTPUT_TOKENS:
        settings = replace(settings, max_tokens_per_call=MIN_OUTPUT_TOKENS)

    try:
        client = client or build_client(PURPOSE_RELATIONSHIP, settings)
        result = run_tool_loop(
            client,
            system=SYSTEM_PROMPT,
            prompt=_build_prompt(graph),
            tools=[SUBMIT_TOOL_SPEC],
            dispatch=_no_tools,
            submit_tool=SUBMIT_TOOL,
        )
    except AgentError as exc:
        suggestion.error = str(exc)
        logger.warning("[order-advisor] failed: %s", exc)
        return suggestion

    suggestion.model_invoked = True
    suggestion.usage = result.usage
    submission = result.submission or {}
    raw_order = submission.get("order")
    if not isinstance(raw_order, list):
        suggestion.error = (
            "The model ran out of room before submitting an order."
            if result.stop_reason == "max_tokens"
            else "The model finished without submitting an order."
        )
        suggestion.summary = result.final_text
        return suggestion

    (
        suggestion.steps,
        suggestion.appended,
        suggestion.dropped,
        suggestion.valid,
    ) = _validate(raw_order, graph, submission.get("notes"))
    suggestion.decisions = _clean_decisions(submission.get("decisions"), graph)
    suggestion.summary = str(submission.get("summary") or result.final_text or "")
    suggestion.matches_computed = [s.object for s in suggestion.steps] == computed

    logger.info(
        "[order-advisor] %d step(s), %d appended, %d dropped, %s",
        len(suggestion.steps),
        len(suggestion.appended),
        len(suggestion.dropped),
        "consistent with the graph" if suggestion.valid else "CONFLICTS with the graph",
    )
    return suggestion


def _no_tools(name: str, arguments: dict):
    raise AgentError(
        f"'{name}' is not available; call {SUBMIT_TOOL} with the order you have."
    )
