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
        "description": "Join multiple source field values with a separator.",
        "params_schema": {
            "separator": {"type": "string", "default": " ", "description": "String to place between field values"},
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
]


@router.get("/")
def list_transforms():
    """Return available generic transformation functions with their parameter schemas."""
    return GENERIC_TRANSFORMS
