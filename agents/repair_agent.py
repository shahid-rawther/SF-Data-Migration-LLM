"""The repair agent.

When the destination rejects rows, this reads the aggregated error digest,
decides which field transform was wrong, patches the mapping and reports whether
a retry is worth running. It uses the same resolution vocabulary and the same
validator as the relationship agent, so a repair cannot express a change that
would have been refused at design time — the only differences are a narrower
prompt, a cheaper model, and the fact that its input is errors rather than
schema.

The retry itself belongs to the pipeline, not here: because the loader records an
id mapping for every row that succeeded, re-transforming an object naturally
emits only the rows that failed.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from common.logger import logger

from agents import audit, error_digest, resolutions as resolution_rules, wiring
from agents.bedrock_client import AgentError, build_client, run_tool_loop
from agents.config import (
    AUTONOMY_APPLY,
    AUTONOMY_PREVIEW,
    PURPOSE_REPAIR,
    AgentSettings,
    load_settings,
)
from agents.context import MigrationContext
from agents.error_digest import ErrorDigest
from agents.tools import SUBMIT_REPAIR_TOOL, ToolDispatcher, build_repair_tools

SYSTEM_PROMPT = """\
You repair field mappings after a data load was partially rejected by the \
destination system.

What has already happened:

1. Source data was extracted and staged locally, then transformed per the mapping \
and loaded. Some rows were rejected and you are given the aggregated errors.
2. Rows that succeeded are recorded, so when the object is re-transformed only the \
rejected rows are produced again. A retry is cheap and does not duplicate work.

Your job is to decide, for each error, whether a mapping change would make those \
rows load, and if so which one.

- Call get_error_digest first, then look up the implicated fields with \
describe_destination_object and see how they are currently produced with \
describe_mapping.
- Match the fix to the error. A value too long for the field wants \
generic_truncate at the field's real length. A malformed date wants \
generic_format_datetime with the source's actual format. A value rejected by a \
restricted picklist wants value_map with the destination's real values. A lookup \
id that the destination will not accept wants lookup_reference against the \
mapping that loads the parent.
- Set retryable false when the mapping is not the problem: insufficient \
permissions, a validation rule or trigger, a required parent record that does not \
exist. Submit a notice explaining it and change nothing.
- Never widen a fix beyond what the error shows. One error, one field, one change.
- Submit once, via submit_repairs.\
"""


@dataclass
class RepairReport:
    run_id: str
    mapping: str
    autonomy: str
    applied: bool = False
    retryable: bool = False
    summary: str = ""
    digest: dict = field(default_factory=dict)
    resolutions: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    change_ids: list[int] = field(default_factory=list)
    model_invoked: bool = False
    usage: dict = field(default_factory=dict)
    stop_reason: str = ""
    error: str = ""

    @property
    def should_retry(self) -> bool:
        """A retry is only worth running when something actually changed."""
        return self.applied and self.retryable and bool(self.changes)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "mapping": self.mapping,
            "autonomy": self.autonomy,
            "applied": self.applied,
            "retryable": self.retryable,
            "should_retry": self.should_retry,
            "summary": self.summary,
            "digest": self.digest,
            "resolutions": self.resolutions,
            "rejected": self.rejected,
            "changes": self.changes,
            "change_ids": self.change_ids,
            "model_invoked": self.model_invoked,
            "usage": self.usage,
            "stop_reason": self.stop_reason,
            "error": self.error,
        }


def build_digest(
    context: MigrationContext,
    mapping_name: str,
    messages: list[str] | None = None,
) -> ErrorDigest:
    """Prefer the written failed-result CSVs, falling back to the loader's messages."""
    config = context.configs.get(mapping_name) or {}
    destination_object = context.destination_object(mapping_name)
    digest = error_digest.from_results(
        mapping=mapping_name,
        destination_object=destination_object,
        transformation_name=config.get("transformation_name", ""),
    )
    if digest.is_empty and messages:
        digest = error_digest.from_messages(
            mapping=mapping_name,
            messages=messages,
            destination_object=destination_object,
        )
    return digest


def repair(
    source_type: str,
    mapping_name: str,
    error_messages: list[str] | None = None,
    source_credentials: dict | None = None,
    destination_credentials: dict | None = None,
    session_id: str = "",
    related_mappings: list[str] | None = None,
    autonomy: str | None = None,
    settings: AgentSettings | None = None,
) -> RepairReport:
    """Diagnose one object's load failures and patch its mapping.

    ``related_mappings`` should be the rest of the migration's objects: a lookup
    repair needs to know which other mapping loads the parent, and without them
    the only available answer would be a notice.
    """
    settings = settings or load_settings()
    effective_autonomy = (autonomy or settings.autonomy).lower()
    if effective_autonomy not in (AUTONOMY_PREVIEW, AUTONOMY_APPLY):
        effective_autonomy = AUTONOMY_PREVIEW
    run_id = uuid.uuid4().hex
    report = RepairReport(
        run_id=run_id, mapping=mapping_name, autonomy=effective_autonomy
    )

    names = [mapping_name] + [
        name for name in (related_mappings or []) if name != mapping_name
    ]
    context = MigrationContext.load(
        source_type=source_type,
        mapping_names=names,
        source_credentials=source_credentials,
        destination_credentials=destination_credentials,
    )
    if mapping_name not in context.mapping_names:
        report.error = f"No mapping config found for '{mapping_name}'"
        return report

    digest = build_digest(context, mapping_name, error_messages)
    report.digest = digest.to_dict()
    if digest.is_empty:
        report.error = "No load errors could be read for this object"
        return report

    try:
        loop = _run_agent(context, mapping_name, digest, settings)
    except AgentError as exc:
        logger.error("[agent] repair pass failed: %s", exc)
        report.error = str(exc)
        return report

    report.model_invoked = True
    report.usage = loop.usage
    report.stop_reason = loop.stop_reason
    if loop.submission is None:
        report.error = (
            "The repair agent finished without submitting a decision "
            f"(stop reason: {loop.stop_reason})"
        )
        report.summary = loop.final_text
        return report

    report.retryable = bool(loop.submission.get("retryable", True))
    summary, raw = resolution_rules.parse_submission(loop.submission)
    report.summary = summary or loop.final_text

    outcome = resolution_rules.validate(context, raw)
    report.rejected = outcome.rejected
    report.resolutions = [item.to_dict() for item in outcome.accepted]

    draft = context.draft()
    changes, unapplied = wiring.apply_resolutions(
        draft, outcome.accepted, agent=wiring.AGENT_REPAIR
    )
    report.rejected.extend(
        {"resolution": item, "reason": item.get("reason", "")} for item in unapplied
    )
    report.changes = [change.to_dict() for change in changes]

    if effective_autonomy == AUTONOMY_APPLY and changes:
        touched = sorted({change.mapping_name for change in changes})
        wiring.persist(draft, touched)
        report.change_ids = audit.record_changes(run_id, changes, session_id=session_id)
        report.applied = True
        logger.info(
            "[agent] repair run %s patched %d field(s) on %s",
            run_id,
            len(changes),
            ", ".join(touched),
        )
    return report


def _run_agent(
    context: MigrationContext,
    mapping_name: str,
    digest: ErrorDigest,
    settings: AgentSettings,
):
    client = build_client(PURPOSE_REPAIR, settings)
    dispatcher = ToolDispatcher(context, digest=digest.to_dict())
    prompt = (
        f"Source system: {context.source_type}\n"
        f"Mapping that failed: {mapping_name} -> "
        f"{context.destination_object(mapping_name)}\n"
        f"Other mappings in this migration: "
        f"{[n for n in context.mapping_names if n != mapping_name]}\n\n"
        f"Errors returned by the destination:\n{json.dumps(digest.to_dict(), indent=2)}\n\n"
        "Investigate the implicated fields, then call submit_repairs once."
    )
    return run_tool_loop(
        client,
        system=SYSTEM_PROMPT,
        prompt=prompt,
        tools=build_repair_tools(),
        dispatch=dispatcher,
        submit_tool=SUBMIT_REPAIR_TOOL,
    )
