"""Deterministic application of agent resolutions to mapping configs.

Nothing an agent decides reaches a mapping config except through this module,
which gives the layer three properties worth stating plainly:

* the id-mapping table name a lookup reads from is **computed here**, with the
  same call the loader uses when it writes those rows, so the agent can never
  point a lookup at a table that will not exist;
* only transforms in :data:`TRANSFORM_ALLOW_LIST` can ever be written;
* every mutation returns a :class:`~agents.audit.ChangeRecord` carrying the value
  it replaced, so it can be reverted individually.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field as dataclass_field

from common.logger import logger
from services.external.db_client import db_client
from services.internal import mappings_service
from services.internal.mappings_service import (
    cfg_destination_object,
    cfg_source_tables,
    get_id_mapping_table_name,
)
from services.transforms.generic import GENERIC_TRANSFORMS

from agents.audit import (
    SEVERITY_INFO,
    TARGET_CONFIG,
    TARGET_FIELD,
    ChangeRecord,
)

# The only transforms an agent may write into a mapping. Every one is
# schema-driven and configured entirely through params, so a patched mapping
# stays declarative.
TRANSFORM_ALLOW_LIST = frozenset(GENERIC_TRANSFORMS)

AGENT_RELATIONSHIP = "relationship"
AGENT_REPAIR = "repair"
AGENT_ASSISTANT = "assistant"

# Resolution kinds. Each maps to one applier below; anything else is rejected
# by the validator before it gets here.
KIND_LOOKUP_REFERENCE = "lookup_reference"
KIND_DEFERRED_LOOKUP = "deferred_lookup"
KIND_CONSTANT_REFERENCE = "constant_reference"
KIND_TYPE_COERCION = "type_coercion"
KIND_VALUE_MAP = "value_map"
KIND_EXTERNAL_ID_UPSERT = "external_id_upsert"
KIND_SELF_REFERENCE_GUARD = "self_reference_guard"
KIND_REMOVE_FIELD = "remove_field"
KIND_NOTICE = "notice"

RESOLUTION_KINDS = frozenset(
    {
        KIND_LOOKUP_REFERENCE,
        KIND_DEFERRED_LOOKUP,
        KIND_CONSTANT_REFERENCE,
        KIND_TYPE_COERCION,
        KIND_VALUE_MAP,
        KIND_EXTERNAL_ID_UPSERT,
        KIND_SELF_REFERENCE_GUARD,
        KIND_REMOVE_FIELD,
        KIND_NOTICE,
    }
)

# Kinds that only inform the expert; they never touch a config.
ADVISORY_KINDS = frozenset({KIND_NOTICE})


@dataclass
class Resolution:
    """One decision the agent reached about one field (or one mapping)."""

    kind: str
    mapping: str
    field: str = ""
    rationale: str = ""
    severity: str = SEVERITY_INFO
    params: dict = dataclass_field(default_factory=dict)

    @property
    def is_advisory(self) -> bool:
        return self.kind in ADVISORY_KINDS

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "mapping": self.mapping,
            "field": self.field,
            "rationale": self.rationale,
            "severity": self.severity,
            "params": self.params,
        }


# ── config helpers ─────────────────────────────────────────────────────────────


def lookup_table_name(config: dict) -> str:
    """The ``id_mappings.table_name`` a parent mapping writes its ids under.

    Delegates to the same helper the loader calls, so the name a child lookup
    reads is the name the parent load actually produced. Edge cases that used to
    be diagnosed as "wrong lookup table" cannot arise from this path.
    """
    source_tables = cfg_source_tables(config)
    if not source_tables:
        return ""
    return get_id_mapping_table_name(
        source_tables[0],
        cfg_destination_object(config),
        config.get("transformation_name"),
    )


def field_entry(config: dict, destination_field: str) -> dict | None:
    """The field mapping for one destination field, or None when unmapped."""
    for entry in config.get("fields") or []:
        if entry.get("destination_field") == destination_field:
            return entry
    return None


def source_type_of(name: str, config: dict) -> str:
    """Resolve the source type for a mapping, preferring the config's own value."""
    declared = config.get("source_type")
    if declared:
        return declared
    try:
        row = db_client.fetch_one(
            "SELECT source_type FROM mapping_configs WHERE name = ?", (name,)
        )
    except Exception:  # noqa: BLE001 — a missing row is not an error here
        row = None
    return row[0] if row and row[0] else "servicenow"


def load_configs(mapping_names: list[str]) -> dict[str, dict]:
    """Deep copies of the stored configs, safe to patch as a draft."""
    configs: dict[str, dict] = {}
    for name in mapping_names:
        config = mappings_service.get_mapping_config(name)
        if config:
            configs[name] = copy.deepcopy(config)
    return configs


# ── appliers ───────────────────────────────────────────────────────────────────


def _ensure_field(config: dict, destination_field: str, source_field: str) -> tuple[dict, dict | None]:
    """Return the field entry for a destination field, creating it if needed.

    The second element is the entry as it was before (None when created), which
    is what the journal stores so a revert can remove an added mapping.
    """
    existing = field_entry(config, destination_field)
    if existing is not None:
        return existing, copy.deepcopy(existing)
    entry = {"destination_field": destination_field, "source_field": source_field}
    config.setdefault("fields", []).append(entry)
    return entry, None


def _clear_value_sources(entry: dict) -> None:
    """Drop competing value sources so one resolution decides the field.

    The transformation service checks ``constant_value`` before
    ``transformation_function``, so leaving a stale constant behind would
    silently win over a lookup that was just wired.
    """
    entry.pop("constant_value", None)
    entry.pop("use_constant", None)


def _change(
    agent: str,
    resolution: Resolution,
    previous: object,
    new: object,
    target: str = TARGET_FIELD,
    field_name: str | None = None,
) -> ChangeRecord:
    return ChangeRecord(
        agent=agent,
        kind=resolution.kind,
        mapping_name=resolution.mapping,
        field_name=resolution.field if field_name is None else field_name,
        target=target,
        previous_value=previous,
        new_value=new,
        rationale=resolution.rationale,
        severity=resolution.severity,
    )


def _apply_lookup_reference(
    configs: dict[str, dict], resolution: Resolution, agent: str
) -> list[ChangeRecord]:
    config = configs[resolution.mapping]
    parents = resolution.params.get("parent_mappings") or []
    tables: list[str] = []
    for parent in parents:
        parent_config = configs.get(parent) or mappings_service.get_mapping_config(parent)
        if not parent_config:
            continue
        table = lookup_table_name(parent_config)
        if table and table not in tables:
            tables.append(table)
    if not tables:
        raise ValueError(
            f"No id-mapping table could be derived for parents {parents!r}; "
            "the parent mapping must declare a source table and destination object"
        )

    entry, previous = _ensure_field(
        config, resolution.field, resolution.params.get("source_field", "")
    )
    _clear_value_sources(entry)
    entry["transformation_function"] = "generic_lookup"
    entry["transformation_params"] = {
        "lookup_tables": tables,
        "default": resolution.params.get("default", ""),
    }
    if resolution.params.get("skip_if_unresolved"):
        entry["skip_if_not_found"] = True
    else:
        entry.pop("skip_if_not_found", None)
    if resolution.params.get("source_field"):
        entry["source_field"] = resolution.params["source_field"]
    return [_change(agent, resolution, previous, copy.deepcopy(entry))]


def _apply_constant_reference(
    configs: dict[str, dict], resolution: Resolution, agent: str
) -> list[ChangeRecord]:
    config = configs[resolution.mapping]
    entry, previous = _ensure_field(config, resolution.field, "")
    entry.pop("transformation_function", None)
    entry.pop("transformation_params", None)
    entry.pop("use_constant", None)
    entry["source_field"] = ""
    entry["constant_value"] = resolution.params.get("constant_value", "")
    return [_change(agent, resolution, previous, copy.deepcopy(entry))]


def _apply_transform(
    configs: dict[str, dict], resolution: Resolution, agent: str
) -> list[ChangeRecord]:
    """Shared applier for type coercion and picklist value maps."""
    config = configs[resolution.mapping]
    entry, previous = _ensure_field(
        config, resolution.field, resolution.params.get("source_field", "")
    )
    _clear_value_sources(entry)
    entry["transformation_function"] = resolution.params["transform"]
    entry["transformation_params"] = resolution.params.get("params") or {}
    if resolution.params.get("source_field"):
        entry["source_field"] = resolution.params["source_field"]
    return [_change(agent, resolution, previous, copy.deepcopy(entry))]


def _apply_external_id_upsert(
    configs: dict[str, dict], resolution: Resolution, agent: str
) -> list[ChangeRecord]:
    config = configs[resolution.mapping]
    external_id = resolution.params.get("external_id_field", "")
    previous = config.get("external_id_field")
    config["external_id_field"] = external_id
    return [
        _change(
            agent,
            resolution,
            previous,
            external_id,
            target=TARGET_CONFIG,
            field_name="external_id_field",
        )
    ]


def _apply_self_reference_guard(
    configs: dict[str, dict], resolution: Resolution, agent: str
) -> list[ChangeRecord]:
    """Make a self-referencing lookup survive a single pass.

    The parent row may not exist yet when the child is loaded, so the field
    resolves through this mapping's own id-mapping table and unresolved rows are
    dropped rather than failing the batch. The accompanying notice tells the
    expert a follow-up update mapping is needed to attach the stragglers.
    """
    config = configs[resolution.mapping]
    table = lookup_table_name(config)
    if not table:
        raise ValueError(
            f"Mapping '{resolution.mapping}' has no source table, so its own "
            "id-mapping table name cannot be derived"
        )
    entry, previous = _ensure_field(
        config, resolution.field, resolution.params.get("source_field", "")
    )
    _clear_value_sources(entry)
    entry["transformation_function"] = "generic_lookup"
    entry["transformation_params"] = {"lookup_tables": [table], "default": ""}
    entry["skip_if_not_found"] = bool(resolution.params.get("skip_if_unresolved", False))
    if resolution.params.get("source_field"):
        entry["source_field"] = resolution.params["source_field"]
    return [_change(agent, resolution, previous, copy.deepcopy(entry))]


def _apply_deferred_lookup(
    configs: dict[str, dict], resolution: Resolution, agent: str
) -> list[ChangeRecord]:
    """Resolve a lookup that cannot be filled on the insert pass, later.

    The record loads with the reference empty and the backfill pass attaches it
    once the parent has ids. With no parent named it is a self-reference.
    """
    config = configs[resolution.mapping]
    parents = resolution.params.get("parent_mappings") or []
    tables: list[str] = []
    for parent in parents:
        parent_config = configs.get(parent) or mappings_service.get_mapping_config(parent)
        table = lookup_table_name(parent_config) if parent_config else ""
        if table and table not in tables:
            tables.append(table)
    if not parents:
        own = lookup_table_name(config)
        if own:
            tables.append(own)
    if not tables:
        raise ValueError(
            f"No id-mapping table could be derived for deferred lookup on "
            f"'{resolution.field}'"
        )
    entry, previous = _ensure_field(
        config, resolution.field, resolution.params.get("source_field", "")
    )
    _clear_value_sources(entry)
    entry["transformation_function"] = "generic_lookup"
    entry["transformation_params"] = {"lookup_tables": tables, "default": ""}
    entry["defer_to_backfill"] = True
    entry.pop("skip_if_not_found", None)
    if resolution.params.get("source_field"):
        entry["source_field"] = resolution.params["source_field"]
    return [_change(agent, resolution, previous, copy.deepcopy(entry))]


def _apply_remove_field(
    configs: dict[str, dict], resolution: Resolution, agent: str
) -> list[ChangeRecord]:
    """Stop writing a destination field. The removed entry is journalled."""
    config = configs[resolution.mapping]
    fields = config.get("fields") or []
    index = next(
        (
            i
            for i, entry in enumerate(fields)
            if entry.get("destination_field") == resolution.field
        ),
        None,
    )
    if index is None:
        raise ValueError(f"'{resolution.field}' is not mapped, so there is nothing to remove")
    previous = copy.deepcopy(fields.pop(index))
    config["fields"] = fields
    return [_change(agent, resolution, previous, None)]


_APPLIERS = {
    KIND_LOOKUP_REFERENCE: _apply_lookup_reference,
    KIND_DEFERRED_LOOKUP: _apply_deferred_lookup,
    KIND_CONSTANT_REFERENCE: _apply_constant_reference,
    KIND_TYPE_COERCION: _apply_transform,
    KIND_VALUE_MAP: _apply_transform,
    KIND_EXTERNAL_ID_UPSERT: _apply_external_id_upsert,
    KIND_SELF_REFERENCE_GUARD: _apply_self_reference_guard,
    KIND_REMOVE_FIELD: _apply_remove_field,
}


def apply_resolutions(
    configs: dict[str, dict],
    resolutions: list[Resolution],
    agent: str = AGENT_RELATIONSHIP,
) -> tuple[list[ChangeRecord], list[dict]]:
    """Patch ``configs`` in place for every non-advisory resolution.

    Returns the change records and a list of resolutions that could not be
    applied, each with the reason. One bad resolution is skipped rather than
    abandoning the rest of the run.

    An advisory resolution changes nothing but is still recorded, unapplied, so
    that something the agent thought worth saying survives the run instead of
    living only in a log line.
    """
    changes: list[ChangeRecord] = []
    rejected: list[dict] = []
    for resolution in resolutions:
        if resolution.is_advisory:
            suggested = str(resolution.params.get("suggested_action") or "").strip()
            changes.append(
                ChangeRecord(
                    agent=agent,
                    kind=resolution.kind,
                    mapping_name=resolution.mapping,
                    field_name=resolution.field,
                    rationale=resolution.rationale,
                    severity=resolution.severity,
                    # Persist the recommended next step; audit surfaces it as
                    # suggested_action when the journal is read back.
                    new_value={"suggested_action": suggested} if suggested else None,
                    applied=False,
                )
            )
            continue
        applier = _APPLIERS.get(resolution.kind)
        if applier is None:
            rejected.append({**resolution.to_dict(), "reason": f"Unknown kind '{resolution.kind}'"})
            continue
        if resolution.mapping not in configs:
            rejected.append(
                {**resolution.to_dict(), "reason": f"Mapping '{resolution.mapping}' is not in this migration"}
            )
            continue
        try:
            changes.extend(applier(configs, resolution, agent))
        except Exception as exc:  # noqa: BLE001 — reported, not fatal
            logger.warning("[agent] could not apply %s: %s", resolution.kind, exc)
            rejected.append({**resolution.to_dict(), "reason": str(exc)})
    return changes, rejected


def assign_load_order(
    configs: dict[str, dict],
    order: list[str],
    agent: str = AGENT_RELATIONSHIP,
) -> list[ChangeRecord]:
    """Write a parent-first position onto each mapping as its priority.

    The order itself is always recomputed from the schema, never taken from the
    model. Persisting it keeps the loader's own ordering and the pipeline's
    priority-based grouping in agreement.
    """
    changes: list[ChangeRecord] = []
    for position, name in enumerate(order):
        config = configs.get(name)
        if config is None:
            continue
        previous = config.get("priority")
        if previous == position:
            continue
        config["priority"] = position
        changes.append(
            ChangeRecord(
                agent=agent,
                kind="load_order",
                mapping_name=name,
                field_name="priority",
                target=TARGET_CONFIG,
                previous_value=previous,
                new_value=position,
                rationale=f"Parent-first position {position} of {len(order)}",
            )
        )
    return changes


def persist(configs: dict[str, dict], names: list[str] | None = None) -> list[str]:
    """Save the given draft configs back to ``mapping_configs``."""
    saved: list[str] = []
    for name in names if names is not None else list(configs):
        config = configs.get(name)
        if config is None:
            continue
        mappings_service.save_mapping_config(
            name=name,
            config=config,
            source_type=source_type_of(name, config),
            priority=config.get("priority"),
        )
        saved.append(name)
    if saved:
        logger.info("[agent] persisted mapping config(s): %s", ", ".join(saved))
    return saved


def restore_value(mapping_name: str, target: str, field_name: str, value) -> None:
    """Write a journalled previous value back into a stored mapping config."""
    config = mappings_service.get_mapping_config(mapping_name)
    if config is None:
        raise KeyError(f"Mapping '{mapping_name}' no longer exists")
    config = copy.deepcopy(config)

    if target == TARGET_CONFIG:
        if value is None:
            config.pop(field_name, None)
        else:
            config[field_name] = value
    elif target == TARGET_FIELD:
        fields = config.get("fields") or []
        index = next(
            (i for i, entry in enumerate(fields) if entry.get("destination_field") == field_name),
            None,
        )
        if value is None:
            if index is not None:
                fields.pop(index)
        elif index is None:
            fields.append(value)
        else:
            fields[index] = value
        config["fields"] = fields
    else:
        raise ValueError(f"Unknown change target '{target}'")

    mappings_service.save_mapping_config(
        name=mapping_name,
        config=config,
        source_type=source_type_of(mapping_name, config),
        priority=config.get("priority"),
    )
