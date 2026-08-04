"""The relationship agent.

It reads schema metadata and the draft mapping configs, decides what each
relationship edge case should become, and writes that decision into the configs
the existing ETL already obeys. What it does *not* do is move data or choose the
load order: the order is recomputed from the patched configs by the same
topological sort the loader uses, so the sequence a migration runs in is never
something a model asserted.

The run is short-circuited when the deterministic detector finds nothing to
decide, which is the common case for a mapping set that has already been
resolved once — no model call, no spend.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from common.logger import logger

from agents import audit, detector, resolutions as resolution_rules, wiring
from agents.bedrock_client import (
    AgentError,
    LoopResult,
    build_client,
    run_tool_loop,
)
from agents.config import (
    AUTONOMY_APPLY,
    AUTONOMY_PREVIEW,
    PURPOSE_RELATIONSHIP,
    AgentSettings,
    load_settings,
)
from agents.context import MigrationContext
from agents.detector import Finding
from agents.tools import SUBMIT_TOOL, ToolDispatcher, build_tools

SYSTEM_PROMPT = """\
You resolve relationship and datatype problems in a data migration whose \
destination assigns its own record ids.

How the pipeline works, which constrains every answer you give:

1. All selected source data is extracted and stored on our side first. Transform \
and load then run only against that staged copy.
2. When a parent object loads, the destination returns new ids and the pipeline \
writes source-id-to-destination-id pairs into a local id_mappings table.
3. A child's lookup field is therefore resolved at transform time by reading that \
table. A lookup that copies the raw source id instead will fail with MALFORMED_ID \
on every row.
4. Objects must load parents first. You do not choose the order: it is recomputed \
from the lookups you wire.

Your job:

- Call get_findings first. It is the pre-computed work list, with the parent \
mappings that could resolve each item and whether it blocks launch.
- Investigate with the other tools where a finding is ambiguous. Reference \
fields can point at several objects; check which one the source field actually \
holds and which mapping loads it.
- Resolve every blocking finding. Resolve non-blocking ones when the right answer \
is clear from the schema.
- Submit once, via submit_resolutions.

Rules you must follow:

- For a lookup that should resolve from a migrated parent, use lookup_reference \
and name the parent *mappings*. The id_mappings table name is derived from the \
mapping for you; you cannot and need not specify it.
- Use constant_reference only when every row should point at the same existing \
destination record, and only with a real 15 or 18 character record id. Never \
invent an id: if you do not have one, submit a notice instead.
- Use self_reference_guard for a lookup that points back at the same object.
- Use notice, changing nothing, whenever the correct answer needs a human \
decision — a parent that is not in the migration, an id you would have to guess, \
a cycle, or a business rule you cannot infer from the schema. A notice is the \
right answer more often than a wrong change.
- Every resolution needs a one-sentence rationale that names the evidence.
- Do not restate findings that are already correct.\
"""


@dataclass
class RelationshipReport:
    run_id: str
    autonomy: str
    applied: bool
    summary: str = ""
    findings: list[dict] = field(default_factory=list)
    resolutions: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    change_ids: list[int] = field(default_factory=list)
    load_order: list[str] = field(default_factory=list)
    dependency_edges: list[dict] = field(default_factory=list)
    remaining_blockers: list[dict] = field(default_factory=list)
    has_cycle: bool = False
    model_invoked: bool = False
    usage: dict = field(default_factory=dict)
    stop_reason: str = ""
    tool_calls: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "autonomy": self.autonomy,
            "applied": self.applied,
            "summary": self.summary,
            "findings": self.findings,
            "resolutions": self.resolutions,
            "rejected": self.rejected,
            "changes": self.changes,
            "change_ids": self.change_ids,
            "load_order": self.load_order,
            "dependency_edges": self.dependency_edges,
            "remaining_blockers": self.remaining_blockers,
            "has_cycle": self.has_cycle,
            "model_invoked": self.model_invoked,
            "usage": self.usage,
            "stop_reason": self.stop_reason,
            "tool_calls": self.tool_calls,
            "error": self.error,
        }


def _needs_agent(findings: list[Finding]) -> bool:
    """Whether anything in the findings actually calls for a decision."""
    decidable = {
        detector.RAW_REFERENCE,
        detector.UNMAPPED_REQUIRED_REFERENCE,
        detector.MISSING_PARENT,
        detector.LOOKUP_TABLE_MISMATCH,
        detector.CONSTANT_OVER_MIGRATED_PARENT,
        detector.SELF_REFERENCE,
        detector.TYPE_MISMATCH,
        detector.PICKLIST_UNMAPPED,
        detector.DEPENDENCY_CYCLE,
    }
    return any(finding.kind in decidable for finding in findings)


def _build_prompt(context: MigrationContext, findings: list[Finding]) -> str:
    """The opening message: the work list, plus just enough shape to act on it."""
    inventory = [
        {
            "mapping": name,
            "destination_object": context.destination_object(name),
            "source_table": context.driving_table(name),
        }
        for name in context.mapping_names
    ]
    work_list = [
        finding.to_dict()
        for finding in findings
        if finding.kind
        not in (detector.LOOKUP_WIRED, detector.JUNCTION_OBJECT, detector.NO_EXTERNAL_ID)
    ]
    return (
        f"Source system: {context.source_type}\n"
        f"Mappings in this migration:\n{json.dumps(inventory, indent=2)}\n\n"
        f"Findings that may need a decision ({len(work_list)}, of which "
        f"{sum(1 for f in findings if f.blocking)} block launch):\n"
        f"{json.dumps(work_list, indent=2)}\n\n"
        "Investigate what you need to, then call submit_resolutions once."
    )


def analyze(
    source_type: str,
    mapping_names: list[str],
    source_credentials: dict | None = None,
    destination_credentials: dict | None = None,
    session_id: str = "",
    autonomy: str | None = None,
    settings: AgentSettings | None = None,
    context: MigrationContext | None = None,
) -> RelationshipReport:
    """Resolve relationship edge cases across a set of mappings.

    With ``autonomy`` of ``preview`` the resolution is computed and returned but
    nothing is written, which is how the change log can be compared against
    hand-wiring before the agent is trusted to apply it.

    Passing a ``context`` reuses its cached describes and leaves it holding the
    post-change configs, so a caller that also runs the launch gate does not
    describe every destination object a second time.
    """
    settings = settings or load_settings()
    effective_autonomy = (autonomy or settings.autonomy).lower()
    if effective_autonomy not in (AUTONOMY_PREVIEW, AUTONOMY_APPLY):
        effective_autonomy = AUTONOMY_PREVIEW
    run_id = uuid.uuid4().hex

    context = context or MigrationContext.load(
        source_type=source_type,
        mapping_names=mapping_names,
        source_credentials=source_credentials,
        destination_credentials=destination_credentials,
    )
    report = RelationshipReport(
        run_id=run_id, autonomy=effective_autonomy, applied=False
    )
    if not context.mapping_names:
        report.error = "None of the selected objects has a mapping config"
        return report

    findings = detector.detect(context)
    report.findings = [finding.to_dict() for finding in findings]

    accepted: list = []
    if _needs_agent(findings):
        try:
            loop = _run_agent(context, findings, settings)
        except AgentError as exc:
            logger.error("[agent] relationship pass failed: %s", exc)
            report.error = str(exc)
            report.remaining_blockers = [
                finding.to_dict() for finding in detector.blocking(findings)
            ]
            return report
        report.model_invoked = True
        report.usage = loop.usage
        report.stop_reason = loop.stop_reason
        report.tool_calls = loop.tool_calls
        if loop.submission is None:
            report.error = (
                "The agent finished without submitting a resolution "
                f"(stop reason: {loop.stop_reason})"
            )
            report.summary = loop.final_text
            report.remaining_blockers = [
                finding.to_dict() for finding in detector.blocking(findings)
            ]
            return report

        summary, raw = resolution_rules.parse_submission(loop.submission)
        report.summary = summary or loop.final_text
        outcome = resolution_rules.validate(context, raw)
        accepted = outcome.accepted
        report.rejected = outcome.rejected
    else:
        report.summary = (
            "No relationship or datatype problem needed a decision; the load order "
            "was recomputed from the existing mappings."
        )

    report.resolutions = [resolution.to_dict() for resolution in accepted]

    # Patch a draft, never the stored config, so ordering and the follow-up scan
    # both see the post-change state before anything is committed.
    draft = context.draft()
    changes, unapplied = wiring.apply_resolutions(
        draft, accepted, agent=wiring.AGENT_RELATIONSHIP
    )
    report.rejected.extend(
        {"resolution": item, "reason": item.get("reason", "")} for item in unapplied
    )

    analysis = context.dependency_analysis(draft_configs=draft)
    report.load_order = analysis.get("order", [])
    report.dependency_edges = analysis.get("edges", [])
    report.has_cycle = bool(analysis.get("has_cycle"))
    changes.extend(
        wiring.assign_load_order(
            draft, report.load_order, agent=wiring.AGENT_RELATIONSHIP
        )
    )
    report.changes = [change.to_dict() for change in changes]

    remaining = detector.blocking(detector.detect(context, draft_configs=draft))
    report.remaining_blockers = [finding.to_dict() for finding in remaining]

    if effective_autonomy == AUTONOMY_APPLY and changes:
        wiring.persist(draft, list(draft))
        report.change_ids = audit.record_changes(
            run_id, changes, session_id=session_id
        )
        report.applied = True
        context.configs = draft
        logger.info(
            "[agent] relationship run %s applied %d change(s) across %d mapping(s)",
            run_id,
            len(changes),
            len(draft),
        )
    elif effective_autonomy == AUTONOMY_APPLY:
        report.applied = True  # nothing needed changing

    return report


def _run_agent(
    context: MigrationContext,
    findings: list[Finding],
    settings: AgentSettings,
) -> LoopResult:
    client = build_client(PURPOSE_RELATIONSHIP, settings)
    dispatcher = ToolDispatcher(context)
    return run_tool_loop(
        client,
        system=SYSTEM_PROMPT,
        prompt=_build_prompt(context, findings),
        tools=build_tools(),
        dispatch=dispatcher,
        submit_tool=SUBMIT_TOOL,
    )
