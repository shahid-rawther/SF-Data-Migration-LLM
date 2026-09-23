"""Model-assisted field matching for the mapping step.

This is the only suggester. Name similarity alone was never enough — nothing
about ``u_caller_id`` resembles ``ContactId``, only their meaning does — so the
model reads names, labels and types as a human would and proposes the whole
draft, from the fields whose names already agree to the ones that only agree in
meaning.

The model only ever proposes. Every proposal is re-checked here against the
source table and the live destination describe: both fields have to exist, the
destination has to be writable and still free, and a lookup has to be wired
through the migrated-id table rather than copied raw. Whatever survives is
returned as a draft for a person to accept in the wizard — field-to-field
mapping stays a human decision, and nothing is written to a mapping config from
here.
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

SUBMIT_TOOL = "submit_field_matches"

SYSTEM_PROMPT = """\
You map fields from a source ITSM system onto Salesforce fields for a data \
migration.

You are given the source fields that have no destination yet and the \
destination fields with nothing feeding them. Pair up everything that holds the \
same real-world fact.

Work in two sweeps. First take the pairs whose names already agree, allowing \
for the conventions each system uses — `short_description` belongs in \
`Subject`, `sys_created_on` in `CreatedDate`. These are the cheapest to get \
right, so do not skip them. Then look for the pairs a name comparison would \
never find, where only the meaning matches: a source field `u_caller_id` \
labelled "Caller" belongs in `ContactId`.

Rules:
- Only use field names exactly as they appear in the lists you are given. Never \
invent, abbreviate or reformat a name.
- Propose a pair only when the two fields hold the same real-world fact. Two \
fields being the same data type is not a reason to pair them, and neither is a \
loose resemblance between two names.
- Each source field and each destination field may appear at most once.
- Prefer leaving a field unmapped over a speculative pair. An unmapped field \
costs a person one decision; a wrong pair corrupts data.
- Set confidence honestly: above 0.8 when the meaning is unambiguous, 0.5 to 0.8 \
when it is a reasonable reading of the labels, below 0.5 when you are guessing.
- Write the rationale for a migration engineer who cannot see this prompt. Say \
what fact the two fields hold, not that the names look alike.

Call submit_field_matches exactly once with everything you found. If nothing \
matches, submit an empty list.
"""

SUBMIT_TOOL_SPEC = ToolSpec(
    name=SUBMIT_TOOL,
    description=(
        "Submit the semantic field pairs you found. Call this exactly once, "
        "even when the list is empty."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One or two sentences on what you matched and what you left alone.",
            },
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_field": {
                            "type": "string",
                            "description": "Exact name from the source field list.",
                        },
                        "destination_field": {
                            "type": "string",
                            "description": "Exact name from the destination field list.",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "0 to 1.",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Why these two hold the same fact.",
                        },
                    },
                    "required": [
                        "source_field",
                        "destination_field",
                        "confidence",
                        "rationale",
                    ],
                },
            },
        },
        "required": ["matches"],
    },
)


@dataclass
class FieldMatch:
    """One accepted pair, shaped to drop straight into a mapping config."""

    source_field: str
    destination_field: str
    confidence: float
    match_reason: str
    transformation_function: str = ""
    transformation_params: dict | None = None
    type_warning: str = ""

    def to_field_entry(self) -> dict:
        entry: dict = {
            "source_field": self.source_field,
            "destination_field": self.destination_field,
            "confidence": self.confidence,
            "match_reason": self.match_reason,
        }
        if self.transformation_function:
            entry["transformation_function"] = self.transformation_function
        if self.transformation_params is not None:
            entry["transformation_params"] = self.transformation_params
        if self.type_warning:
            entry["type_warning"] = self.type_warning
        return entry


@dataclass
class FieldMatchReport:
    source_table: str
    destination_object: str
    matches: list[FieldMatch] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    summary: str = ""
    considered_source_fields: int = 0
    considered_destination_fields: int = 0
    truncated: bool = False
    usage: dict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "source_table": self.source_table,
            "destination_object": self.destination_object,
            "matches": [m.to_field_entry() for m in self.matches],
            "rejected": self.rejected,
            "summary": self.summary,
            "considered_source_fields": self.considered_source_fields,
            "considered_destination_fields": self.considered_destination_fields,
            "truncated": self.truncated,
            "usage": self.usage,
            "error": self.error,
        }


# ── candidate selection ────────────────────────────────────────────────────────

# Source columns that never carry business meaning worth matching semantically.
_SOURCE_NOISE = {
    "sys_created_by",
    "sys_created_on",
    "sys_domain",
    "sys_domain_path",
    "sys_id",
    "sys_mod_count",
    "sys_tags",
    "sys_updated_by",
    "sys_updated_on",
}


def _short(name: str) -> str:
    return name.split(".")[-1].lower()


def _already_used(existing_fields: list[dict]) -> tuple[set[str], set[str]]:
    """Return the source and destination names the draft mapping already uses."""
    used_source: set[str] = set()
    used_destination: set[str] = set()
    for entry in existing_fields or []:
        destination = str(entry.get("destination_field") or "")
        if destination:
            used_destination.add(destination.lower())
        source = entry.get("source_field")
        if isinstance(source, list):
            used_source.update(_short(str(s)) for s in source if s)
        elif source:
            used_source.add(_short(str(source)))
    return used_source, used_destination


def _source_candidates(source_fields: list, used_source: set[str]) -> list[dict]:
    candidates = []
    for f in source_fields:
        short = _short(f.name)
        if short in used_source or short in _SOURCE_NOISE:
            continue
        entry = {
            "name": f.name,
            "label": getattr(f, "label", "") or f.name,
            "type": getattr(f, "field_type", ""),
        }
        reference_table = getattr(f, "reference_table", "")
        if reference_table:
            entry["references"] = reference_table
        candidates.append(entry)
    return candidates


def _destination_candidates(
    destination_fields: list[dict], used_destination: set[str], operation: str
) -> list[dict]:
    candidates = []
    for meta in destination_fields:
        name = meta.get("name", "")
        if not name or name.lower() in used_destination:
            continue
        # Insert loads can only write createable fields; on update/upsert the
        # updateable set is what matters.
        writable = meta.get("createable") if operation == "insert" else meta.get("updateable")
        if not writable:
            continue
        entry = {
            "name": name,
            "label": meta.get("label", name),
            "type": meta.get("type", ""),
        }
        if meta.get("required"):
            entry["required"] = True
        if meta.get("reference_to"):
            entry["references"] = meta["reference_to"]
        picklist = meta.get("picklist_values") or []
        if picklist:
            entry["picklist_values"] = picklist[:12]
        candidates.append(entry)
    return candidates


def _build_prompt(
    source_table: str,
    destination_object: str,
    source_candidates: list[dict],
    destination_candidates: list[dict],
) -> str:
    import json

    return (
        f"Source table: {source_table}\n"
        f"Destination Salesforce object: {destination_object}\n\n"
        "Unmapped source fields:\n"
        f"{json.dumps(source_candidates, indent=1)}\n\n"
        "Unmapped destination fields:\n"
        f"{json.dumps(destination_candidates, indent=1)}\n\n"
        "Find the pairs that mean the same thing and submit them."
    )


# ── validation ────────────────────────────────────────────────────────────────


def _resolve_transform(
    source_meta: dict,
    destination_meta: dict,
    lookup_targets: dict[str, dict],
) -> tuple[str, dict | None, str]:
    """Decide the transform for an accepted pair.

    Both arguments are the candidate dicts built above, where a reference target
    is held under ``references`` on either side.

    Returns ``(transformation_function, params, warning)``. A lookup destination
    is resolved through the migrated-id table for the parent mapping when that
    parent is part of the run; otherwise the pair is still offered but carries a
    warning, because copying a source id into a lookup fails the load.
    """
    from api.compat_engine import get_type_coercion

    if destination_meta.get("references"):
        reference_table = source_meta.get("references", "")
        target = lookup_targets.get(reference_table) if reference_table else None
        if target:
            lookup_table = (
                f"{reference_table}_{target['sf_object']}_{target['mapping_name']}"
            )
            return (
                "generic_lookup",
                {"lookup_tables": [lookup_table], "default": ""},
                "",
            )
        return (
            "",
            None,
            (
                f"'{destination_meta.get('name')}' is a lookup. Add the parent object to "
                "this migration so ids can be resolved, or pin it to a constant record — "
                "a raw source id will be rejected on load."
            ),
        )

    if destination_meta.get("picklist_values"):
        return (
            "generic_value_map",
            {"map": {}, "default": ""},
            (
                f"'{destination_meta.get('name')}' is a picklist. Fill in the value map, "
                "or unmatched source values will be blanked."
            ),
        )

    coercion = get_type_coercion(
        source_meta.get("type", ""), destination_meta.get("type", "")
    )
    if coercion.get("compatible"):
        return "", None, ""
    if coercion.get("coercible"):
        return coercion["transform"], coercion["params"], ""
    return "", None, coercion.get("warning", "")


def _validate(
    raw_matches: list[dict],
    source_candidates: list[dict],
    destination_candidates: list[dict],
    lookup_targets: dict[str, dict],
    min_confidence: float,
) -> tuple[list[FieldMatch], list[dict]]:
    source_index = {c["name"].lower(): c for c in source_candidates}
    destination_index = {c["name"].lower(): c for c in destination_candidates}

    accepted: list[FieldMatch] = []
    rejected: list[dict] = []
    claimed_source: set[str] = set()
    claimed_destination: set[str] = set()

    def reject(item: dict, reason: str) -> None:
        rejected.append({"match": item, "reason": reason})
        logger.info("[field-match] rejected %s: %s", item, reason)

    for item in raw_matches:
        if not isinstance(item, dict):
            continue
        source_name = str(item.get("source_field") or "").strip()
        destination_name = str(item.get("destination_field") or "").strip()
        rationale = str(item.get("rationale") or "").strip()

        source_meta = source_index.get(source_name.lower())
        if not source_meta:
            reject(item, f"'{source_name}' is not an unmapped field on this source table")
            continue
        destination_meta = destination_index.get(destination_name.lower())
        if not destination_meta:
            reject(
                item,
                f"'{destination_name}' is not a writable, unmapped field on this object",
            )
            continue
        if not rationale:
            reject(item, "A rationale is required for every match")
            continue

        try:
            confidence = round(float(item.get("confidence")), 2)
        except (TypeError, ValueError):
            reject(item, "Confidence must be a number between 0 and 1")
            continue
        if not 0.0 <= confidence <= 1.0:
            reject(item, "Confidence must be between 0 and 1")
            continue
        if confidence < min_confidence:
            reject(item, f"Confidence {confidence} is below the {min_confidence} threshold")
            continue

        canonical_source = source_meta["name"]
        canonical_destination = destination_meta["name"]
        if canonical_source in claimed_source:
            reject(item, f"'{canonical_source}' was already matched in this run")
            continue
        if canonical_destination in claimed_destination:
            reject(item, f"'{canonical_destination}' was already matched in this run")
            continue

        transform, params, warning = _resolve_transform(
            source_meta, destination_meta, lookup_targets
        )
        claimed_source.add(canonical_source)
        claimed_destination.add(canonical_destination)
        accepted.append(
            FieldMatch(
                source_field=canonical_source,
                destination_field=canonical_destination,
                confidence=confidence,
                match_reason=f"semantic match — {rationale}",
                transformation_function=transform,
                transformation_params=params,
                type_warning=warning,
            )
        )

    accepted.sort(key=lambda m: m.confidence, reverse=True)
    return accepted, rejected


# ── entry point ────────────────────────────────────────────────────────────────


def suggest(
    *,
    source_type: str,
    source_table: str,
    destination_object: str,
    source_credentials: dict,
    destination_credentials: dict,
    existing_fields: list[dict] | None = None,
    other_mappings: list[dict] | None = None,
    operation: str = "insert",
    settings: AgentSettings | None = None,
    client: AgentClient | None = None,
) -> FieldMatchReport:
    """Propose field pairs for everything the draft has not already mapped."""
    from api import sources as source_registry
    from api.compat_engine import get_destination_fields, record_type_id_of

    settings = settings or load_settings()
    report = FieldMatchReport(
        source_table=source_table, destination_object=destination_object
    )

    provider = source_registry.get(source_type)
    if not provider:
        report.error = f"Unknown source type '{source_type}'"
        return report

    source_fields = provider.get_table_fields(source_table, source_credentials)
    # If the draft already pins a record type, match against the values that
    # record type accepts. Showing the model the describe's union invites it to
    # propose a pairing the load will refuse.
    destination_fields = get_destination_fields(
        destination_object,
        destination_credentials,
        record_type_id_of({"fields": existing_fields or []}),
    )
    if not destination_fields:
        report.error = (
            f"{destination_object} could not be described, so matches cannot be "
            "schema-checked. Check the Salesforce connection and object permissions."
        )
        return report

    used_source, used_destination = _already_used(existing_fields or [])
    source_candidates = _source_candidates(source_fields, used_source)
    destination_candidates = _destination_candidates(
        destination_fields, used_destination, operation
    )

    cap = settings.field_match_max_fields
    if len(source_candidates) > cap or len(destination_candidates) > cap:
        report.truncated = True
        source_candidates = source_candidates[:cap]
        destination_candidates = destination_candidates[:cap]

    report.considered_source_fields = len(source_candidates)
    report.considered_destination_fields = len(destination_candidates)

    if not source_candidates or not destination_candidates:
        report.summary = (
            "Nothing left to match — every source field or every writable "
            "destination field is already mapped."
        )
        return report

    lookup_targets = {
        m["source_table"]: m
        for m in (other_mappings or [])
        if m.get("source_table") and m.get("sf_object")
    }

    try:
        client = client or build_client(PURPOSE_FIELD_MATCH, settings)
        result = run_tool_loop(
            client,
            system=SYSTEM_PROMPT,
            prompt=_build_prompt(
                source_table, destination_object, source_candidates, destination_candidates
            ),
            tools=[SUBMIT_TOOL_SPEC],
            dispatch=_no_tools,
            submit_tool=SUBMIT_TOOL,
        )
    except AgentError as exc:
        report.error = str(exc)
        logger.warning("[field-match] %s -> %s failed: %s", source_table, destination_object, exc)
        return report

    report.usage = result.usage
    submission = result.submission or {}
    raw_matches = submission.get("matches")
    if not isinstance(raw_matches, list):
        report.error = "The model finished without submitting any matches."
        report.summary = result.final_text
        return report

    report.matches, report.rejected = _validate(
        raw_matches,
        source_candidates,
        destination_candidates,
        lookup_targets,
        settings.field_match_min_confidence,
    )
    report.summary = str(submission.get("summary") or result.final_text or "")
    logger.info(
        "[field-match] %s -> %s: %d accepted, %d rejected from %d proposals",
        source_table,
        destination_object,
        len(report.matches),
        len(report.rejected),
        len(raw_matches),
    )
    return report


def _no_tools(name: str, arguments: dict):
    raise AgentError(
        f"'{name}' is not available; call {SUBMIT_TOOL} with the matches you have."
    )
