"""The read-only tools the relationship agent may call, and their dispatcher.

Every tool answers a question about schema or configuration. None of them reads
record data and none of them writes anything, so the agent's exploration cannot
have side effects and its cost scales with schema size rather than data volume.
Results are trimmed to keep a single answer from swamping the token budget.
"""
from __future__ import annotations

from dataclasses import dataclass

from agents import wiring
from agents.bedrock_client import ToolSpec
from agents.context import MigrationContext
from agents.detector import detect

SUBMIT_TOOL = "submit_resolutions"
SUBMIT_REPAIR_TOOL = "submit_repairs"

# Trim limits for tool answers.
_MAX_FIELDS = 120
_MAX_SOURCE_FIELDS = 150

_SEVERITIES = ["info", "warning", "blocker"]


def _trim(items: list, limit: int) -> tuple[list, str]:
    if len(items) <= limit:
        return items, ""
    return items[:limit], f"{len(items) - limit} further entries omitted"


# One resolution, as the model states it. Both agents submit this shape and both
# are checked by the same validator, so the repair agent cannot express a change
# the relationship agent would have been refused.
RESOLUTION_ITEM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": sorted(wiring.RESOLUTION_KINDS),
            "description": (
                "lookup_reference: resolve a lookup from a parent mapping's migrated "
                "ids. constant_reference: pin a lookup to one existing destination "
                "record. self_reference_guard: resolve a lookup that points at the "
                "mapping's own object. type_coercion / value_map: fix a datatype or "
                "picklist. external_id_upsert: make re-runs update instead of "
                "duplicate. notice: flag something a human must decide, changing "
                "nothing."
            ),
        },
        "mapping": {"type": "string"},
        "field": {
            "type": "string",
            "description": (
                "Destination field. Omit only for external_id_upsert and notice."
            ),
        },
        "rationale": {
            "type": "string",
            "description": "Why this is right, in one sentence.",
        },
        "severity": {"type": "string", "enum": _SEVERITIES},
        "parent_mappings": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "For lookup_reference: the mappings whose migrated ids this field "
                "resolves against. Name the mappings, not tables — the id table is "
                "derived for you."
            ),
        },
        "source_field": {
            "type": "string",
            "description": (
                "Source field holding the value. Required when the field is not "
                "mapped yet."
            ),
        },
        "constant_value": {"type": "string"},
        "transform": {"type": "string", "enum": sorted(wiring.TRANSFORM_ALLOW_LIST)},
        "transform_params": {"type": "object"},
        "value_map": {
            "type": "object",
            "description": "For value_map: source value -> destination picklist value.",
        },
        "value_map_default": {"type": "string"},
        "external_id_field": {"type": "string"},
        "skip_if_unresolved": {
            "type": "boolean",
            "description": (
                "Drop rows whose parent id cannot be resolved instead of loading them "
                "with an empty lookup."
            ),
        },
        "default": {"type": "string"},
    },
    "required": ["kind", "mapping", "rationale"],
}


def build_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="list_mappings",
            description=(
                "List every mapping in this migration with its source table, destination "
                "object, operation and field counts. Start here."
            ),
            input_schema={"type": "object", "properties": {}},
        ),
        ToolSpec(
            name="describe_mapping",
            description=(
                "The full field mappings for one mapping: destination field, source field, "
                "transform, constant and skip flag for each."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "mapping_name": {"type": "string", "description": "Name from list_mappings."}
                },
                "required": ["mapping_name"],
            },
        ),
        ToolSpec(
            name="describe_destination_object",
            description=(
                "Field metadata for a destination object. Defaults to lookup fields only; "
                "use filter='required', 'id_lookup', 'picklist' or 'all', or name specific "
                "fields, to see more."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_name": {"type": "string"},
                    "filter": {
                        "type": "string",
                        "enum": ["reference", "required", "id_lookup", "picklist", "all"],
                    },
                    "field_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Return exactly these fields, ignoring the filter.",
                    },
                },
                "required": ["object_name"],
            },
        ),
        ToolSpec(
            name="describe_source_table",
            description=(
                "Fields of a source table. Defaults to reference-type fields, which are the "
                "ones that carry parent ids."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "table_name": {"type": "string"},
                    "only_references": {"type": "boolean"},
                },
                "required": ["table_name"],
            },
        ),
        ToolSpec(
            name="get_findings",
            description=(
                "The pre-computed list of relationship and datatype problems across all "
                "mappings, each with the parent mappings that could resolve it and whether "
                "it blocks launch. This is the work list."
            ),
            input_schema={"type": "object", "properties": {}},
        ),
        ToolSpec(
            name="get_dependency_analysis",
            description=(
                "The parent-first load order implied by the current mappings, the lookup "
                "edges behind it, and whether the graph has a cycle."
            ),
            input_schema={"type": "object", "properties": {}},
        ),
        ToolSpec(
            name=SUBMIT_TOOL,
            description=(
                "Submit the final resolution for every finding that needs one. Call this "
                "exactly once, when you are done investigating. Do not include a resolution "
                "for findings that are already correct."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Two or three sentences an engineer can read.",
                    },
                    "resolutions": {
                        "type": "array",
                        "items": RESOLUTION_ITEM_SCHEMA,
                    },
                },
                "required": ["resolutions"],
            },
        ),
    ]


def build_repair_tools() -> list[ToolSpec]:
    """The narrower tool set for a post-load repair.

    No findings walk and no dependency analysis: the errors the destination
    returned are the work list, and the only question is which field transform
    was wrong.
    """
    return [
        ToolSpec(
            name="get_error_digest",
            description=(
                "The aggregated errors the destination returned for this object: status "
                "code, message, implicated fields and how many rows hit each."
            ),
            input_schema={"type": "object", "properties": {}},
        ),
        ToolSpec(
            name="describe_mapping",
            description=(
                "The full field mappings for one mapping, so you can see how the failing "
                "field is currently produced."
            ),
            input_schema={
                "type": "object",
                "properties": {"mapping_name": {"type": "string"}},
                "required": ["mapping_name"],
            },
        ),
        ToolSpec(
            name="describe_destination_object",
            description=(
                "Field metadata for a destination object. Use field_names to look up the "
                "exact fields the errors named, including their type, length and picklist "
                "values."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_name": {"type": "string"},
                    "filter": {
                        "type": "string",
                        "enum": ["reference", "required", "id_lookup", "picklist", "all"],
                    },
                    "field_names": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["object_name"],
            },
        ),
        ToolSpec(
            name="describe_source_table",
            description="Fields of a source table, to confirm what the source actually holds.",
            input_schema={
                "type": "object",
                "properties": {
                    "table_name": {"type": "string"},
                    "only_references": {"type": "boolean"},
                },
                "required": ["table_name"],
            },
        ),
        ToolSpec(
            name=SUBMIT_REPAIR_TOOL,
            description=(
                "Submit the repair for each failing field, or a notice for anything that "
                "is not a mapping problem. Call this exactly once."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "What went wrong and what you changed, in two sentences.",
                    },
                    "retryable": {
                        "type": "boolean",
                        "description": (
                            "False when re-running would fail again regardless of the "
                            "mapping — a permission problem, a validation rule, a missing "
                            "parent record."
                        ),
                    },
                    "resolutions": {
                        "type": "array",
                        "items": RESOLUTION_ITEM_SCHEMA,
                    },
                },
                "required": ["resolutions"],
            },
        ),
    ]


@dataclass
class ToolDispatcher:
    """Executes tool calls against one migration context."""

    context: MigrationContext
    # Present only for a repair run, where it is the work list.
    digest: dict | None = None

    def __call__(self, name: str, arguments: dict) -> dict:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise ValueError(f"Unknown tool '{name}'")
        return handler(arguments or {})

    # ── handlers ───────────────────────────────────────────────────────────

    def _tool_list_mappings(self, _: dict) -> dict:
        mappings = []
        for name in self.context.mapping_names:
            config = self.context.configs[name]
            entries = config.get("fields") or []
            mappings.append(
                {
                    "mapping": name,
                    "source_tables": config.get("source_tables")
                    or config.get("servicenow_tables", []),
                    "destination_object": self.context.destination_object(name),
                    "operation": config.get("operation", "insert"),
                    "transformation_name": config.get("transformation_name", ""),
                    "external_id_field": config.get("external_id_field", ""),
                    "is_attachment_table": bool(config.get("is_attachment_table")),
                    "mapped_field_count": len(entries),
                    "id_mapping_table": wiring.lookup_table_name(config),
                }
            )
        return {"source_type": self.context.source_type, "mappings": mappings}

    def _tool_describe_mapping(self, arguments: dict) -> dict:
        name = arguments.get("mapping_name", "")
        config = self.context.configs.get(name)
        if not config:
            raise ValueError(f"'{name}' is not one of the mappings in this migration")
        entries = []
        for entry in config.get("fields") or []:
            described = {
                "destination_field": entry.get("destination_field", ""),
                "source_field": entry.get("source_field", ""),
            }
            if entry.get("transformation_function"):
                described["transform"] = entry["transformation_function"]
                described["transform_params"] = entry.get("transformation_params") or {}
            if entry.get("constant_value") is not None:
                described["constant_value"] = entry["constant_value"]
            if entry.get("use_constant"):
                described["constant_from_env"] = entry["use_constant"]
            if entry.get("skip_if_not_found"):
                described["skip_if_not_found"] = True
            entries.append(described)
        trimmed, omitted = _trim(entries, _MAX_FIELDS)
        return {
            "mapping": name,
            "destination_object": self.context.destination_object(name),
            "operation": config.get("operation", "insert"),
            "id_mapping_table": wiring.lookup_table_name(config),
            "fields": trimmed,
            "note": omitted,
        }

    def _tool_describe_destination_object(self, arguments: dict) -> dict:
        object_name = arguments.get("object_name", "")
        fields = self.context.destination_fields(object_name)
        if not fields:
            raise ValueError(
                f"{object_name} could not be described. Check destination credentials "
                "and object permissions."
            )

        requested = arguments.get("field_names") or []
        if requested:
            selected = [entry for entry in fields if entry["name"] in set(requested)]
        else:
            mode = arguments.get("filter") or "reference"
            if mode == "all":
                selected = fields
            elif mode == "required":
                selected = [f for f in fields if f.get("required")]
            elif mode == "id_lookup":
                selected = [f for f in fields if f.get("id_lookup")]
            elif mode == "picklist":
                selected = [f for f in fields if f.get("picklist_values")]
            else:
                selected = [f for f in fields if f.get("type") == "reference"]

        summarised = []
        for entry in selected:
            item = {
                "name": entry["name"],
                "type": entry["type"],
                "required": entry["required"],
                "createable": entry["createable"],
            }
            if entry.get("reference_to"):
                item["reference_to"] = entry["reference_to"]
                item["loaded_by"] = [
                    self.context.mapping_for_destination_object(target)
                    for target in entry["reference_to"]
                    if self.context.mapping_for_destination_object(target)
                ]
            if entry.get("id_lookup"):
                item["id_lookup"] = True
            if entry.get("picklist_values"):
                values, _ = _trim(entry["picklist_values"], 25)
                item["picklist_values"] = values
            if entry.get("length"):
                item["length"] = entry["length"]
            summarised.append(item)

        trimmed, omitted = _trim(summarised, _MAX_FIELDS)
        return {
            "object": object_name,
            "total_field_count": len(fields),
            "fields": trimmed,
            "note": omitted,
        }

    def _tool_describe_source_table(self, arguments: dict) -> dict:
        table_name = arguments.get("table_name", "")
        fields = self.context.source_fields(table_name)
        if not fields:
            return {
                "table": table_name,
                "fields": [],
                "note": (
                    "This source does not support field introspection, so source field "
                    "names cannot be confirmed here."
                ),
            }
        only_references = arguments.get("only_references")
        if only_references is None:
            only_references = True
        selected = (
            [f for f in fields if f["type"] == "reference"] if only_references else fields
        )
        trimmed, omitted = _trim(selected, _MAX_SOURCE_FIELDS)
        return {
            "table": table_name,
            "total_field_count": len(fields),
            "fields": trimmed,
            "note": omitted,
        }

    def _tool_get_error_digest(self, _: dict) -> dict:
        if self.digest is None:
            raise ValueError("No error digest is available in this run")
        return self.digest

    def _tool_get_findings(self, _: dict) -> dict:
        findings = detect(self.context)
        return {
            "blocking_count": sum(1 for f in findings if f.blocking),
            "findings": [f.to_dict() for f in findings],
        }

    def _tool_get_dependency_analysis(self, _: dict) -> dict:
        analysis = self.context.dependency_analysis()
        return {
            "order": analysis.get("order", []),
            "edges": analysis.get("edges", []),
            "warnings": analysis.get("warnings", []),
            "missing_parents": analysis.get("missing_parents", []),
            "has_cycle": analysis.get("has_cycle", False),
        }
