"""Mechanical detection of relationship and datatype edge cases.

This is deliberately not the agent's job. Finding *which* fields are suspect is
a schema walk with an exact answer, so it runs deterministically here and the
agent is handed the list; the agent's contribution is deciding what each one
should become. The same findings drive the launch gate in :mod:`agents.preflight`,
which means the check that blocks a migration is the check the agent was asked
to resolve, not a second opinion that can drift from it.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

from agents import wiring
from agents.audit import SEVERITY_BLOCKER, SEVERITY_INFO, SEVERITY_WARNING
from agents.context import MigrationContext

# Findings, in the order the catalog in the architecture reference lists them.
RAW_REFERENCE = "raw_reference"
UNMAPPED_REQUIRED_REFERENCE = "unmapped_required_reference"
MISSING_PARENT = "missing_parent"
LOOKUP_TABLE_MISMATCH = "lookup_table_mismatch"
LOOKUP_WIRED = "lookup_wired"
CONSTANT_OVER_MIGRATED_PARENT = "constant_over_migrated_parent"
SELF_REFERENCE = "self_reference"
JUNCTION_OBJECT = "junction_object"
ATTACHMENT_PARENT_LINK = "attachment_parent_link"
UNVERIFIED_TRANSFORM = "unverified_transform"
TYPE_MISMATCH = "type_mismatch"
PICKLIST_UNMAPPED = "picklist_unmapped"
NO_EXTERNAL_ID = "no_external_id"
DEPENDENCY_CYCLE = "dependency_cycle"
SCHEMA_UNAVAILABLE = "schema_unavailable"


@dataclass
class Finding:
    kind: str
    mapping: str
    field: str = ""
    severity: str = SEVERITY_INFO
    detail: str = ""
    # Destination objects the field points at.
    references: list[str] = dataclass_field(default_factory=list)
    # Selected mappings that load those objects, i.e. the wiring candidates.
    candidate_parents: list[str] = dataclass_field(default_factory=list)
    current: str = ""
    # True when the migration must not launch until this is resolved.
    blocking: bool = False

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "mapping": self.mapping,
            "field": self.field,
            "severity": self.severity,
            "detail": self.detail,
            "references": self.references,
            "candidate_parents": self.candidate_parents,
            "current": self.current,
            "blocking": self.blocking,
        }


def _has_constant(entry: dict) -> bool:
    return entry.get("constant_value") is not None or bool(entry.get("use_constant"))


def _describe_value_source(entry: dict) -> str:
    if entry.get("transformation_function"):
        return f"transform:{entry['transformation_function']}"
    if _has_constant(entry):
        return "constant"
    if entry.get("source_field"):
        return f"copy:{entry['source_field']}"
    return "unset"


def _lookup_tables_of(entry: dict) -> list[str]:
    params = entry.get("transformation_params") or {}
    if not isinstance(params, dict):
        return []
    tables = params.get("lookup_tables")
    if tables:
        return [t for t in tables if t]
    single = params.get("lookup_table")
    return [single] if single else []


def detect(
    context: MigrationContext,
    draft_configs: dict[str, dict] | None = None,
) -> list[Finding]:
    """Walk the selected mappings against live destination metadata."""
    configs = draft_configs if draft_configs is not None else context.configs
    findings: list[Finding] = []

    object_to_mapping = {
        context.destination_object(name): name
        for name in context.mapping_names
        if context.destination_object(name)
    }

    for name in context.mapping_names:
        config = configs.get(name)
        if not config:
            continue
        destination_object = context.destination_object(name)
        if not destination_object:
            continue

        destination_fields = context.destination_fields(destination_object)
        if not destination_fields:
            findings.append(
                Finding(
                    kind=SCHEMA_UNAVAILABLE,
                    mapping=name,
                    severity=SEVERITY_WARNING,
                    detail=(
                        f"{destination_object} could not be described, so this "
                        "mapping's relationships cannot be verified. Check the "
                        "destination credentials and object permissions."
                    ),
                    references=[destination_object],
                )
            )
            continue

        by_name = {entry["name"]: entry for entry in destination_fields}
        entries = config.get("fields") or []
        mapped_destinations = {
            entry.get("destination_field", "") for entry in entries
        }
        source_types = {
            source_field["name"]: source_field["type"]
            for source_field in context.source_fields(context.driving_table(name))
        }
        parent_link_count = 0

        for entry in entries:
            destination_field = entry.get("destination_field", "")
            meta = by_name.get(destination_field)
            if not meta:
                # Custom field that does not exist yet; the compatibility report
                # already covers creating it.
                continue

            if meta.get("type") == "reference":
                findings.extend(
                    _inspect_reference(
                        context, configs, name, destination_object, entry, meta, object_to_mapping
                    )
                )
                if any(
                    object_to_mapping.get(target) not in (None, name)
                    for target in meta.get("reference_to") or []
                ):
                    parent_link_count += 1
                continue

            findings.extend(
                _inspect_scalar(name, entry, meta, source_types)
            )

        # Required lookups that were never mapped at all.
        for meta in destination_fields:
            if meta["name"] in mapped_destinations:
                continue
            if meta.get("type") != "reference":
                continue
            if not (meta.get("required") and meta.get("createable")):
                continue
            targets = [t for t in meta.get("reference_to") or [] if t != destination_object]
            candidates = [object_to_mapping[t] for t in targets if t in object_to_mapping]
            if candidates:
                findings.append(
                    Finding(
                        kind=UNMAPPED_REQUIRED_REFERENCE,
                        mapping=name,
                        field=meta["name"],
                        severity=SEVERITY_BLOCKER,
                        detail=(
                            f"Required lookup '{meta['name']}' on {destination_object} "
                            f"has no field mapping, but {', '.join(candidates)} loads "
                            "the object it points at, so it can be resolved from that "
                            "parent's ids."
                        ),
                        references=targets,
                        candidate_parents=candidates,
                        current="unset",
                        blocking=True,
                    )
                )
            else:
                findings.append(
                    Finding(
                        kind=MISSING_PARENT,
                        mapping=name,
                        field=meta["name"],
                        severity=SEVERITY_BLOCKER,
                        detail=(
                            f"Required lookup '{meta['name']}' on {destination_object} "
                            f"points at {', '.join(targets) or 'an object'} which is not "
                            "part of this migration and has no value set. Either include "
                            "a mapping for it or pin the field to an existing record."
                        ),
                        references=targets,
                        current="unset",
                        blocking=True,
                    )
                )

        if parent_link_count >= 2:
            findings.append(
                Finding(
                    kind=JUNCTION_OBJECT,
                    mapping=name,
                    severity=SEVERITY_INFO,
                    detail=(
                        f"{name} links {parent_link_count} migrated parents, so it must "
                        "load after all of them rather than after just one."
                    ),
                )
            )

        if config.get("is_attachment_table"):
            findings.append(
                Finding(
                    kind=ATTACHMENT_PARENT_LINK,
                    mapping=name,
                    severity=SEVERITY_INFO,
                    detail=(
                        f"{name} carries attachments, whose parent link must resolve "
                        "to the new destination id of the record they hang off."
                    ),
                )
            )

        if config.get("operation") == "insert" and not config.get("external_id_field"):
            findings.append(
                Finding(
                    kind=NO_EXTERNAL_ID,
                    mapping=name,
                    severity=SEVERITY_INFO,
                    detail=(
                        f"{name} inserts without an external id, so a re-run after the "
                        "local id table is cleared would create duplicates instead of "
                        "updating what is already there."
                    ),
                )
            )

    analysis = context.dependency_analysis(draft_configs=configs)
    if analysis.get("has_cycle"):
        findings.append(
            Finding(
                kind=DEPENDENCY_CYCLE,
                mapping="",
                severity=SEVERITY_WARNING,
                detail=(
                    "The selected mappings reference each other in a cycle, so no "
                    "parent-first order exists. One side has to load first and be "
                    "attached by a follow-up update."
                ),
                references=analysis.get("order", []),
            )
        )

    return findings


def _inspect_reference(
    context: MigrationContext,
    configs: dict[str, dict],
    name: str,
    destination_object: str,
    entry: dict,
    meta: dict,
    object_to_mapping: dict[str, str],
) -> list[Finding]:
    """Classify one mapped lookup field."""
    findings: list[Finding] = []
    destination_field = meta["name"]
    targets = meta.get("reference_to") or []
    external_targets = [t for t in targets if t != destination_object]
    candidates = [object_to_mapping[t] for t in external_targets if t in object_to_mapping]
    is_self_reference = destination_object in targets
    transform = entry.get("transformation_function") or ""
    current = _describe_value_source(entry)

    if is_self_reference:
        findings.append(
            Finding(
                kind=SELF_REFERENCE,
                mapping=name,
                field=destination_field,
                severity=SEVERITY_WARNING,
                detail=(
                    f"'{destination_field}' points back at {destination_object}, so a "
                    "parent row may not exist yet when its child loads."
                ),
                references=targets,
                candidate_parents=[name],
                current=current,
            )
        )

    if transform == "generic_lookup":
        expected = {
            wiring.lookup_table_name(configs.get(parent) or {})
            for parent in candidates
        }
        if is_self_reference:
            expected.add(wiring.lookup_table_name(configs.get(name) or {}))
        expected.discard("")
        actual = set(_lookup_tables_of(entry))
        if expected and actual and not (actual & expected):
            findings.append(
                Finding(
                    kind=LOOKUP_TABLE_MISMATCH,
                    mapping=name,
                    field=destination_field,
                    severity=SEVERITY_BLOCKER,
                    detail=(
                        f"'{destination_field}' resolves against {sorted(actual)}, but the "
                        f"parent load writes its ids under {sorted(expected)}. Every row "
                        "would silently get an empty lookup."
                    ),
                    references=external_targets,
                    candidate_parents=candidates,
                    current=current,
                    blocking=True,
                )
            )
        else:
            findings.append(
                Finding(
                    kind=LOOKUP_WIRED,
                    mapping=name,
                    field=destination_field,
                    severity=SEVERITY_INFO,
                    detail=f"'{destination_field}' already resolves through {sorted(actual)}.",
                    references=external_targets,
                    candidate_parents=candidates,
                    current=current,
                )
            )
        return findings

    if _has_constant(entry):
        if candidates:
            findings.append(
                Finding(
                    kind=CONSTANT_OVER_MIGRATED_PARENT,
                    mapping=name,
                    field=destination_field,
                    severity=SEVERITY_WARNING,
                    detail=(
                        f"'{destination_field}' is pinned to a fixed record even though "
                        f"{', '.join(candidates)} migrates the object it points at. Every "
                        "row will share one parent unless that is intended."
                    ),
                    references=external_targets,
                    candidate_parents=candidates,
                    current=current,
                )
            )
        return findings

    if transform:
        findings.append(
            Finding(
                kind=UNVERIFIED_TRANSFORM,
                mapping=name,
                field=destination_field,
                severity=SEVERITY_INFO,
                detail=(
                    f"'{destination_field}' resolves through the bespoke transform "
                    f"'{transform}', which this check cannot verify."
                ),
                references=external_targets,
                candidate_parents=candidates,
                current=current,
            )
        )
        return findings

    if entry.get("source_field"):
        findings.append(
            Finding(
                kind=RAW_REFERENCE,
                mapping=name,
                field=destination_field,
                severity=SEVERITY_BLOCKER,
                detail=(
                    f"'{destination_field}' copies the source id in "
                    f"'{entry.get('source_field')}' straight into a lookup. The "
                    "destination assigns its own ids, so every row fails with "
                    "MALFORMED_ID."
                ),
                references=external_targets,
                candidate_parents=candidates,
                current=current,
                blocking=True,
            )
        )
    elif meta.get("required"):
        findings.append(
            Finding(
                kind=MISSING_PARENT,
                mapping=name,
                field=destination_field,
                severity=SEVERITY_BLOCKER,
                detail=(
                    f"Required lookup '{destination_field}' has no value of any kind."
                ),
                references=external_targets,
                candidate_parents=candidates,
                current=current,
                blocking=True,
            )
        )
    return findings


def _inspect_scalar(
    name: str,
    entry: dict,
    meta: dict,
    source_types: dict[str, str],
) -> list[Finding]:
    """Flag datatype and picklist problems on a non-lookup field."""
    from api.compat_engine import get_type_coercion

    findings: list[Finding] = []
    destination_field = meta["name"]
    transform = entry.get("transformation_function") or ""
    source_field = entry.get("source_field") or ""

    if meta.get("picklist_values") and source_field and not _has_constant(entry):
        params = entry.get("transformation_params") or {}
        value_map = params.get("map") if isinstance(params, dict) else None
        if transform == "generic_value_map" and not value_map:
            findings.append(
                Finding(
                    kind=PICKLIST_UNMAPPED,
                    mapping=name,
                    field=destination_field,
                    severity=SEVERITY_WARNING,
                    detail=(
                        f"'{destination_field}' is a picklist with an empty value map, so "
                        "source values will be blanked."
                    ),
                    current=_describe_value_source(entry),
                )
            )
        elif not transform:
            findings.append(
                Finding(
                    kind=PICKLIST_UNMAPPED,
                    mapping=name,
                    field=destination_field,
                    severity=SEVERITY_WARNING,
                    detail=(
                        f"'{destination_field}' is a picklist fed by a raw copy; values "
                        f"that are not already valid picklist entries "
                        f"({', '.join((meta.get('picklist_values') or [])[:5])}...) will be "
                        "blanked."
                    ),
                    current=_describe_value_source(entry),
                )
            )

    if transform or not source_field or _has_constant(entry):
        return findings

    bare = source_field.split(".")[-1] if isinstance(source_field, str) else ""
    source_type = source_types.get(bare) or source_types.get(source_field, "")
    if not source_type:
        return findings

    coercion = get_type_coercion(source_type, meta.get("type", ""))
    if coercion.get("compatible"):
        return findings

    findings.append(
        Finding(
            kind=TYPE_MISMATCH,
            mapping=name,
            field=destination_field,
            severity=SEVERITY_WARNING,
            detail=coercion.get("warning", "Type mismatch."),
            current=_describe_value_source(entry),
        )
    )
    return findings


def blocking(findings: list[Finding]) -> list[Finding]:
    return [finding for finding in findings if finding.blocking]
