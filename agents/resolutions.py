"""Parsing and schema validation of what the relationship agent submits.

The agent proposes; this module decides what is admissible. Each resolution is
re-checked against the live destination describe — the field has to exist, a
lookup has to actually be a lookup, a parent has to be an object that field can
point at, a picklist value has to be in the picklist — so a plausible-sounding
but wrong conclusion is rejected before it can reach a mapping config rather
than after a failed load.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from common.logger import logger

from agents import wiring
from agents.audit import SEVERITY_INFO
from agents.context import MigrationContext
from agents.wiring import Resolution

_SEVERITIES = {"info", "warning", "blocker"}
_SALESFORCE_ID = re.compile(r"^[a-zA-Z0-9]{15}$|^[a-zA-Z0-9]{18}$")


@dataclass
class ValidationOutcome:
    accepted: list[Resolution] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    def reject(self, payload: dict, reason: str) -> None:
        self.rejected.append({"resolution": payload, "reason": reason})
        logger.warning("[agent] rejected resolution %s: %s", payload.get("kind"), reason)


def parse_submission(payload: dict) -> tuple[str, list[dict]]:
    """Split a submission into its summary and its raw resolution dicts."""
    if not isinstance(payload, dict):
        return "", []
    raw = payload.get("resolutions")
    if not isinstance(raw, list):
        return str(payload.get("summary") or ""), []
    return str(payload.get("summary") or ""), [item for item in raw if isinstance(item, dict)]


def _to_resolution(item: dict) -> Resolution:
    """Flatten the model-facing shape into the internal resolution."""
    severity = str(item.get("severity") or SEVERITY_INFO).lower()
    if severity not in _SEVERITIES:
        severity = SEVERITY_INFO

    params: dict = {}
    for key in (
        "parent_mappings",
        "source_field",
        "constant_value",
        "external_id_field",
        "skip_if_unresolved",
        "default",
    ):
        if item.get(key) not in (None, ""):
            params[key] = item[key]

    kind = str(item.get("kind") or "")
    if kind == wiring.KIND_VALUE_MAP:
        params["transform"] = "generic_value_map"
        params["params"] = {
            "map": item.get("value_map") or {},
            "default": item.get("value_map_default", ""),
            "case_insensitive": True,
        }
    elif kind == wiring.KIND_TYPE_COERCION:
        params["transform"] = item.get("transform", "")
        params["params"] = item.get("transform_params") or {}

    return Resolution(
        kind=kind,
        mapping=str(item.get("mapping") or ""),
        field=str(item.get("field") or ""),
        rationale=str(item.get("rationale") or ""),
        severity=severity,
        params=params,
    )


def validate(context: MigrationContext, raw_resolutions: list[dict]) -> ValidationOutcome:
    outcome = ValidationOutcome()
    for item in raw_resolutions:
        resolution = _to_resolution(item)

        if resolution.kind not in wiring.RESOLUTION_KINDS:
            outcome.reject(item, f"Unsupported resolution kind '{resolution.kind}'")
            continue
        if not resolution.rationale:
            outcome.reject(item, "A rationale is required for every resolution")
            continue

        if resolution.kind == wiring.KIND_NOTICE:
            if resolution.mapping and resolution.mapping not in context.mapping_names:
                outcome.reject(item, f"'{resolution.mapping}' is not in this migration")
                continue
            outcome.accepted.append(resolution)
            continue

        if resolution.mapping not in context.mapping_names:
            outcome.reject(item, f"'{resolution.mapping}' is not in this migration")
            continue

        destination_object = context.destination_object(resolution.mapping)
        if not context.destination_fields(destination_object):
            outcome.reject(
                item,
                f"{destination_object} could not be described, so this change cannot "
                "be schema-checked",
            )
            continue

        error = _validate_kind(context, resolution, destination_object)
        if error:
            outcome.reject(item, error)
            continue
        outcome.accepted.append(resolution)

    return outcome


def _validate_kind(
    context: MigrationContext, resolution: Resolution, destination_object: str
) -> str:
    """Return an empty string when the resolution is admissible."""
    config = context.configs[resolution.mapping]

    if resolution.kind == wiring.KIND_EXTERNAL_ID_UPSERT:
        external_id = resolution.params.get("external_id_field", "")
        if not external_id:
            return "external_id_upsert needs external_id_field"
        meta = context.destination_field(destination_object, external_id)
        if not meta:
            return f"'{external_id}' does not exist on {destination_object}"
        if not meta.get("id_lookup"):
            return (
                f"'{external_id}' is not usable as an upsert key; the field must be "
                "flagged External Id (and unique) in the destination"
            )
        return ""

    if not resolution.field:
        return f"{resolution.kind} needs a destination field"

    meta = context.destination_field(destination_object, resolution.field)
    if not meta:
        return f"'{resolution.field}' does not exist on {destination_object}"
    if not meta.get("createable") and config.get("operation") == "insert":
        return f"'{resolution.field}' is not writable on insert for {destination_object}"

    # A field that has no mapping yet can only be created if we are told which
    # source field feeds it.
    entry = wiring.field_entry(config, resolution.field)
    source_field = resolution.params.get("source_field", "")
    needs_source = resolution.kind in (
        wiring.KIND_LOOKUP_REFERENCE,
        wiring.KIND_SELF_REFERENCE_GUARD,
        wiring.KIND_TYPE_COERCION,
        wiring.KIND_VALUE_MAP,
    )
    if entry is None and needs_source and not source_field:
        return (
            f"'{resolution.field}' is not mapped yet, so source_field is required to "
            "create the mapping"
        )
    if source_field:
        table = context.driving_table(resolution.mapping)
        if table and not context.has_source_field(table, source_field):
            return f"'{source_field}' is not a field on source table '{table}'"

    if resolution.kind == wiring.KIND_LOOKUP_REFERENCE:
        if meta.get("type") != "reference":
            return (
                f"'{resolution.field}' is a {meta.get('type')} field, not a lookup, so "
                "resolving it from migrated ids makes no sense"
            )
        parents = resolution.params.get("parent_mappings") or []
        if not parents:
            return "lookup_reference needs at least one parent mapping"
        targets = set(meta.get("reference_to") or [])
        for parent in parents:
            if parent not in context.mapping_names:
                return f"parent mapping '{parent}' is not in this migration"
            if parent == resolution.mapping:
                return (
                    f"'{parent}' is the mapping being changed; use self_reference_guard "
                    "for a lookup onto its own object"
                )
            parent_object = context.destination_object(parent)
            if parent_object not in targets:
                return (
                    f"'{resolution.field}' points at {sorted(targets)}, so it cannot "
                    f"resolve against '{parent}' which loads {parent_object}"
                )
        return ""

    if resolution.kind == wiring.KIND_SELF_REFERENCE_GUARD:
        if meta.get("type") != "reference":
            return f"'{resolution.field}' is not a lookup field"
        if destination_object not in (meta.get("reference_to") or []):
            return (
                f"'{resolution.field}' does not point back at {destination_object}, so "
                "it is not a self-reference"
            )
        return ""

    if resolution.kind == wiring.KIND_CONSTANT_REFERENCE:
        constant = resolution.params.get("constant_value", "")
        if not constant:
            return "constant_reference needs constant_value"
        if meta.get("type") == "reference" and not _SALESFORCE_ID.match(str(constant)):
            return (
                f"'{constant}' is not a destination record id; a lookup pinned to a "
                "constant needs the 15- or 18-character id of an existing record"
            )
        return ""

    if resolution.kind in (wiring.KIND_TYPE_COERCION, wiring.KIND_VALUE_MAP):
        transform = resolution.params.get("transform", "")
        if transform not in wiring.TRANSFORM_ALLOW_LIST:
            return f"transform '{transform}' is not in the allow-list"
        if transform == "generic_lookup":
            return (
                "use lookup_reference instead of writing generic_lookup directly, so the "
                "id-mapping table is derived rather than guessed"
            )
        if meta.get("type") == "reference":
            return (
                f"'{resolution.field}' is a lookup; use lookup_reference or "
                "constant_reference rather than a value transform"
            )
        if resolution.kind == wiring.KIND_VALUE_MAP:
            value_map = (resolution.params.get("params") or {}).get("map") or {}
            if not value_map:
                return "value_map needs at least one source value to map"
            allowed = set(meta.get("picklist_values") or [])
            if allowed:
                unknown = sorted(
                    {
                        str(target)
                        for target in value_map.values()
                        if target and str(target) not in allowed
                    }
                )
                if unknown:
                    return (
                        f"{unknown} are not valid values for picklist "
                        f"'{resolution.field}'"
                    )
        return ""

    return f"No validation rule for kind '{resolution.kind}'"
