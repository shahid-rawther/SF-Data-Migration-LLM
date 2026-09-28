"""The diagnosis agent.

Runs when a load still has rejected rows after the deterministic rules and the
repair agent have had their turn. Its job is not to change anything: it looks
at the rows the destination rejected and the staged source rows behind them,
explains in plain language why they failed, and ranks a few ways to fix it.
Where a mapping change would work it attaches validated resolutions, so a
person can turn a suggestion into a proposal with one click.

When the model is unavailable a deterministic summary of the error digest is
stored instead, so the session still says what failed and the conversation can
pick it up from there.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from common.logger import logger

from agents import assistant_store, resolutions as resolution_rules
from agents.bedrock_client import AgentError, build_client, run_tool_loop
from agents.config import PURPOSE_RELATIONSHIP, AgentSettings, load_settings
from agents.context import MigrationContext
from agents.error_digest import ErrorDigest
from agents.tools import SUBMIT_DIAGNOSIS_TOOL, ToolDispatcher, build_diagnosis_tools

SYSTEM_PROMPT = """\
You diagnose data-migration load failures that the automatic repairs could not \
fix. Rows were extracted from the source, transformed by a field mapping and \
loaded into the destination, which rejected some of them.

Investigate before concluding:

- Read the error digest, then call sample_failed_rows to see the values that \
were sent and the source rows they came from. Many rejections are explained by \
the source data rather than the mapping — an empty prerequisite column, a value \
outside a picklist, a status that makes another field mandatory.
- Use profile_source_field to size the problem (how many rows are empty, which \
values occur) and describe_mapping / describe_destination_object to see how \
the failing field is produced and what the destination accepts.

Then submit_diagnosis once:

- analysis: the root cause in plain language, naming fields and values. Say \
whether it is a data problem, a mapping problem, or a destination rule.
- evidence: short, concrete observations you actually saw.
- suggestions: two or three, best first, each with its trade-off. Attach \
resolutions whenever a mapping change implements the suggestion. Useful \
patterns: generic_rules (type_coercion) to set or clear a field based on other \
source columns — its params are {"inputs": [source fields, in source_field \
order], "rules": [{"all": [{"field", "op", "value"}], "then": {"value"} or \
{"transform", "from", "params"}}], "otherwise": {"value"}}, and source_field \
must then list every input comma-separated; generic_default_if_empty with \
several source fields for a fallback column; value_map for picklists; \
remove_field to stop sending a field. A suggestion that needs a person \
(change a validation rule, fix source data) has no resolutions.\
"""


@dataclass
class Diagnosis:
    mapping: str
    analysis: str = ""
    evidence: list[str] = field(default_factory=list)
    suggestions: list[dict] = field(default_factory=list)
    id: int | None = None
    model_invoked: bool = False
    error: str = ""
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "mapping": self.mapping,
            "analysis": self.analysis,
            "evidence": self.evidence,
            "suggestions": self.suggestions,
            "model_invoked": self.model_invoked,
            "error": self.error,
            "usage": self.usage,
        }


def diagnose(
    context: MigrationContext,
    mapping_name: str,
    digest: ErrorDigest,
    session_id: str = "",
    settings: AgentSettings | None = None,
) -> Diagnosis:
    """Explain why a mapping's rows are still rejected, and persist the result."""
    settings = settings or load_settings()
    result = Diagnosis(mapping=mapping_name)

    try:
        loop = _run(context, mapping_name, digest, settings)
        result.model_invoked = True
        result.usage = loop.usage
        submission = loop.submission or {}
        result.analysis = str(submission.get("analysis") or loop.final_text or "").strip()
        result.evidence = [
            str(item) for item in submission.get("evidence") or [] if str(item).strip()
        ]
        result.suggestions = _validated_suggestions(
            context, submission.get("suggestions") or []
        )
        if not result.analysis:
            raise AgentError(
                f"the model finished without a diagnosis (stop reason: {loop.stop_reason})"
            )
    except AgentError as exc:
        logger.warning("[agent] diagnosis for %s fell back to the digest: %s", mapping_name, exc)
        result.error = str(exc)
        result.analysis, result.evidence = _fallback(digest)
        result.suggestions = []

    result.id = assistant_store.save_diagnosis(
        session_id=session_id,
        mapping_name=mapping_name,
        digest=digest.to_dict(),
        analysis=result.analysis,
        evidence=result.evidence,
        suggestions=result.suggestions,
    )
    return result


def _run(context: MigrationContext, mapping_name: str, digest: ErrorDigest, settings):
    client = build_client(PURPOSE_RELATIONSHIP, settings)
    dispatcher = ToolDispatcher(context, digest=digest.to_dict())
    prompt = (
        f"Source system: {context.source_type}\n"
        f"Mapping still failing: {mapping_name} -> "
        f"{context.destination_object(mapping_name)} "
        f"(driving source table: {context.driving_table(mapping_name)})\n"
        f"Other mappings in this migration: "
        f"{[n for n in context.mapping_names if n != mapping_name]}\n\n"
        f"Errors that remain after automatic repair:\n"
        f"{json.dumps(digest.to_dict(), indent=2)}\n\n"
        "Investigate, then call submit_diagnosis once."
    )
    return run_tool_loop(
        client,
        system=SYSTEM_PROMPT,
        prompt=prompt,
        tools=build_diagnosis_tools(),
        dispatch=dispatcher,
        submit_tool=SUBMIT_DIAGNOSIS_TOOL,
    )


def _validated_suggestions(context: MigrationContext, raw: list) -> list[dict]:
    """Keep every suggestion's text; keep only resolutions the validator admits."""
    suggestions: list[dict] = []
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        accepted: list[dict] = []
        rejected: list[dict] = []
        for resolution in item.get("resolutions") or []:
            if not isinstance(resolution, dict):
                continue
            outcome = resolution_rules.validate(context, [resolution])
            if outcome.accepted:
                accepted.append(resolution)
            else:
                rejected.extend(outcome.rejected)
        suggestions.append(
            {
                "title": str(item.get("title")).strip(),
                "explanation": str(item.get("explanation") or "").strip(),
                "resolutions": accepted,
                "rejected": rejected,
            }
        )
    return suggestions


def _fallback(digest: ErrorDigest) -> tuple[str, list[str]]:
    """What can be said about the failure without the model."""
    evidence = [
        f"{entry.count} row(s): {entry.code or 'ERROR'} — {entry.message}"
        + (f" (fields: {', '.join(entry.fields)})" if entry.fields else "")
        for entry in digest.entries
    ]
    analysis = (
        f"{digest.failed_rows} row(s) of {digest.mapping} are still rejected by the "
        "destination after the automatic repairs. The agent could not be reached to "
        "analyse the cause, so this is the raw error summary. Ask the assistant below "
        "to investigate once it is available."
    )
    return analysis, evidence
