"""Transforms router — lists available generic transformation functions with parameter schemas."""
from fastapi import APIRouter

router = APIRouter(prefix="/transforms", tags=["transforms"])

GENERIC_TRANSFORMS: list[dict] = [
    {
        "name": "generic_format_datetime",
        "label": "Format DateTime",
        "description": "Convert a datetime string from one format to another.",
        "params_schema": {
            "input_format": {"type": "string", "default": "%Y-%m-%d %H:%M:%S", "description": "Python strftime format of the source value"},
            "output_format": {"type": "string", "default": "%Y-%m-%dT%H:%M:%S.000+0000", "description": "Python strftime format for the destination"},
        },
    },
    {
        "name": "generic_value_map",
        "label": "Value Map",
        "description": "Map source values to destination values using a lookup dictionary.",
        "params_schema": {
            "map": {"type": "object", "default": {}, "description": "Key-value pairs mapping source values to destination values"},
            "default": {"type": "string", "default": "", "description": "Default value when source value is not in the map"},
            "case_insensitive": {"type": "boolean", "default": False, "description": "Match map keys regardless of case"},
            "passthrough_if_missing": {"type": "boolean", "default": False, "description": "Keep the original value instead of using the default"},
        },
    },
    {
        "name": "generic_truncate",
        "label": "Truncate",
        "description": "Truncate the value to a maximum character length.",
        "params_schema": {
            "max_length": {"type": "integer", "default": 255, "description": "Maximum number of characters"},
            "suffix": {"type": "string", "default": "", "description": "Marker appended to a value that was cut, counted within max_length"},
        },
    },
    {
        "name": "generic_concat",
        "label": "Concatenate",
        "description": "Join multiple source field values with a separator; optionally pad to a minimum length and cap at a maximum length.",
        "params_schema": {
            "separator": {"type": "string", "default": " ", "description": "String to place between field values"},
            "skip_empty": {"type": "boolean", "default": True, "description": "Leave out fields that have no value instead of joining a gap"},
            "min_length": {"type": "integer", "default": 0, "description": "Pad the result up to this length (0 = no padding)"},
            "pad_with": {"type": "string", "default": "", "description": "Filler text appended to reach min_length"},
            "max_length": {"type": "integer", "default": 0, "description": "Truncate the result to this length (0 = no limit)"},
        },
    },
    {
        "name": "generic_strip_html",
        "label": "Strip HTML",
        "description": "Remove HTML tags from the value, returning plain text.",
        "params_schema": {
            "default": {"type": "string", "default": "", "description": "Value to use when the result is empty"},
        },
    },
    {
        "name": "generic_length_guard",
        "label": "Length Guard",
        "description": "Enforce a minimum and/or maximum character length on the value.",
        "params_schema": {
            "min_length": {"type": "integer", "default": 0, "description": "Pad up to this length (0 = no padding)"},
            "pad_with": {"type": "string", "default": "", "description": "Filler text appended, repeatedly if needed, to reach min_length"},
            "default": {"type": "string", "default": "", "description": "Text to use when every source field is empty"},
            "max_length": {"type": "integer", "default": 0, "description": "Truncate to this length (0 = no limit)"},
            "limit_margin": {"type": "integer", "default": 0, "description": "Stay this many characters inside both limits, for rules that exclude the boundary"},
            "context_separator": {"type": "string", "default": " - ", "description": "Joins the value to any context fields mapped after the first"},
            "truncation_suffix": {"type": "string", "default": "", "description": "Marker appended to a value that was cut"},
        },
    },
    {
        "name": "generic_default_if_empty",
        "label": "First Non-Empty",
        "description": "Use the first source field that has a value, falling back to a fixed default when they are all empty.",
        "params_schema": {
            "default": {"type": "string", "default": "", "description": "Value to use when every source field is empty"},
        },
    },
    {
        "name": "generic_lookup",
        "label": "ID Lookup",
        "description": "Resolve a source sys_id to a Salesforce record ID via id_mappings.",
        "params_schema": {
            "lookup_tables": {"type": "array", "default": [], "description": "id_mapping table names to search"},
            "default": {"type": "string", "default": "", "description": "Fallback value if not found"},
        },
    },
    {
        "name": "generic_rules",
        "label": "Conditional Rules",
        "description": "Test conditions against source columns in order and use the value of the first rule that matches.",
        "params_schema": {
            "inputs": {"type": "array", "default": [], "description": "Source column names, in the same order the mapping lists them"},
            "rules": {"type": "array", "default": [], "description": "Ordered rules, each with 'all'/'any' conditions and a 'then' outcome"},
            "otherwise": {"type": "object", "default": {}, "description": "Outcome when no rule matches"},
            "case_insensitive": {"type": "boolean", "default": True, "description": "Compare text regardless of case"},
        },
        # The rules array needs its own editor, so a plain parameter form would
        # only ever show a raw blob. Consumers that render params generically
        # skip this one; the column-fill drawer builds it instead.
        "builder_only": True,
    },
    {
        "name": "jsm_extract_first_name",
        "label": "Extract First Name",
        "description": "Split a display name (e.g. 'Mia Krystof') and return everything except the last word as the first name.",
        "params_schema": {},
        "source_types": ["jsm"],
    },
    {
        "name": "jsm_extract_last_name",
        "label": "Extract Last Name",
        "description": "Split a display name (e.g. 'Mia Krystof') and return the last word as the last name.",
        "params_schema": {},
        "source_types": ["jsm"],
    },
]


@router.get("/")
def list_transforms(source_type: str | None = None):
    """The transformation functions a mapping may use, with their parameters.

    A transform without ``source_types`` lives in the shared module and works
    anywhere. The rest are implemented in one source system's module and cannot
    be resolved from another, so offering them to a mapping that could never run
    them only produces a failure part way through the transform.

    Every name listed here is resolvable at transform time. A ``builder_only``
    entry needs a purpose-built editor for its parameters, so callers that
    render a generic parameter form should leave it out of their picker.
    """
    if not source_type:
        return GENERIC_TRANSFORMS
    return [
        transform
        for transform in GENERIC_TRANSFORMS
        if source_type in (transform.get("source_types") or [source_type])
    ]
