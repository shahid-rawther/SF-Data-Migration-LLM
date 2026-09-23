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
# The same mistake as RAW_REFERENCE, on a destination field the API does not call
# a reference. Separate because the consequence is the opposite: a real lookup
# rejects the row loudly, a text column accepts it and says nothing.
RAW_ID_IN_TEXT = "raw_id_in_text"
UNMAPPED_REQUIRED_REFERENCE = "unmapped_required_reference"
MISSING_PARENT = "missing_parent"
LOOKUP_TABLE_MISMATCH = "lookup_table_mismatch"
UNRESOLVABLE_LOOKUP = "unresolvable_lookup"
LOOKUP_WIRED = "lookup_wired"
CONSTANT_OVER_MIGRATED_PARENT = "constant_over_migrated_parent"
SELF_REFERENCE = "self_reference"
JUNCTION_OBJECT = "junction_object"
ATTACHMENT_PARENT_LINK = "attachment_parent_link"
UNVERIFIED_TRANSFORM = "unverified_transform"
TRANSFORM_MISSING = "transform_missing"
TYPE_MISMATCH = "type_mismatch"
PICKLIST_UNMAPPED = "picklist_unmapped"
NO_EXTERNAL_ID = "no_external_id"
DEPENDENCY_CYCLE = "dependency_cycle"
DEFERRABLE_CYCLE = "deferrable_cycle"
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
    # Mappings that load those objects, i.e. the wiring candidates. Selected
    # ones, except on a deferrable_cycle, where the point is that they are not.
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
        mapped_fields = {
            entry.get("destination_field", ""): entry
            for entry in entries
            if entry.get("destination_field")
        }
        mapped_destinations = set(mapped_fields)
        driving_fields = context.source_fields(context.driving_table(name))
        source_types = {
            source_field["name"]: source_field["type"]
            for source_field in driving_fields
        }
        # Source columns that hold a pointer rather than a value, and what they
        # point at. The destination cannot reveal this: a text field receiving one
        # of these looks like any other text field.
        source_refs = {
            source_field["name"]: source_field.get("reference_table") or ""
            for source_field in driving_fields
            if source_field.get("reference_table")
        }
        parent_link_count = 0

        mapping_source_type = wiring.source_type_of(name, config)

        for entry in entries:
            destination_field = entry.get("destination_field", "")

            # Checked before the describe guard below, because the transform runs
            # whether or not the destination field exists yet.
            findings.extend(
                _inspect_transform_exists(name, entry, mapping_source_type)
            )

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
                _inspect_scalar(name, entry, meta, source_types, mapped_fields)
            )
            findings.extend(
                _inspect_raw_id_in_text(name, config, entry, meta, source_refs)
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

    findings.extend(
        _inspect_absent_cycle_partner(
            context, name, destination_object, entry, external_targets, candidates, current
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
        # A table already holding ids resolves regardless of what this run loads:
        # the parent may have been migrated in an earlier session, or the table
        # may be maintained outside a migration altogether, as the catalog-item
        # to product sync does. So "no selected mapping writes it" is not on its
        # own proof that the lookup cannot resolve.
        filled = _populated_lookup_tables(actual)
        if not actual or not (actual & (expected | filled)):
            unresolvable = _unresolvable_lookup(
                context,
                name,
                destination_field,
                entry,
                meta,
                external_targets,
                actual,
                current,
            )
            if unresolvable:
                findings.append(unresolvable)
        elif expected and not (actual & expected):
            findings.append(
                Finding(
                    kind=LOOKUP_TABLE_MISMATCH,
                    mapping=name,
                    field=destination_field,
                    severity=SEVERITY_BLOCKER,
                    detail=(
                        f"'{destination_field}' resolves against {sorted(actual)}, but the "
                        f"parent in this migration writes its ids under {sorted(expected)}. "
                        "The configured table does hold ids from elsewhere, so this fails "
                        "selectively rather than outright: rows whose parent is migrated "
                        "now will not be found, while older ones will."
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


def _inspect_transform_exists(
    name: str, entry: dict, source_type: str
) -> list[Finding]:
    """A named transform that cannot be resolved for this mapping's source.

    Transforms live in a shared module and in one module per source system, and
    a mapping can only reach its own system's. Naming another's — usually from a
    mapping copied between source systems — raises no complaint until the
    transform runs, which is after the extract has already been paid for, and
    then it fails the whole batch rather than the one field.
    """
    from services.internal.transformation_service import transform_exists

    func_name = entry.get("transformation_function") or ""
    if not func_name or transform_exists(func_name, source_type):
        return []

    return [
        Finding(
            kind=TRANSFORM_MISSING,
            mapping=name,
            field=entry.get("destination_field", ""),
            severity=SEVERITY_BLOCKER,
            detail=(
                f"'{entry.get('destination_field', '')}' is transformed by "
                f"'{func_name}', which does not exist for a {source_type} "
                "mapping. Every batch fails on it once the transform starts. "
                "Pick a transform this source system provides, or map the field "
                "straight across."
            ),
            current=f"transform:{func_name}",
            blocking=True,
        )
    ]


def _populated_lookup_tables(tables: set[str]) -> set[str]:
    """Which of ``tables`` already hold ids in ``id_mappings``."""
    if not tables:
        return set()
    from services.external.db_client import db_client

    ordered = sorted(tables)
    placeholders = ", ".join("?" for _ in ordered)
    rows = db_client.fetch_all(
        f"SELECT DISTINCT table_name FROM id_mappings "
        f"WHERE table_name IN ({placeholders})",
        tuple(ordered),
    )
    return {row[0] for row in rows}


def _unresolvable_lookup(
    context: MigrationContext,
    name: str,
    destination_field: str,
    entry: dict,
    meta: dict,
    external_targets: list[str],
    actual: set[str],
    current: str,
) -> Finding | None:
    """A lookup with no id source at all, in this run or from an earlier one.

    This is the failure the other lookup checks miss between them.
    ``missing_parent`` only covers required fields, and ``lookup_table_mismatch``
    compares the configured table against the one a selected parent writes —
    which needs a selected parent to compare against, so when nothing loads the
    referenced object there is nothing to disagree with and the field was
    reported as correctly wired.

    It is the more damaging case, not the milder one. A mismatch resolves to
    empty; this resolves to empty *and*, when the field is set to skip rows it
    cannot attach, discards the record outright — so a whole table can extract,
    transform to nothing and report no failure at all.

    Returns ``None`` for a field already deferred to backfill whose parent can
    still be loaded, because that field is *meant* to be empty for now and the
    deferral is already reported before launch. Deferral with no parent to wait
    for is a different matter and is still raised: it never completes.
    """
    targets = ", ".join(external_targets) or "another object"
    migratable = sorted(
        {
            mapping
            for target in external_targets
            for mapping in context.available_mappings_for_object(target)
        }
    )
    if entry.get("defer_to_backfill") and migratable:
        return None
    deferred = bool(entry.get("defer_to_backfill"))
    skips_rows = bool(entry.get("skip_if_not_found"))
    required = bool(meta.get("required"))

    if deferred and not required:
        # Deferred with nothing yet able to supply the ids. Still worth saying,
        # because records are accumulating against an object nobody has built a
        # mapping for — but not in the terms below, which describe a link being
        # lost. Here it is kept, and building that mapping is what redeems it, so
        # advising the operator to pin or unmap the field would talk them out of
        # the arrangement that preserves their data.
        return Finding(
            kind=UNRESOLVABLE_LOOKUP,
            mapping=name,
            field=destination_field,
            severity=SEVERITY_WARNING,
            detail=(
                f"'{destination_field}' waits on {targets}, which nothing in the "
                "catalog migrates yet. Every record loads with it empty, and each "
                "one is recorded so the reference can be attached later. Build a "
                f"mapping for {targets} and run it, and you will be offered the "
                "fill; until then these records stay unlinked."
            ),
            references=external_targets,
            candidate_parents=migratable,
            current=current,
            blocking=False,
        )

    if not actual:
        cause = (
            f"'{destination_field}' is set to resolve through a lookup but names no "
            "id-mapping table, so there is nothing to resolve against."
        )
    else:
        cause = (
            f"'{destination_field}' resolves against {sorted(actual)}, which holds no "
            f"ids: nothing in this migration loads {targets}, and no earlier run left "
            "a mapping behind."
        )

    if skips_rows:
        consequence = (
            " Because the field skips rows it cannot attach, every record is "
            "discarded during transformation rather than loaded with the field empty."
        )
    elif required:
        consequence = (
            " The destination requires the field, so every record is rejected on load."
        )
    else:
        consequence = " Every record loads with the field empty."

    if migratable:
        remedy = (
            f" {', '.join(migratable)} loads {targets} and can be added to this "
            "migration, or the field can be deferred so the rows are kept and "
            "attached once it has."
        )
    else:
        remedy = (
            f" No mapping loads {targets}, so migrating it is not an option here: "
            "pin the field to a fixed record, point it at an object this run does "
            "load, or leave it unmapped."
        )

    return Finding(
        kind=UNRESOLVABLE_LOOKUP,
        mapping=name,
        field=destination_field,
        severity=SEVERITY_BLOCKER if (skips_rows or required) else SEVERITY_WARNING,
        detail=cause + consequence + remedy,
        references=external_targets,
        candidate_parents=migratable,
        current=current,
        blocking=skips_rows or required,
    )


def _inspect_absent_cycle_partner(
    context: MigrationContext,
    name: str,
    destination_object: str,
    entry: dict,
    external_targets: list[str],
    candidates: list[str],
    current: str,
) -> list[Finding]:
    """A lookup onto a mutually-referencing object that this run does not cover.

    With both ends selected the cycle is visible in the dependency graph and
    reported as ``dependency_cycle``. With only one end selected there is no
    cycle to find — the graph has a single node — and the reference is quietly
    left empty on every row.

    That silence is the problem. The object on the other end has a mapping and
    can be added to the session later, so the rows are worth recording now: the
    field is deferred, the unattached rows are kept, and the fill is offered once
    the other side has loaded. Without this the reference is lost, because
    nothing remembers which rows needed it.
    """
    if candidates or not external_targets:
        return []
    if entry.get("defer_to_backfill"):
        return []

    waiting_on: list[str] = []
    parents: list[str] = []
    for target in external_targets:
        available = context.available_mappings_for_object(target)
        if not available:
            continue
        if not context.references_back(target, destination_object):
            continue
        waiting_on.append(target)
        parents.extend(available)

    if not parents:
        return []

    return [
        Finding(
            kind=DEFERRABLE_CYCLE,
            mapping=name,
            field=entry.get("destination_field", ""),
            severity=SEVERITY_WARNING,
            detail=(
                f"'{entry.get('destination_field', '')}' points at "
                f"{', '.join(waiting_on)}, which references {destination_object} "
                "back, so neither can load first with this reference filled. "
                f"{', '.join(waiting_on)} is not in this migration, but "
                f"{', '.join(dict.fromkeys(parents))} can load it later. Deferring "
                "records the rows now so the reference can be attached once it has."
            ),
            references=waiting_on,
            candidate_parents=list(dict.fromkeys(parents)),
            current=current,
        )
    ]


def _inspect_picklist(
    name: str,
    entry: dict,
    meta: dict,
    mapped_fields: dict[str, dict],
) -> list[Finding]:
    """Flag picklist mappings the destination will not accept.

    Three things go wrong here and they are worth telling apart. A value the
    field does not define is the familiar case. A value the field does define
    but the chosen record type does not offer looks correct in every describe
    and still fails. And a dependent picklist is judged against its controller,
    so a child value can be valid in isolation and rejected in context.

    A restricted picklist rejects the row outright; an unrestricted one stores
    whatever it is handed, which is a data-quality problem rather than a failed
    load. Both are reported, at different severities, because a silent success
    that fills a column with values nobody can filter on is its own trap.
    """
    findings: list[Finding] = []
    destination_field = meta["name"]
    transform = entry.get("transformation_function") or ""
    source_field = entry.get("source_field") or ""
    restricted = bool(meta.get("picklist_restricted"))
    severity = SEVERITY_BLOCKER if restricted else SEVERITY_WARNING
    consequence = (
        "those rows are rejected"
        if restricted
        else "the source value is written through unchanged, off the picklist"
    )
    current = _describe_value_source(entry)

    # A value map pointing at something this record type does not offer fails
    # however carefully it was checked against the object describe.
    unavailable = set(meta.get("picklist_unavailable") or [])
    if unavailable:
        params = entry.get("transformation_params") or {}
        value_map = params.get("map") if isinstance(params, dict) else {}
        targets = set()
        if isinstance(value_map, dict):
            targets = {str(v) for v in value_map.values() if v}
        constant = entry.get("constant_value")
        if constant not in (None, ""):
            targets.add(str(constant))
        offending = sorted(targets & unavailable)
        if offending:
            findings.append(
                Finding(
                    kind=PICKLIST_UNMAPPED,
                    mapping=name,
                    field=destination_field,
                    severity=SEVERITY_BLOCKER,
                    detail=(
                        f"'{destination_field}' is set to {', '.join(offending)}, which "
                        "the field defines but the record type this mapping loads with "
                        "does not offer. Those rows are rejected. Valid choices here are "
                        f"{', '.join((meta.get('picklist_values') or [])[:8])}."
                    ),
                    current=current,
                )
            )

    # A dependent picklist is only meaningful next to its controller.
    controller = meta.get("picklist_controller") or ""
    if controller and controller not in mapped_fields:
        findings.append(
            Finding(
                kind=PICKLIST_UNMAPPED,
                mapping=name,
                field=destination_field,
                severity=severity,
                detail=(
                    f"'{destination_field}' is controlled by '{controller}', which this "
                    f"mapping does not set. A value here is only accepted alongside a "
                    f"'{controller}' it is valid for; with the controller left to its "
                    f"default, {consequence}."
                ),
                current=current,
            )
        )

    if not source_field or _has_constant(entry):
        return findings

    params = entry.get("transformation_params") or {}
    value_map = params.get("map") if isinstance(params, dict) else None
    if transform == "generic_value_map" and not value_map:
        findings.append(
            Finding(
                kind=PICKLIST_UNMAPPED,
                mapping=name,
                field=destination_field,
                severity=severity,
                detail=(
                    f"'{destination_field}' is a picklist with an empty value map, so no "
                    f"source value reaches a valid entry and {consequence}."
                ),
                current=current,
            )
        )
    elif not transform:
        findings.append(
            Finding(
                kind=PICKLIST_UNMAPPED,
                mapping=name,
                field=destination_field,
                severity=severity,
                detail=(
                    f"'{destination_field}' is a picklist fed by a raw copy. Where the "
                    f"source value is not already one of "
                    f"({', '.join((meta.get('picklist_values') or [])[:5])}...), "
                    f"{consequence}."
                ),
                current=current,
            )
        )
    return findings


_TEXT_TYPES = {"string", "textarea"}


def _inspect_raw_id_in_text(
    name: str,
    config: dict,
    entry: dict,
    meta: dict,
    source_refs: dict[str, str],
) -> list[Finding]:
    """A destination text field fed a source column that is a pointer, not a value.

    Every other lookup check starts from the destination calling a field a
    reference. Nothing here does: the destination calls this a text box, and a
    text box legitimately holds any text at all, so no rule fires and the editor
    offers no lookup option. The source is the only side that knows the value is
    a pointer, which is why this check reads the source schema instead.

    It is the quietest failure of the set. A real lookup rejects a source id with
    ``MALFORMED_ID`` and the row fails visibly; a 255-character text column
    accepts a 32-character id, reports success, and leaves a value that cannot be
    followed, filtered or reported on, and that nothing keeps in step with the
    record it names. The load looks clean, so nobody goes looking.

    Not blocking. The load genuinely succeeds, and keeping the source id as a
    deliberate traceability column is a real choice — so this states the cost and
    leaves the decision alone.
    """
    destination_field = meta["name"]
    if meta.get("type") not in _TEXT_TYPES:
        return []
    # Holding the source id is this field's entire purpose. Read through the
    # config helper, since the key has a legacy spelling as well.
    from services.internal.mappings_service import cfg_external_id_field

    if cfg_external_id_field(config) == destination_field:
        return []
    source_field = entry.get("source_field") or ""
    if not source_field or _has_constant(entry):
        return []
    transform = (entry.get("transformation_function") or "").lower()
    if "lookup" in transform:
        # Already resolving through an id map, whatever the destination type.
        return []

    target_table = source_refs.get(source_field) or source_refs.get(
        source_field.split(".")[-1]
    )
    if not target_table:
        return []

    from services.internal.mappings_service import destinations_for_source_table

    migrated = [
        d
        for d in destinations_for_source_table(target_table)
        if d["destination_object"]
    ]
    if migrated:
        best = migrated[0]
        held = (
            f"{best['migrated_count']:,} already migrated"
            if best["migrated_count"]
            else "none migrated yet"
        )
        remedy = (
            f" {target_table} is migrated to {best['destination_object']} by "
            f"'{best['mapping_name']}' ({held}), so there is a real id this could "
            f"hold. Making '{destination_field}' a lookup to "
            f"{best['destination_object']} in the destination would let it resolve "
            "to that id, and be deferred and filled later when it cannot. Keeping "
            "it as text is a valid choice, but only as a deliberate record of "
            "where the row came from."
        )
    else:
        remedy = (
            f" Nothing migrates {target_table}, so there is no destination id this "
            "could resolve to yet. Either migrate it, or keep the column as a "
            "deliberate record of the source value rather than a working link."
        )

    return [
        Finding(
            kind=RAW_ID_IN_TEXT,
            mapping=name,
            field=destination_field,
            severity=SEVERITY_WARNING,
            detail=(
                f"'{destination_field}' is a text field, and '{source_field}' is a "
                f"pointer to {target_table}, so the raw source id is stored as text. "
                "The destination accepts it and reports no error, so this does not "
                "fail — but the value cannot be followed or reported on, and nothing "
                "keeps it in step with the record it names." + remedy
            ),
            current=_describe_value_source(entry),
            blocking=False,
        )
    ]


def _inspect_scalar(
    name: str,
    entry: dict,
    meta: dict,
    source_types: dict[str, str],
    mapped_fields: dict[str, dict],
) -> list[Finding]:
    """Flag datatype and picklist problems on a non-lookup field."""
    from api.compat_engine import get_type_coercion

    findings: list[Finding] = []
    destination_field = meta["name"]
    transform = entry.get("transformation_function") or ""
    source_field = entry.get("source_field") or ""

    if meta.get("picklist_values"):
        findings.extend(_inspect_picklist(name, entry, meta, mapped_fields))

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
