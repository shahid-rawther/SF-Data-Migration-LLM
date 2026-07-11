"""Standalone, schema-driven transformation functions.

These transforms are object-agnostic and configured entirely via ``params`` in
the mapping, so they can be used by any source/destination without bespoke,
hardcoded per-object logic. This module intentionally has NO dependency on the
legacy ``integrations/servicenow/transformations.py`` module, so the wizard
runtime can execute a migration without importing any hardcoded ITSM code.

Every transform returns ``{destination_field: value}`` and accepts the incoming
source value(s) plus an optional ``params`` dict.
"""
from datetime import datetime

import pandas as pd
from bs4 import BeautifulSoup


def _clean_value(value) -> str:
    """Normalise an incoming source value to a plain string ('' for NaN/None)."""
    try:
        if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    if not bool(value):
        return ""
    return str(value).strip() if isinstance(value, str) else str(value)


def generic_default_if_empty(destination_field: str, *values, params: dict | None = None):
    """Use the first non-empty source value, else a configured default.

    params: {"default": "Not provided"}
    """
    params = params or {}
    for v in values:
        cleaned = _clean_value(v)
        if cleaned:
            return {destination_field: cleaned}
    return {destination_field: params.get("default", "")}


def generic_truncate(destination_field: str, value=None, params: dict | None = None):
    """Truncate a string to a maximum length, optionally appending a suffix.

    params: {"max_length": 255, "suffix": "..."}
    The suffix (if any) is included within the max_length budget.
    """
    params = params or {}
    max_length = int(params.get("max_length", 255))
    suffix = params.get("suffix", "")
    text = _clean_value(value)
    if len(text) <= max_length:
        return {destination_field: text}
    if suffix and len(suffix) < max_length:
        return {destination_field: text[: max_length - len(suffix)] + suffix}
    return {destination_field: text[:max_length]}


def generic_value_map(destination_field: str, value=None, params: dict | None = None):
    """Map a source value to a destination value using a lookup table.

    params: {
        "map": {"1": "High", "2": "Medium"},
        "default": "Medium",          # optional, used when no key matches
        "case_insensitive": true,     # optional
        "passthrough_if_missing": false  # optional: keep original instead of default
    }
    """
    params = params or {}
    mapping = params.get("map", {}) or {}
    case_insensitive = bool(params.get("case_insensitive", False))
    raw = _clean_value(value)

    if case_insensitive:
        lowered = {str(k).lower(): v for k, v in mapping.items()}
        if raw.lower() in lowered:
            return {destination_field: lowered[raw.lower()]}
    elif raw in mapping:
        return {destination_field: mapping[raw]}

    if params.get("passthrough_if_missing"):
        return {destination_field: raw}
    return {destination_field: params.get("default", "")}


def generic_format_datetime(destination_field: str, value=None, params: dict | None = None):
    """Reformat a datetime string between input/output strftime patterns.

    params: {
        "input_format": "%Y-%m-%d %H:%M:%S",   # optional, default ServiceNow format
        "output_format": "%Y-%m-%dT%H:%M:%S.000+0000"  # optional, default SF format
    }
    Returns '' for empty/unparseable values.
    """
    params = params or {}
    input_format = params.get("input_format", "%Y-%m-%d %H:%M:%S")
    output_format = params.get("output_format", "%Y-%m-%dT%H:%M:%S.000+0000")
    raw = _clean_value(value)
    if not raw:
        return {destination_field: ""}
    try:
        parsed = datetime.strptime(raw, input_format)
        return {destination_field: parsed.strftime(output_format)}
    except (ValueError, TypeError):
        return {destination_field: ""}


def generic_concat(destination_field: str, *values, params: dict | None = None):
    """Concatenate multiple source field values with a separator.

    params: {
        "separator": " ",           # optional
        "skip_empty": true,          # optional
        "min_length": 30,            # optional: pad the result up to this length
        "pad_with": " (no details)", # optional: filler appended to reach min_length
        "max_length": 255            # optional: truncate the result to this length
    }
    Replaces bespoke subject builders (e.g. populate_subject) — the min_length
    padding and max_length truncation enforce destination field limits.
    """
    params = params or {}
    separator = params.get("separator", " ")
    skip_empty = params.get("skip_empty", True)
    parts = []
    for v in values:
        cleaned = _clean_value(v)
        if cleaned or not skip_empty:
            parts.append(cleaned)
    result = separator.join(parts)

    min_length = int(params.get("min_length", 0) or 0)
    pad_with = params.get("pad_with", "")
    if min_length and len(result) < min_length and pad_with:
        while len(result) < min_length:
            result = f"{result} {pad_with}".strip() if result else pad_with

    max_length = int(params.get("max_length", 0) or 0)
    if max_length and len(result) > max_length:
        result = result[:max_length]

    return {destination_field: result}


def generic_strip_html(destination_field: str, value=None, params: dict | None = None):
    """Strip HTML tags from a value, returning plain text.

    params: {"default": "..."}  # optional value when the result is empty
    Replaces remove_html and the HTML-stripping part of convert_resolution_summary.
    """
    params = params or {}
    raw = _clean_value(value)
    if not raw:
        return {destination_field: params.get("default", "")}
    text = BeautifulSoup(raw, "html.parser").get_text().strip()
    if not text:
        return {destination_field: params.get("default", "")}
    return {destination_field: text}


def generic_length_guard(destination_field: str, value=None, params: dict | None = None):
    """Enforce min/max length on a string value.

    params: {
        "min_length": 30,            # optional: pad up to this length
        "pad_with": " (no details)", # optional: filler appended to reach min_length
        "max_length": 990            # optional: truncate to this length
    }
    Replaces sanitize_description-style length enforcement.
    """
    params = params or {}
    text = _clean_value(value)

    min_length = int(params.get("min_length", 0) or 0)
    pad_with = params.get("pad_with", "")
    if min_length and len(text) < min_length and pad_with:
        while len(text) < min_length:
            text = f"{text} {pad_with}".strip() if text else pad_with

    max_length = int(params.get("max_length", 0) or 0)
    if max_length and len(text) > max_length:
        text = text[:max_length]

    return {destination_field: text}


def generic_lookup(destination_field: str, value=None, params: dict | None = None):
    """Resolve a source id to a destination id via the id_mappings table.

    Replaces most bespoke look_up_* functions for new clients. The lookup is
    cached per table-set for the duration of a migration run.

    params: {
        "lookup_table": "sys_user_group_Group_",   # a single id_mappings table_name
        # or
        "lookup_tables": ["tableA", "tableB"],       # search multiple tables
        "default": ""                                # optional fallback value
    }
    """
    # Lazy imports: these are internal services (not hardcoded ITSM logic) and
    # importing them lazily keeps this module free of import-time side effects.
    from services.internal import mappings_service
    from tools.cache import execution_cache

    params = params or {}
    tables = params.get("lookup_tables")
    if not tables:
        single = params.get("lookup_table")
        tables = [single] if single else []
    sys_id = _clean_value(value)

    if not tables or not sys_id:
        return {destination_field: params.get("default", "")}

    def _load_all_id_mappings():
        rows = mappings_service.get_sales_force_id_mappings_for_tables(tables)
        return {row[1]: row[0] for row in rows}

    cache_key = "ID_MAPPING::GENERIC::" + "#".join(sorted(tables))
    mappings: dict[str, str] = execution_cache.get_or_set(
        cache_key, lambda: _load_all_id_mappings()
    )
    sales_force_id = mappings.get(sys_id)
    return {destination_field: sales_force_id if sales_force_id else params.get("default", "")}


# Registry of all schema-driven transforms, keyed by the name used in mapping
# configs. The transformation-service resolver consults this first so wizard
# mappings never need to import the legacy transformations module.
GENERIC_TRANSFORMS = {
    "generic_default_if_empty": generic_default_if_empty,
    "generic_truncate": generic_truncate,
    "generic_value_map": generic_value_map,
    "generic_format_datetime": generic_format_datetime,
    "generic_concat": generic_concat,
    "generic_strip_html": generic_strip_html,
    "generic_length_guard": generic_length_guard,
    "generic_lookup": generic_lookup,
}
