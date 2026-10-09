"""The read-only tools the agents may call, and their dispatcher.

Most tools answer a question about schema or configuration. The diagnosis and
assistant agents additionally get two data tools — a sample of the rows the
destination rejected and a profile of one staged source column — because an
error like "Resolution Date must be after Start Date" cannot be explained from
schema alone. Both are capped and truncated, and ``AGENT_DIGEST_SAMPLES=0``
turns the row values off. Nothing here writes to a mapping: a proposal is
stored for a person to approve.
Results are trimmed to keep a single answer from swamping the token budget.
"""
from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from agents import wiring
from agents.bedrock_client import ToolSpec
from agents.context import MigrationContext
from agents.detector import detect

SUBMIT_TOOL = "submit_resolutions"
SUBMIT_REPAIR_TOOL = "submit_repairs"
SUBMIT_DIAGNOSIS_TOOL = "submit_diagnosis"
SUBMIT_READINESS_TOOL = "submit_readiness"
PROPOSE_CHANGES_TOOL = "propose_changes"
PROPOSE_RERUN_TOOL = "propose_rerun"

# Trim limits for tool answers.
_MAX_FIELDS = 120
_MAX_SOURCE_FIELDS = 150
_MAX_SAMPLE_ROWS = 10
_MAX_VALUE_LENGTH = 60
_MAX_PROFILE_ROWS = 200_000

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
                "ids. deferred_lookup: load the record with the lookup empty and "
                "attach it in the backfill pass. constant_reference: pin a lookup "
                "to one existing destination record. self_reference_guard: resolve "
                "a lookup that points at the mapping's own object. type_coercion / "
                "value_map: fix a datatype or picklist, or set any allow-listed "
                "transform (generic_rules for conditional values, "
                "generic_default_if_empty for fallbacks). remove_field: stop "
                "writing a destination field. external_id_upsert: make re-runs "
                "update instead of duplicate. record_value_map: fill a lookup by "
                "translating source values into ids of records that already exist "
                "(e.g. ServiceNow role -> Profile id). notice: flag something a human "
                "must decide, changing nothing."
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
            "description": (
                "For value_map: source value -> destination picklist value. For "
                "record_value_map: source value -> destination record id."
            ),
        },
        "target_object": {
            "type": "string",
            "description": "For record_value_map: the object the mapped ids belong to.",
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
        "suggested_action": {
            "type": "string",
            "description": "For notice: what the person should do next. Required for notices.",
        },
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


def _data_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="sample_failed_rows",
            description=(
                "Rows the destination rejected on the latest load of a mapping: the "
                "error, the values that were sent, and the staged source row they came "
                "from. Use this to see WHY a value was wrong — for example that a "
                "prerequisite column is empty at the source."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "mapping_name": {"type": "string"},
                    "limit": {"type": "integer", "description": "At most 10."},
                    "error_contains": {
                        "type": "string",
                        "description": "Only rows whose error contains this text.",
                    },
                },
                "required": ["mapping_name"],
            },
        ),
        ToolSpec(
            name="profile_source_field",
            description=(
                "Profile one column of the staged source extract: how many rows are "
                "empty, how many distinct values there are, and the most common ones."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "table_name": {"type": "string"},
                    "field_name": {"type": "string"},
                },
                "required": ["table_name", "field_name"],
            },
        ),
    ]


def _schema_tools() -> list[ToolSpec]:
    """The investigation tools every post-load agent shares."""
    repair = {tool.name: tool for tool in build_repair_tools()}
    general = {tool.name: tool for tool in build_tools()}
    return [
        general["list_mappings"],
        repair["get_error_digest"],
        repair["describe_mapping"],
        repair["describe_destination_object"],
        general["describe_source_table"],
        *_data_tools(),
    ]


def build_diagnosis_tools() -> list[ToolSpec]:
    """Tools for explaining failures that nothing fixed automatically."""
    return [
        *_schema_tools(),
        ToolSpec(
            name=SUBMIT_DIAGNOSIS_TOOL,
            description=(
                "Submit the diagnosis once, after investigating. Explain the root cause "
                "plainly, cite the evidence you saw, and rank two or three solutions. "
                "Attach concrete resolutions to a solution whenever a mapping change "
                "would fix it, so the person can apply it with one click."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "analysis": {
                        "type": "string",
                        "description": (
                            "What is going wrong and why, in plain language a "
                            "migration engineer can act on. A short paragraph."
                        ),
                    },
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Concrete observations, e.g. '4 rows have empty work_start'.",
                    },
                    "suggestions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "explanation": {
                                    "type": "string",
                                    "description": "What it does and its trade-off.",
                                },
                                "resolutions": {
                                    "type": "array",
                                    "items": RESOLUTION_ITEM_SCHEMA,
                                },
                            },
                            "required": ["title", "explanation"],
                        },
                    },
                },
                "required": ["analysis", "suggestions"],
            },
        ),
    ]


# One Salesforce write a readiness suggestion needs, as the model states it.
# Checked by agents.sf_actions before a person sees it, and again on Apply.
SF_ACTION_ITEM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["create_record", "create_custom_field"],
            "description": (
                "create_record: make one record in an object the API can create, "
                "e.g. a default Group or a placeholder Account, so a required lookup "
                "has something to point at. Refer to it from a resolution as "
                "{{action:N}}, N being its position in this list. "
                "create_custom_field: add a field for source data with no home."
            ),
        },
        "object": {"type": "string"},
        "rationale": {"type": "string"},
        "values": {
            "type": "object",
            "description": (
                "For create_record: field -> value, including every field the object "
                "requires. A lookup takes a record id or {{action:N}} of an earlier action."
            ),
        },
        "field_api_name": {"type": "string", "description": "For create_custom_field; ends with __c."},
        "field_label": {"type": "string"},
        "field_type": {
            "type": "string",
            "enum": [
                "Text", "Number", "TextArea", "LongTextArea", "Checkbox",
                "Date", "DateTime", "Url", "Email", "Phone",
            ],
        },
        "length": {"type": "integer"},
        "description": {"type": "string"},
    },
    "required": ["kind", "object", "rationale"],
}


# One write to the source system a readiness suggestion needs. Checked by
# agents.source_actions before a person sees it, and again on Apply.
SOURCE_ACTION_ITEM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["fill_source_values"],
            "description": (
                "fill_source_values: set one column to one value on the records of a "
                "source table where that column is empty. Records that already have a "
                "value are never touched; the change can be undone."
            ),
        },
        "table": {"type": "string", "description": "A source table a mapping in this migration reads."},
        "field": {"type": "string", "description": "The column, bare (short_description, not incident.short_description)."},
        "value": {
            "type": "string",
            "description": "The value to write. For a reference column, the sys_id of an existing record.",
        },
        "filter": {
            "type": "string",
            "description": (
                "Optional encoded query that narrows the records further, e.g. active=true. "
                "The column-is-empty condition is always added."
            ),
        },
        "rationale": {"type": "string"},
    },
    "required": ["kind", "table", "field", "value", "rationale"],
}


def build_readiness_tools() -> list[ToolSpec]:
    """Tools for checking the source can supply what the destination requires.

    Everything here reads. The live counts replace the staged-data tools the
    post-load agents use, because at this point nothing has been extracted.
    """
    general = {tool.name: tool for tool in build_tools()}
    repair = {tool.name: tool for tool in build_repair_tools()}
    return [
        ToolSpec(
            name="get_readiness_report",
            description=(
                "The deterministic readiness check: every required destination field of "
                "every mapping, its status, source row and empty counts, candidate source "
                "columns, the parent object of each required lookup and whether the API "
                "can create it, and user licence capacity. This is the work list."
            ),
            input_schema={"type": "object", "properties": {}},
        ),
        general["list_mappings"],
        repair["describe_mapping"],
        repair["describe_destination_object"],
        general["describe_source_table"],
        ToolSpec(
            name="count_source_empty",
            description=(
                "Live count of rows in a source table, and of rows where one column is "
                "empty. Nothing needs to be extracted first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "table_name": {"type": "string"},
                    "field_name": {"type": "string"},
                },
                "required": ["table_name", "field_name"],
            },
        ),
        ToolSpec(
            name="query_destination_records",
            description=(
                "Records that already exist in a destination object: id and name, "
                "optionally filtered by a name search, plus the total count and whether "
                "the API can create more. Use it to find a Profile, Group, Queue or "
                "Account a lookup can point at."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_name": {"type": "string"},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "description": "At most 50."},
                },
                "required": ["object_name"],
            },
        ),
        ToolSpec(
            name="query_source_records",
            description=(
                "Records in a source table: sys_id and a few columns, filtered by an "
                "encoded query. Use it to find the record a reference column should be "
                "filled with (a default caller in sys_user, an assignment group) or to "
                "look at rows where a required column is empty."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "table_name": {"type": "string"},
                    "query": {"type": "string", "description": "Encoded query, e.g. nameLIKEservice desk."},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Columns to return besides sys_id; at most 8.",
                    },
                    "limit": {"type": "integer", "description": "At most 25."},
                },
                "required": ["table_name"],
            },
        ),
        ToolSpec(
            name=SUBMIT_READINESS_TOOL,
            description=(
                "Submit once, after investigating. For each gap, explain it and rank one "
                "to three suggestions. A suggestion can add mappings to the run, change "
                "mappings, make Salesforce changes, fix the data in the source system, or "
                "any combination; one that needs a person has manual steps instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Two or three sentences on whether the source can fill the destination.",
                    },
                    "gaps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {
                                    "type": "string",
                                    "description": "The requirement key from the report, Mapping.Field.",
                                },
                                "analysis": {"type": "string"},
                                "suggestions": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "title": {"type": "string"},
                                            "explanation": {
                                                "type": "string",
                                                "description": "What it does and its trade-off.",
                                            },
                                            "include_mappings": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                                "description": "Mappings to add to the run.",
                                            },
                                            "salesforce_actions": {
                                                "type": "array",
                                                "items": SF_ACTION_ITEM_SCHEMA,
                                            },
                                            "source_actions": {
                                                "type": "array",
                                                "items": SOURCE_ACTION_ITEM_SCHEMA,
                                                "description": "Writes to the source system, run before anything else.",
                                            },
                                            "resolutions": {
                                                "type": "array",
                                                "items": RESOLUTION_ITEM_SCHEMA,
                                            },
                                            "manual_steps": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                        },
                                        "required": ["title", "explanation"],
                                    },
                                },
                            },
                            "required": ["key", "analysis", "suggestions"],
                        },
                    },
                },
                "required": ["summary", "gaps"],
            },
        ),
    ]


def build_assistant_tools() -> list[ToolSpec]:
    """Tools for the conversational agent: investigate, then propose."""
    return [
        *_schema_tools(),
        ToolSpec(
            name="get_diagnosis",
            description=(
                "The diagnoses already written for this session's failing objects, "
                "with their suggested solutions."
            ),
            input_schema={"type": "object", "properties": {}},
        ),
        ToolSpec(
            name=PROPOSE_CHANGES_TOOL,
            description=(
                "Propose mapping changes for the person to approve. Nothing is written "
                "until they click Apply. Each resolution is schema-checked; the result "
                "tells you which were accepted, which were rejected and why, and the "
                "before/after of each field. Fix rejected ones and propose again if "
                "needed. Then tell the person, in text, what you proposed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "One or two sentences on what this proposal fixes.",
                    },
                    "resolutions": {
                        "type": "array",
                        "items": RESOLUTION_ITEM_SCHEMA,
                    },
                    "rerun": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Mappings to offer re-running once applied.",
                    },
                },
                "required": ["summary", "resolutions"],
            },
        ),
        ToolSpec(
            name=PROPOSE_RERUN_TOOL,
            description=(
                "Offer to re-run mappings without changing anything, e.g. when the "
                "cause was transient. The person confirms before it runs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "mappings": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["mappings", "reason"],
            },
        ),
    ]


def _truncate(value: object) -> str:
    text = str(value if value is not None else "")
    return text if len(text) <= _MAX_VALUE_LENGTH else text[:_MAX_VALUE_LENGTH] + "…"


def _read_csv_rows(path: Path):
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            yield from csv.DictReader(handle)
    except (OSError, csv.Error):
        return


@dataclass
class ToolDispatcher:
    """Executes tool calls against one migration context."""

    context: MigrationContext
    # Present only for a repair run, where it is the work list.
    digest: dict | None = None
    # Present only for the assistant: already-written diagnoses, and the
    # callbacks that store a proposal. Neither callback writes to a mapping.
    diagnoses: list[dict] | None = None
    on_propose_changes: Callable[[dict], dict] | None = None
    on_propose_rerun: Callable[[dict], dict] | None = None
    # Present only for a readiness check: the deterministic report.
    readiness: dict | None = None
    # Per-call cache of staged source rows by table, keyed by source id.
    _staged: dict[str, dict[str, dict]] = field(default_factory=dict, repr=False)

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

    # ── data tools ─────────────────────────────────────────────────────────

    def _config(self, mapping_name: str) -> dict:
        config = self.context.configs.get(mapping_name)
        if not config:
            raise ValueError(f"'{mapping_name}' is not one of the mappings in this migration")
        return config

    def _source_id_column(self, config: dict) -> str:
        from config.settings import SERVICE_NOW_ID_COLUMN

        return config.get("source_id_column") or SERVICE_NOW_ID_COLUMN

    def _staged_by_id(self, table: str, id_column: str) -> dict[str, dict]:
        """Staged source rows for one table, keyed by source id."""
        if table in self._staged:
            return self._staged[table]
        from config.settings import PATH_TO_DOWNLOADS

        rows: dict[str, dict] = {}
        folder = PATH_TO_DOWNLOADS / self.context.source_type / table
        for path in sorted(folder.glob("*.csv")) if folder.exists() else []:
            for row in _read_csv_rows(path):
                key = (row.get(id_column) or "").strip()
                if key:
                    rows[key] = row
        self._staged[table] = rows
        return rows

    def _latest_failed_csvs(self, config: dict) -> list[Path]:
        """The failed-result files from the most recent load of a mapping."""
        from agents.error_digest import latest_job_dir
        from config.settings import PATH_TO_RESULTS

        folder = config.get("transformation_name") or wiring.cfg_destination_object(config)
        root = PATH_TO_RESULTS / folder if folder else None
        if not root or not root.exists():
            return []
        root = latest_job_dir(root) or root
        return sorted(root.rglob("failed.csv"))

    def _tool_sample_failed_rows(self, arguments: dict) -> dict:
        from agents.error_digest import _sample_limit, parse_error
        from services.external.db_client import db_client

        name = arguments.get("mapping_name", "")
        config = self._config(name)
        limit = max(1, min(int(arguments.get("limit") or 5), _MAX_SAMPLE_ROWS))
        needle = str(arguments.get("error_contains") or "").lower()
        show_values = _sample_limit() > 0

        table = self.context.driving_table(name)
        id_column = self._source_id_column(config)
        qualified_id = f"{table}.{id_column}"
        # The destination column that carries the source id, when one does.
        id_carrier = next(
            (
                entry.get("destination_field")
                for entry in config.get("fields") or []
                if (entry.get("source_field") or "").strip() in (qualified_id, id_column)
            ),
            "",
        )
        mapped_sources = sorted(
            {
                part.strip().split(".")[-1]
                for entry in config.get("fields") or []
                for part in str(entry.get("source_field") or "").split(",")
                if part.strip()
            }
        )
        staged = self._staged_by_id(table, id_column) if table else {}
        id_table = wiring.lookup_table_name(config)

        samples: list[dict] = []
        total = 0
        for path in self._latest_failed_csvs(config):
            for row in _read_csv_rows(path):
                error = row.get("sf__Error", "")
                if needle and needle not in error.lower():
                    continue
                total += 1
                if len(samples) >= limit:
                    continue
                code, message, fields = parse_error(error)
                source_id = (row.get(id_carrier) or "").strip() if id_carrier else ""
                if not source_id:
                    destination_id = (row.get("Id") or row.get("sf__Id") or "").strip()
                    if destination_id and id_table:
                        found = db_client.fetch_one(
                            "SELECT servicenow_id FROM id_mappings "
                            "WHERE table_name = ? AND salesforce_id = ?",
                            (id_table, destination_id),
                        )
                        source_id = found[0] if found else ""
                sample: dict = {
                    "error_code": code,
                    "error": message,
                    "fields": fields,
                    "source_id": source_id,
                    "batch": path.parent.name,
                }
                if show_values:
                    sample["sent"] = {
                        key: _truncate(value)
                        for key, value in row.items()
                        if key not in ("sf__Id", "sf__Error") and value
                    }
                    source_row = staged.get(source_id)
                    if source_row is not None:
                        # Empty values are kept: an empty prerequisite is often
                        # the whole explanation.
                        sample["source"] = {
                            column: _truncate(source_row.get(column, ""))
                            for column in mapped_sources
                            if column in source_row
                        }
                    else:
                        sample["source"] = None
                samples.append(sample)
        return {
            "mapping": name,
            "matching_failed_rows": total,
            "samples": samples,
            "note": (
                ""
                if show_values
                else "Row values are hidden because AGENT_DIGEST_SAMPLES=0."
            ),
        }

    def _tool_profile_source_field(self, arguments: dict) -> dict:
        table = arguments.get("table_name", "")
        column = str(arguments.get("field_name", "")).split(".")[-1]
        if not table or not column:
            raise ValueError("table_name and field_name are required")
        from config.settings import PATH_TO_DOWNLOADS

        folder = PATH_TO_DOWNLOADS / self.context.source_type / table
        files = sorted(folder.glob("*.csv")) if folder.exists() else []
        if not files:
            raise ValueError(f"Nothing is staged on disk for source table '{table}'")

        counts: Counter[str] = Counter()
        rows = 0
        empty = 0
        present = False
        for path in files:
            for row in _read_csv_rows(path):
                if rows >= _MAX_PROFILE_ROWS:
                    break
                rows += 1
                if column not in row:
                    continue
                present = True
                value = (row.get(column) or "").strip()
                if not value:
                    empty += 1
                else:
                    counts[value] += 1
        if not present:
            raise ValueError(f"'{column}' is not a column in the staged '{table}' extract")
        top = counts.most_common(10) if _sample_limit_enabled() else []
        return {
            "table": table,
            "field": column,
            "rows": rows,
            "empty": empty,
            "distinct": len(counts),
            "top_values": [{"value": _truncate(v), "count": c} for v, c in top],
        }

    # ── readiness-only ─────────────────────────────────────────────────────

    def _tool_get_readiness_report(self, _: dict) -> dict:
        if self.readiness is None:
            raise ValueError("No readiness report is available in this run")
        return self.readiness

    def _tool_count_source_empty(self, arguments: dict) -> dict:
        from api import sources as source_registry

        table = arguments.get("table_name", "")
        column = str(arguments.get("field_name", "")).split(".")[-1]
        if not table or not column:
            raise ValueError("table_name and field_name are required")
        if not self.context.has_source_field(table, column):
            raise ValueError(f"'{column}' is not a field on source table '{table}'")
        provider = source_registry.get(self.context.source_type)
        if provider is None:
            raise ValueError(f"No source provider for '{self.context.source_type}'")
        credentials = self.context.source_credentials
        total = provider.count_rows(table, credentials)
        empty = provider.count_empty(table, column, credentials)
        return {
            "table": table,
            "field": column,
            "rows": total,
            "empty": empty,
            "note": "" if total >= 0 and empty >= 0 else "The source could not count this.",
        }

    def _tool_query_destination_records(self, arguments: dict) -> dict:
        from services.internal import sf_inventory

        object_name = arguments.get("object_name", "")
        if not object_name:
            raise ValueError("object_name is required")
        credentials = self.context.destination_credentials
        info = sf_inventory.object_info(object_name, credentials)
        page = sf_inventory.sample_records(
            object_name,
            credentials,
            search=str(arguments.get("search") or ""),
            limit=int(arguments.get("limit") or 20),
        )
        if page["total"] < 0:
            raise ValueError(f"{object_name} could not be queried")
        return {
            "object": object_name,
            "createable": bool(info.get("createable")),
            "total": page["total"],
            "name_field": page["name_field"],
            "records": [
                {"id": r["id"], "name": _truncate(r["name"])} for r in page["records"]
            ],
        }

    def _tool_query_source_records(self, arguments: dict) -> dict:
        from api import sources as source_registry

        table = str(arguments.get("table_name") or "")
        if not table:
            raise ValueError("table_name is required")
        provider = source_registry.get(self.context.source_type)
        if provider is None or not hasattr(provider, "query_records"):
            raise ValueError(f"{self.context.source_type} records cannot be queried")
        fields = [str(f).split(".")[-1] for f in arguments.get("fields") or [] if f][:8]
        query = str(arguments.get("query") or "")
        records = provider.query_records(
            table,
            self.context.source_credentials,
            query,
            fields,
            min(max(int(arguments.get("limit") or 10), 1), 25),
        )
        return {
            "table": table,
            "query": query,
            "total": provider.count_rows(table, self.context.source_credentials, query),
            "records": [{k: _truncate(str(v)) for k, v in r.items()} for r in records],
        }

    # ── assistant-only ─────────────────────────────────────────────────────

    def _tool_get_diagnosis(self, _: dict) -> dict:
        return {"diagnoses": self.diagnoses or []}

    def _tool_propose_changes(self, arguments: dict) -> dict:
        if self.on_propose_changes is None:
            raise ValueError("Proposals are not available in this run")
        return self.on_propose_changes(arguments)

    def _tool_propose_rerun(self, arguments: dict) -> dict:
        if self.on_propose_rerun is None:
            raise ValueError("Re-run proposals are not available in this run")
        return self.on_propose_rerun(arguments)


def _sample_limit_enabled() -> bool:
    from agents.error_digest import _sample_limit

    return _sample_limit() > 0
