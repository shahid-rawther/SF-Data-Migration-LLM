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
        },
    },
    {
        "name": "generic_truncate",
        "label": "Truncate",
        "description": "Truncate the value to a maximum character length.",
        "params_schema": {
            "max_length": {"type": "integer", "default": 255, "description": "Maximum number of characters"},
        },
    },
    {
        "name": "generic_concat",
        "label": "Concatenate",
        "description": "Join multiple source field values with a separator; optionally pad to a minimum length and cap at a maximum length.",
        "params_schema": {
            "separator": {"type": "string", "default": " ", "description": "String to place between field values"},
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
            "pad_with": {"type": "string", "default": "", "description": "Filler text appended to reach min_length"},
            "max_length": {"type": "integer", "default": 0, "description": "Truncate to this length (0 = no limit)"},
        },
    },
    {
        "name": "generic_default_value",
        "label": "Default Value",
        "description": "Use a fixed default value when the source field is empty or null.",
        "params_schema": {
            "value": {"type": "string", "default": "", "description": "The default value to use"},
        },
    },
    {
        "name": "generic_prefix",
        "label": "Add Prefix",
        "description": "Add a prefix string before the source value.",
        "params_schema": {
            "prefix": {"type": "string", "default": "", "description": "The prefix to prepend"},
        },
    },
    {
        "name": "generic_suffix",
        "label": "Add Suffix",
        "description": "Add a suffix string after the source value.",
        "params_schema": {
            "suffix": {"type": "string", "default": "", "description": "The suffix to append"},
        },
    },
    {
        "name": "generic_regex_replace",
        "label": "Regex Replace",
        "description": "Apply a regular expression substitution on the source value.",
        "params_schema": {
            "pattern": {"type": "string", "default": "", "description": "Regex pattern to match"},
            "replacement": {"type": "string", "default": "", "description": "Replacement string"},
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
        "name": "jsm_extract_first_name",
        "label": "Extract First Name",
        "description": "Split a display name (e.g. 'Mia Krystof') and return everything except the last word as the first name.",
        "params_schema": {},
    },
    {
        "name": "jsm_extract_last_name",
        "label": "Extract Last Name",
        "description": "Split a display name (e.g. 'Mia Krystof') and return the last word as the last name.",
        "params_schema": {},
    },
]


@router.get("/")
def list_transforms():
    """Return available generic transformation functions with their parameter schemas."""
    return GENERIC_TRANSFORMS
