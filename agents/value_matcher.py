"""Model-assisted picklist value matching for the mapping step.

Pairing two fields is only half the job. Once ``incident.state`` is mapped to
``Status``, every distinct value in that column still has to land on a value the
destination will accept, and the two vocabularies rarely share any literal
spelling: a ServiceNow incident state is a numeric code with a label, and the
Salesforce picklist it feeds was named by whoever configured the org. Comparing
strings finds almost nothing across that gap, and the substring fallback it
tempts you into is worse than nothing — ``Closed`` "matches" ``Closed
Incomplete`` and quietly changes what a record means.

So the model reads both vocabularies and proposes the pairing, the same way it
proposes field pairs in :mod:`agents.field_matcher`. Two things differ:

* several source values may legitimately collapse onto one destination value —
  ``Closed Complete`` and ``Closed Incomplete`` are both ``Closed`` — so the
  destination side is deliberately not held to one use each;
* every proposal is checked against the values the *record type* offers rather
  than the object describe's union, and against the controlling field where the
  picklist is dependent, because those are the constraints the load enforces.

The bias also runs the other way. Field matching prefers to leave a field alone
over pairing it speculatively, because an unmapped field costs one decision and
a wrong one corrupts a column. Values have no such safe default: a source value
with no destination rejects the whole record on a restricted picklist, and is
written through unrecognised on an unrestricted one. So the model is asked to
find every value its closest available home — a catch-all where nothing specific
fits — and confidence becomes the signal for which pairings are approximations
rather than a bar they have to clear. Anything below the threshold is applied and
flagged for review, so an approximation is visible instead of absent.

Nothing is written to a mapping config from here. The result is a draft the
expert accepts in the value-map editor.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from common.logger import logger

from agents.bedrock_client import (
    AgentClient,
    AgentError,
    ToolSpec,
    build_client,
    run_tool_loop,
)
from agents.config import PURPOSE_FIELD_MATCH, AgentSettings, load_settings

SUBMIT_TOOL = "submit_value_matches"

SYSTEM_PROMPT = """\
You map the values of one source ITSM field onto the values a Salesforce \
picklist accepts, for a data migration.

You are given the distinct values found in the source column and the values the \
destination picklist will accept. Find every source value the closest home \
available on the destination side.

**Map every source value.** A source value you leave alone does not stay as it \
is: on a restricted picklist it rejects the whole record, and on an unrestricted \
one it is written through into a column no report will match. Neither is a safe \
resting place, so "no destination value captures this exactly" is not a reason \
to leave one out — it is a reason to choose the nearest one and say what is lost. \
An expert reviews every pairing you make and can change any of them in seconds; \
what they cannot do is notice the value you never mentioned.

The two vocabularies will not line up cleanly, so expect to approximate:
- The spellings rarely agree. A source state may be a numeric code with a label \
("2" / "In Progress"); judge by the label and the meaning, never by how close \
two strings look.
- Several source values belong on one destination value all the time. "Closed \
Complete" and "Closed Incomplete" both mean Closed; "Resolved by change" and \
"Resolved by problem" may both be the same generic "Fixed". Reusing a \
destination value as often as you need to is expected and correct, not a \
conflict to avoid.
- Where the source draws a distinction the destination does not, that \
distinction is simply not migrating. Pick the destination value that keeps the \
most meaning and name the lost detail in the rationale.
- When nothing specific fits, use the general or catch-all value the list \
offers — Other, Unknown, General, and so on — rather than leaving the value out. \
A record landing on "Other" with a note is recoverable; a rejected record is not.
- Only where the list offers no plausible home at all, not even a catch-all, \
should a source value go in `unmapped`. Treat that as a last resort and say what \
kind of value the picklist would need.

Two constraints are about correctness, not caution, and they outrank the \
instruction to map everything:
- Ordered scales must keep their order and their direction. If both sides rank \
severity, the most severe source value maps to the most severe destination \
value. Check the ends before the middle: an inverted scale is the most damaging \
mistake available here, and the easiest to make when one side counts up and the \
other counts down.
- Terminal states must stay terminal and active states active. Never map a \
closed or cancelled source value onto an open destination value, or the reverse. \
Where that leaves no home, a catch-all on the correct side of the line beats a \
precise value on the wrong side.

Rules:
- Only use values exactly as they appear in the two lists. Never invent, reword, \
re-case or reformat a value.
- Confidence is how close the fit is, and it is how the expert knows which \
pairings to read first. Be honest and use the whole range: above 0.8 when the \
two name the same thing, 0.5 to 0.8 when it is a fair reading, below 0.5 when it \
is the nearest available home rather than a real equivalent. A low confidence is \
not a reason to withhold the pairing — submit it with the number that reflects \
the truth.
- Write each rationale for a migration engineer who cannot see this prompt. Say \
what both values describe, and for anything approximate say plainly what \
distinction is being dropped.
- Suggest a `default` when one destination value is a sensible home for values \
that appear later and match nothing, which is usually the catch-all if there is \
one.

Call submit_value_matches exactly once with everything you found.
"""

SUBMIT_TOOL_SPEC = ToolSpec(
    name=SUBMIT_TOOL,
    description=(
        "Submit the source-to-destination value pairs you found. Call this "
        "exactly once, even when the list is empty."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One or two sentences on what you mapped and what you left alone.",
            },
            "default": {
                "type": "string",
                "description": (
                    "Destination value for anything unmatched at load time. Must "
                    "be one of the accepted values, or empty to leave blank."
                ),
            },
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_value": {
                            "type": "string",
                            "description": "Exact value from the source list.",
                        },
                        "destination_value": {
                            "type": "string",
                            "description": (
                                "Exact value from the destination list. May repeat "
                                "across matches."
                            ),
                        },
                        "confidence": {
                            "type": "number",
                            "description": (
                                "0 to 1, expressing how close the fit is. Submit "
                                "approximate pairings with a low number rather "
                                "than withholding them."
                            ),
                        },
                        "rationale": {
                            "type": "string",
                            "description": (
                                "What both values describe, and for an "
                                "approximate pairing what distinction is lost."
                            ),
                        },
                    },
                    "required": [
                        "source_value",
                        "destination_value",
                        "confidence",
                        "rationale",
                    ],
                },
            },
            "unmapped": {
                "type": "array",
                "description": (
                    "Last resort only: source values for which the picklist "
                    "offers no plausible home at all, not even a catch-all."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "source_value": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["source_value", "reason"],
                },
            },
        },
        "required": ["matches"],
    },
)


@dataclass
class ValueMatch:
    """One accepted pair, ready to drop into a ``generic_value_map``."""

    source_value: str
    destination_value: str
    confidence: float
    rationale: str
    # An approximation rather than an equivalence. Still applied — an unmapped
    # value is not a safe resting place — but surfaced for review, because the
    # expert reading the result needs to know which pairings are judgement calls.
    needs_review: bool = False

    def to_dict(self) -> dict:
        return {
            "source_value": self.source_value,
            "destination_value": self.destination_value,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "needs_review": self.needs_review,
        }


@dataclass
class ValueMatchReport:
    source_field: str
    destination_field: str
    matches: list[ValueMatch] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    unmapped: list[dict] = field(default_factory=list)
    suggested_default: str = ""
    summary: str = ""
    considered_source_values: int = 0
    considered_destination_values: int = 0
    truncated: bool = False
    restricted: bool = False
    usage: dict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "source_field": self.source_field,
            "destination_field": self.destination_field,
            "matches": [m.to_dict() for m in self.matches],
            "rejected": self.rejected,
            "unmapped": self.unmapped,
            "suggested_default": self.suggested_default,
            "summary": self.summary,
            "considered_source_values": self.considered_source_values,
            "considered_destination_values": self.considered_destination_values,
            "truncated": self.truncated,
            "restricted": self.restricted,
            "usage": self.usage,
            "error": self.error,
        }


# ── prompt ────────────────────────────────────────────────────────────────────


def _controller_values(existing_fields: list[dict], controller: str) -> list[str]:
    """The values the controlling field's own mapping will produce.

    Read from the draft rather than asked for, because the controller's mapping
    is already a decision the expert made, and it is what decides which of the
    dependent field's values are reachable at all.
    """
    for entry in existing_fields or []:
        if entry.get("destination_field") != controller:
            continue
        constant = entry.get("constant_value")
        if constant not in (None, ""):
            return [str(constant)]
        params = entry.get("transformation_params") or {}
        value_map = params.get("map") if isinstance(params, dict) else {}
        return sorted({str(v) for v in (value_map or {}).values() if v})
    return []


def _build_prompt(
    *,
    source_table: str,
    source_field: str,
    destination_object: str,
    destination_field: str,
    source_values: list[dict],
    destination_values: list[str],
    meta: dict,
    reachable: list[str],
    controller: str,
    already_mapped: dict[str, str],
) -> str:
    import json

    lines = [
        f"Source field: {source_table}.{source_field}",
        f"Destination picklist: {destination_object}.{destination_field}",
        "",
        "Distinct source values:",
        json.dumps(source_values, indent=1),
        "",
        "Destination values this load may write:",
        json.dumps(destination_values, indent=1),
    ]

    if meta.get("picklist_restricted"):
        lines += [
            "",
            "This picklist is restricted: a value outside the list above is "
            "rejected and the record fails to load. There is no partial credit "
            "for being close.",
        ]
    else:
        lines += [
            "",
            "This picklist is not restricted, so an unmatched value is stored "
            "as-is rather than rejected. That looks like a successful load and "
            "leaves values no report or filter will match, so treat a wrong "
            "mapping as the more expensive outcome, not a survivable one.",
        ]

    withheld = meta.get("picklist_unavailable") or []
    if withheld:
        lines += [
            "",
            f"These values exist on the field but the record type this mapping "
            f"loads with does not offer them, so they are not available to you: "
            f"{json.dumps(withheld)}",
        ]

    if controller:
        if reachable:
            lines += [
                "",
                f"This picklist depends on '{controller}'. Given what the "
                f"'{controller}' mapping produces, only these values are "
                f"reachable: {json.dumps(reachable)}. Anything else is rejected "
                "however well it fits the meaning.",
            ]
        else:
            lines += [
                "",
                f"This picklist depends on '{controller}', which this mapping "
                "does not set yet. Propose the pairing on meaning alone; it will "
                "be re-checked once the controller is mapped.",
            ]

    if already_mapped:
        lines += [
            "",
            "Already mapped by a person — leave these alone and do not repeat "
            f"them: {json.dumps(already_mapped, indent=1)}",
        ]

    lines += ["", "Pair the remaining source values and submit them."]
    return "\n".join(lines)


# ── validation ────────────────────────────────────────────────────────────────


def _validate(
    raw_matches: list[dict],
    source_index: dict[str, str],
    allowed: set[str],
    withheld: set[str],
    reachable: set[str] | None,
    controller: str,
    review_below: float,
) -> tuple[list[ValueMatch], list[dict]]:
    """Keep every pairing the destination would accept, flagging the loose ones.

    Only the schema decides what is refused here. Confidence no longer gates a
    pairing, because the alternative to a loose pairing is not a careful one —
    it is a value left out, which rejects the record on a restricted picklist and
    goes through unrecognised on an unrestricted one. So a low confidence marks
    the pair for review instead of dropping it.
    """
    accepted: list[ValueMatch] = []
    rejected: list[dict] = []
    claimed: set[str] = set()

    def reject(item: dict, reason: str) -> None:
        rejected.append({"match": item, "reason": reason})
        logger.info("[value-match] rejected %s: %s", item, reason)

    for item in raw_matches:
        if not isinstance(item, dict):
            continue
        source_value = str(item.get("source_value") or "").strip()
        destination_value = str(item.get("destination_value") or "").strip()
        rationale = str(item.get("rationale") or "").strip()

        canonical_source = source_index.get(source_value.lower())
        if canonical_source is None:
            reject(item, f"'{source_value}' is not one of this column's source values")
            continue
        if not rationale:
            reject(item, "A rationale is required for every match")
            continue
        if destination_value not in allowed:
            if destination_value in withheld:
                reject(
                    item,
                    f"'{destination_value}' exists on the field but the record type "
                    "this mapping loads with does not offer it",
                )
            else:
                reject(
                    item,
                    f"'{destination_value}' is not a value this picklist accepts",
                )
            continue
        # Checked separately from membership so the message names the real
        # constraint: the value is spelled correctly and still unreachable.
        if reachable is not None and destination_value not in reachable:
            reject(
                item,
                f"'{destination_value}' is not valid for any '{controller}' value "
                "this mapping produces",
            )
            continue

        try:
            confidence = round(float(item.get("confidence")), 2)
        except (TypeError, ValueError):
            reject(item, "Confidence must be a number between 0 and 1")
            continue
        if not 0.0 <= confidence <= 1.0:
            reject(item, "Confidence must be between 0 and 1")
            continue

        # One home per source value. The destination side is intentionally not
        # constrained: collapsing several source values onto one is the normal
        # shape of this mapping, not a collision.
        if canonical_source in claimed:
            reject(item, f"'{canonical_source}' was already mapped in this run")
            continue

        claimed.add(canonical_source)
        accepted.append(
            ValueMatch(
                source_value=canonical_source,
                destination_value=destination_value,
                confidence=confidence,
                rationale=rationale,
                needs_review=confidence < review_below,
            )
        )

    accepted.sort(key=lambda m: m.confidence, reverse=True)
    return accepted, rejected


# ── entry point ───────────────────────────────────────────────────────────────


def suggest(
    *,
    source_type: str,
    source_table: str,
    source_field: str,
    destination_object: str,
    destination_field: str,
    source_credentials: dict,
    destination_credentials: dict,
    existing_fields: list[dict] | None = None,
    source_values: list[dict] | None = None,
    settings: AgentSettings | None = None,
    client: AgentClient | None = None,
) -> ValueMatchReport:
    """Propose a value map for one picklist field.

    ``source_values`` may be supplied by the caller when the editor has already
    loaded them, so opening the panel and asking for suggestions costs one trip
    to the source rather than two.
    """
    from api import sources as source_registry
    from api.compat_engine import get_destination_fields, record_type_id_of

    settings = settings or load_settings()
    report = ValueMatchReport(
        source_field=source_field, destination_field=destination_field
    )

    provider = source_registry.get(source_type)
    if not provider:
        report.error = f"Unknown source type '{source_type}'"
        return report

    record_type_id = record_type_id_of({"fields": existing_fields or []})
    destination_fields = get_destination_fields(
        destination_object, destination_credentials, record_type_id
    )
    if not destination_fields:
        report.error = (
            f"{destination_object} could not be described, so values cannot be "
            "checked. Check the Salesforce connection and object permissions."
        )
        return report

    meta = next(
        (f for f in destination_fields if f["name"] == destination_field), None
    )
    if not meta:
        report.error = f"'{destination_field}' does not exist on {destination_object}"
        return report
    allowed = list(meta.get("picklist_values") or [])
    if not allowed:
        report.error = (
            f"'{destination_field}' is not a picklist, so there are no values to map."
        )
        return report
    report.restricted = bool(meta.get("picklist_restricted"))

    if source_values is None:
        try:
            source_values = provider.get_field_values(
                source_table, source_field, source_credentials, limit=200
            )
        except Exception as exc:  # noqa: BLE001 — a source that cannot list values
            report.error = f"Could not read the values of {source_table}.{source_field}: {exc}"
            return report

    # A value a person already mapped is left exactly as it is: the model is
    # here to fill gaps, not to relitigate decisions someone already made.
    already_mapped: dict[str, str] = {}
    for entry in existing_fields or []:
        if entry.get("destination_field") != destination_field:
            continue
        params = entry.get("transformation_params") or {}
        current = params.get("map") if isinstance(params, dict) else {}
        already_mapped = {
            str(k): str(v) for k, v in (current or {}).items() if v
        }
        break

    candidates = [
        {"value": str(v.get("value", "")), "label": str(v.get("label", "") or v.get("value", ""))}
        for v in source_values or []
        if str(v.get("value", "")) and str(v.get("value", "")) not in already_mapped
    ]

    cap = settings.field_match_max_fields
    if len(candidates) > cap:
        report.truncated = True
        candidates = candidates[:cap]

    report.considered_source_values = len(candidates)
    report.considered_destination_values = len(allowed)

    if not candidates:
        report.summary = (
            "Nothing left to map — every source value already has a destination."
        )
        return report

    controller = meta.get("picklist_controller") or ""
    reachable_list: list[str] = []
    reachable: set[str] | None = None
    if controller:
        valid_for = meta.get("picklist_valid_for") or {}
        produced = _controller_values(existing_fields or [], controller)
        if valid_for and produced:
            for parent in produced:
                reachable_list.extend(valid_for.get(parent) or [])
            reachable_list = sorted(set(reachable_list))
            reachable = set(reachable_list)

    try:
        client = client or build_client(PURPOSE_FIELD_MATCH, settings)
        result = run_tool_loop(
            client,
            system=SYSTEM_PROMPT,
            prompt=_build_prompt(
                source_table=source_table,
                source_field=source_field,
                destination_object=destination_object,
                destination_field=destination_field,
                source_values=candidates,
                destination_values=reachable_list or allowed,
                meta=meta,
                reachable=reachable_list,
                controller=controller,
                already_mapped=already_mapped,
            ),
            tools=[SUBMIT_TOOL_SPEC],
            dispatch=_no_tools,
            submit_tool=SUBMIT_TOOL,
        )
    except AgentError as exc:
        report.error = str(exc)
        logger.warning(
            "[value-match] %s.%s -> %s failed: %s",
            source_table,
            source_field,
            destination_field,
            exc,
        )
        return report

    report.usage = result.usage
    submission = result.submission or {}
    raw_matches = submission.get("matches")
    if not isinstance(raw_matches, list):
        report.error = "The model finished without submitting any value matches."
        report.summary = result.final_text
        return report

    source_index = {c["value"].lower(): c["value"] for c in candidates}
    report.matches, report.rejected = _validate(
        raw_matches,
        source_index,
        set(allowed),
        set(meta.get("picklist_unavailable") or []),
        reachable,
        controller,
        # Here the threshold marks a pairing as a judgement call rather than
        # discarding it, which is what the same number does in field matching.
        settings.field_match_min_confidence,
    )

    raw_unmapped = submission.get("unmapped")
    if isinstance(raw_unmapped, list):
        matched_sources = {m.source_value for m in report.matches}
        report.unmapped = [
            {
                "source_value": source_index[str(u.get("source_value", "")).lower()],
                "reason": str(u.get("reason") or ""),
            }
            for u in raw_unmapped
            if isinstance(u, dict)
            and str(u.get("source_value", "")).lower() in source_index
            and source_index[str(u.get("source_value", "")).lower()] not in matched_sources
        ]

    suggested_default = str(submission.get("default") or "").strip()
    if suggested_default and suggested_default in set(allowed):
        if reachable is None or suggested_default in reachable:
            report.suggested_default = suggested_default

    report.summary = str(submission.get("summary") or result.final_text or "")
    logger.info(
        "[value-match] %s.%s -> %s: %d accepted, %d rejected, %d left unmapped",
        source_table,
        source_field,
        destination_field,
        len(report.matches),
        len(report.rejected),
        len(report.unmapped),
    )
    return report


def _no_tools(name: str, arguments: dict):
    raise AgentError(
        f"'{name}' is not available; call {SUBMIT_TOOL} with the matches you have."
    )
