"""Compatibility engine.

Analyses how well a source ITSM system's objects/fields map onto the
Salesforce data model for the selected objects, returning a structured
report with compatible fields, suggestions for unmatched fields, and
overall compatibility scores.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import PATH_TO_MAPPING


# ── Salesforce → generic type normalisation ────────────────────────────────────

_SF_TYPE_MAP: dict[str, str] = {
    "string": "string",
    "textarea": "string",
    "email": "string",
    "phone": "string",
    "url": "string",
    "picklist": "string",
    "multipicklist": "string",
    "id": "string",
    "reference": "reference",
    "boolean": "boolean",
    "checkbox": "boolean",
    "int": "integer",
    "integer": "integer",
    "double": "number",
    "currency": "number",
    "percent": "number",
    "date": "date",
    "datetime": "datetime",
    "time": "datetime",
}

# Types that are treated as compatible with any source type
# (empty string means the SF schema did not return a type for the field)
FLEXIBLE_TYPES = ("", "string")


def _norm_sf_type(sf_type: str) -> str:
    return _SF_TYPE_MAP.get((sf_type or "").lower(), "string")


# ── Data classes ────────────────────────────────────────────────────────────────

@dataclass
class FieldResult:
    source_field: str
    destination_field: str
    status: str          # "mapped" | "type_mismatch" | "missing_in_source" | "no_mapping"
    source_type: str
    destination_type: str
    suggestion: str      # Human-readable guidance
    suggested_custom_field: str = ""   # Suggested custom field API name (only for no_mapping)
    suggested_field_type: str = "Text" # Inferred Salesforce field type for creation


@dataclass
class ObjectCompatibility:
    object_name: str
    source_label: str
    salesforce_object: str
    total_fields: int
    mapped_fields: int
    missing_fields: int
    type_mismatches: int
    compatibility_pct: float
    fields: list[FieldResult] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


@dataclass
class CompatibilityReport:
    source_type: str
    overall_pct: float
    objects: list[ObjectCompatibility] = field(default_factory=list)
    summary: str = ""


# ── Salesforce schema helpers ──────────────────────────────────────────────────

def _get_sf_fields(sf_object: str, sf_credentials: dict) -> dict[str, str]:
    """Return {field_name: normalised_type} for the given Salesforce object.

    Falls back to an empty dict on any error so that the compat check
    still produces a partial report without crashing.
    """
    try:
        import requests

        base_url = (sf_credentials.get("base_url") or "").rstrip("/")
        client_id = sf_credentials.get("client_id", "")
        client_secret = sf_credentials.get("client_secret", "")
        api_version = sf_credentials.get("api_version", "v60.0")

        if not (base_url and client_id and client_secret):
            return {}

        # Obtain access token
        token_resp = requests.post(
            f"{base_url}/services/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=12,
        )
        if token_resp.status_code != 200:
            return {}
        access_token = token_resp.json().get("access_token", "")
        instance_url = (token_resp.json().get("instance_url") or base_url).rstrip("/")

        describe_resp = requests.get(
            f"{instance_url}/services/data/{api_version}/sobjects/{sf_object}/describe",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        if describe_resp.status_code != 200:
            return {}
        return {
            f["name"]: _norm_sf_type(f.get("type", ""))
            for f in describe_resp.json().get("fields", [])
        }
    except Exception:
        return {}


def _sf_describe(sf_object: str, sf_credentials: dict) -> list[dict]:
    """Return the raw Salesforce describe `fields` list for an object.

    Returns [] on any error so callers can degrade gracefully.
    """
    try:
        import requests

        base_url = (sf_credentials.get("base_url") or "").rstrip("/")
        client_id = sf_credentials.get("client_id", "")
        client_secret = sf_credentials.get("client_secret", "")
        api_version = sf_credentials.get("api_version", "v60.0")

        if not (base_url and client_id and client_secret):
            return []

        token_resp = requests.post(
            f"{base_url}/services/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=12,
        )
        if token_resp.status_code != 200:
            return []
        access_token = token_resp.json().get("access_token", "")
        instance_url = (token_resp.json().get("instance_url") or base_url).rstrip("/")

        describe_resp = requests.get(
            f"{instance_url}/services/data/{api_version}/sobjects/{sf_object}/describe",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        if describe_resp.status_code != 200:
            return []
        return describe_resp.json().get("fields", [])
    except Exception:
        return []


def get_destination_fields(sf_object: str, sf_credentials: dict) -> list[dict]:
    """Return rich field metadata for a Salesforce object.

    Each entry: {name, label, type, length, required, createable, updateable,
    reference_to, picklist_values}. Used by the schema-discovery and
    auto-suggest APIs.
    """
    fields: list[dict] = []
    for f in _sf_describe(sf_object, sf_credentials):
        required = (not f.get("nillable", True)) and (not f.get("defaultedOnCreate", False))
        fields.append({
            "name": f.get("name", ""),
            "label": f.get("label", f.get("name", "")),
            "type": f.get("type", ""),
            "length": f.get("length", 0),
            "required": bool(required),
            "createable": bool(f.get("createable", False)),
            "updateable": bool(f.get("updateable", False)),
            "reference_to": f.get("referenceTo", []),
            "picklist_values": [
                pv.get("value", "")
                for pv in f.get("picklistValues", [])
                if pv.get("active", True)
            ],
        })
    return sorted(fields, key=lambda x: x["name"])


# ── Auto-suggest mapping ────────────────────────────────────────────────────────

# Generic source type → normalised type, mirrors _norm_sf_type for the SF side.
_SOURCE_TYPE_MAP: dict[str, str] = {
    "string": "string",
    "glide_date_time": "datetime",
    "glide_date": "date",
    "due_date": "datetime",
    "integer": "integer",
    "decimal": "number",
    "float": "number",
    "boolean": "boolean",
    "reference": "reference",
    "glide_list": "string",
    "journal": "string",
    "journal_input": "string",
    "html": "string",
    "translated_text": "string",
    "choice": "string",
}


def _norm_source_type(src_type: str) -> str:
    return _SOURCE_TYPE_MAP.get((src_type or "").lower(), "string")


def _short_name(field_name: str) -> str:
    """Return the comparable short name (strip table prefix and __c suffix)."""
    base = field_name.split(".")[-1]
    return base.replace("__c", "").replace("_", "").lower()


def auto_suggest_mapping(
    source_type: str,
    source_table: str,
    salesforce_object: str,
    source_credentials: dict,
    destination_credentials: dict,
    other_mappings: list[dict] | None = None,
) -> dict:
    """Generate a draft mapping config by matching source fields to Salesforce
    fields using name similarity and type compatibility.

    ``other_mappings`` is an optional list of dicts with keys
    ``source_table``, ``sf_object``, ``mapping_name`` describing the other
    tables being mapped in the same wizard session.  When a source field is a
    reference to one of those tables, a ``generic_lookup`` transform is emitted
    instead of a plain name match.

    Returns a mapping dict in the same shape as the JSON mapping files, with an
    extra per-field `confidence` (0-1) and `match_reason` for UI review.
    """
    import difflib
    from api import sources as source_registry

    provider = source_registry.get(source_type)
    source_fields = (
        provider.get_table_fields(source_table, source_credentials) if provider else []
    )
    dest_fields = get_destination_fields(salesforce_object, destination_credentials)

    # Build a lookup from SN table -> (sf_object, mapping_name) using
    # the other mappings context provided by the wizard.
    ref_target_map: dict[str, dict] = {}
    for om in (other_mappings or []):
        sn_table = om.get("source_table", "")
        if sn_table:
            ref_target_map[sn_table] = om

    # Index source reference fields by their reference_table for quick access.
    src_ref_by_table: dict[str, list] = {}
    for sf in source_fields:
        ref_tbl = getattr(sf, "reference_table", "")
        if sf.field_type == "reference" and ref_tbl:
            src_ref_by_table.setdefault(ref_tbl, []).append(sf)

    # Index source fields by their comparable short name for quick lookup.
    src_by_short: dict[str, list] = {}
    for sf in source_fields:
        src_by_short.setdefault(_short_name(sf.name), []).append(sf)
    src_short_names = list(src_by_short.keys())

    # Track which source fields have already been emitted as reference lookups
    # so they are not duplicated by the normal name-matching loop.
    emitted_source_fields: set[str] = set()

    suggested_fields: list[dict] = []

    # ── Pass 1: reference-field lookups ─────────────────────────────────────
    for ref_table, ref_fields in src_ref_by_table.items():
        target_om = ref_target_map.get(ref_table)
        if not target_om:
            continue
        target_sf_obj = target_om.get("sf_object", "")
        target_mapping = target_om.get("mapping_name", "")
        if not target_sf_obj:
            continue
        lookup_table_name = f"{ref_table}_{target_sf_obj}_{target_mapping}"
        for src_f in ref_fields:
            src_short = _short_name(src_f.name)
            # Try to find a matching destination field (reference type or name match)
            dest_match = None
            for dest in dest_fields:
                if not dest.get("createable"):
                    continue
                d_short = _short_name(dest["name"])
                if d_short == src_short:
                    dest_match = dest
                    break
                ref_to = dest.get("reference_to") or []
                if target_sf_obj in ref_to:
                    d_short_loose = _short_name(dest["name"])
                    if difflib.SequenceMatcher(None, d_short_loose, src_short).ratio() > 0.5:
                        dest_match = dest
                        break
            if not dest_match:
                continue
            field_entry: dict = {
                "source_field": src_f.name,
                "destination_field": dest_match["name"],
                "confidence": 0.85,
                "match_reason": f"reference lookup -> {ref_table}",
                "transformation_function": "generic_lookup",
                "transformation_params": {"lookup_tables": [lookup_table_name], "default": ""},
            }
            suggested_fields.append(field_entry)
            emitted_source_fields.add(src_f.name)

    # ── Pass 2: normal name matching ────────────────────────────────────────
    for dest in dest_fields:
        if not dest.get("createable"):
            continue
        dest_name = dest["name"]
        dest_short = _short_name(dest_name)
        dest_norm_type = _norm_sf_type(dest.get("type", ""))

        confidence = 0.0
        match_reason = ""
        matched_source = ""

        # 1. Exact short-name match
        if dest_short in src_by_short:
            matched_source = src_by_short[dest_short][0].name
            confidence = 0.95
            match_reason = "name match"
        else:
            # 2. Fuzzy name match
            close = difflib.get_close_matches(dest_short, src_short_names, n=1, cutoff=0.6)
            if close:
                matched_source = src_by_short[close[0]][0].name
                ratio = difflib.SequenceMatcher(None, dest_short, close[0]).ratio()
                confidence = round(0.6 + 0.3 * ratio, 2)
                match_reason = "fuzzy name match"

        if not matched_source:
            continue
        if matched_source in emitted_source_fields:
            continue

        # Adjust confidence by type compatibility.
        src_field = src_by_short[_short_name(matched_source)][0]
        src_norm_type = _norm_source_type(src_field.field_type)
        transformation_function = ""
        transformation_params = None

        if src_norm_type == dest_norm_type:
            pass
        elif dest_norm_type in ("", "string"):
            pass
        elif dest_norm_type == "datetime" and src_norm_type in ("datetime", "date"):
            transformation_function = "generic_format_datetime"
            transformation_params = {"input_format": "%Y-%m-%d %H:%M:%S", "output_format": "%Y-%m-%dT%H:%M:%S.000+0000"}
        elif dest_norm_type == "string" and dest.get("length", 0) > 0:
            src_len_hint = 255
            if src_len_hint > dest["length"]:
                transformation_function = "generic_truncate"
                transformation_params = {"max_length": dest["length"]}
        elif dest.get("picklist_values"):
            transformation_function = "generic_value_map"
            transformation_params = {"map": {}, "default": ""}
            confidence = round(confidence * 0.8, 2)
            match_reason += "; picklist — fill value map"
        else:
            confidence = round(confidence * 0.7, 2)
            match_reason += "; type may need coercion"

        field_entry = {
            "source_field": matched_source,
            "destination_field": dest_name,
            "confidence": confidence,
            "match_reason": match_reason,
        }
        if transformation_function:
            field_entry["transformation_function"] = transformation_function
        if transformation_params is not None:
            field_entry["transformation_params"] = transformation_params
        suggested_fields.append(field_entry)

    suggested_fields.sort(key=lambda f: f["confidence"], reverse=True)

    return {
        "name": salesforce_object,
        "source_type": source_type,
        "destination_object": salesforce_object,
        "source_tables": [source_table],
        "salesforce_object": salesforce_object,
        "servicenow_tables": [source_table],
        "operation": "insert",
        "joins": [],
        "filters": [],
        "priority": 9999,
        "fields": suggested_fields,
        "_meta": {
            "source_field_count": len(source_fields),
            "destination_field_count": len(dest_fields),
            "suggested_count": len(suggested_fields),
        },
    }


def _load_mapping_file(object_name: str) -> dict:
    """Load the field-mapping JSON for object_name.

    The name is validated against a safe-character allowlist and the
    resolved path is verified to remain inside PATH_TO_MAPPING before
    the file is opened, preventing path-traversal attacks.
    """
    import re
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", object_name):
        return {}
    path: Path = (PATH_TO_MAPPING / f"{object_name}.json").resolve()
    # Ensure the resolved path stays inside the allowed mapping directory
    try:
        path.relative_to(PATH_TO_MAPPING.resolve())
    except ValueError:
        return {}
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


# ── Public API ─────────────────────────────────────────────────────────────────

def check_compatibility(
    source_type: str,
    source_credentials: dict,
    destination_credentials: dict,
    selected_objects: list[str],
) -> CompatibilityReport:
    """Run a full compatibility analysis and return a structured report."""
    from api import sources as source_registry

    provider = source_registry.get(source_type)
    if not provider:
        return CompatibilityReport(
            source_type=source_type,
            overall_pct=0.0,
            summary=f"Unknown source type: {source_type}",
        )

    all_objects = {o.name: o for o in provider.get_objects()}
    results: list[ObjectCompatibility] = []

    for obj_name in selected_objects:
        obj_meta = all_objects.get(obj_name)
        if not obj_meta:
            continue

        sf_object = obj_meta.salesforce_object
        sf_fields = _get_sf_fields(sf_object, destination_credentials)
        source_fields = {f.name: f for f in provider.get_source_fields(obj_name, source_credentials)}

        # Load the explicit field-mapping file when available (ServiceNow style)
        mapping_file = _load_mapping_file(obj_name)
        explicit_mappings: list[dict] = mapping_file.get("fields", [])

        field_results: list[FieldResult] = []
        mapped = 0
        mismatches = 0
        missing_in_source = 0

        if explicit_mappings:
            # Use declared mappings for analysis
            for fm in explicit_mappings:
                dest = fm.get("destination_field", "")
                src = fm.get("source_field", "")
                transform_fn = fm.get("transformation_function", "")
                if isinstance(src, list):
                    src = ", ".join(src)

                dest_type = sf_fields.get(dest, "")
                has_dest = bool(dest_type or dest in sf_fields)

                if not src and fm.get("use_constant"):
                    # Constant injection – always mapped
                    status = "mapped"
                    suggestion = "Constant value injected."
                    mapped += 1
                    suggested_name = ""
                    inferred_type = "Text"
                elif not has_dest:
                    status = "no_mapping"
                    if not dest.endswith("__c"):
                        # Standard Salesforce field (e.g. ProductId, OwnerId, ContactId).
                        # These already exist in Salesforce — never suggest creating a
                        # custom duplicate. A missing standard field means a permissions
                        # or org configuration issue, not a missing custom field.
                        suggested_name = ""
                        inferred_type = "Text"
                        suggestion = (
                            f"Standard field '{dest}' not found on {sf_object}. "
                            "Check field-level security or object permissions — "
                            "do not create a custom field for this."
                        )
                    else:
                        # Genuine custom field that needs to be created.
                        suggested_name = _suggest_custom_field_api_name(dest)
                        inferred_type = _infer_field_type(dest, transform_fn)
                        suggestion = (
                            f"Destination field '{dest}' not found in Salesforce {sf_object}. "
                            f"You can create this as a custom {inferred_type} field in Salesforce."
                        )
                    missing_in_source += 1
                else:
                    status = "mapped"
                    suggestion = ""
                    mapped += 1
                    suggested_name = ""
                    inferred_type = "Text"

                field_results.append(FieldResult(
                    source_field=src,
                    destination_field=dest,
                    status=status,
                    source_type="",
                    destination_type=dest_type,
                    suggestion=suggestion,
                    suggested_custom_field=suggested_name if status == "no_mapping" else "",
                    suggested_field_type=inferred_type if status == "no_mapping" else "Text",
                ))
        else:
            # Generic analysis: compare source fields to SF fields by name similarity
            for sf_field_name, sf_field_type in sf_fields.items():
                # Direct name match
                direct_match = sf_field_name in source_fields
                # Loose match: strip trailing underscore suffixes (__c) and compare lower
                loose_key = sf_field_name.replace("__c", "").lower()
                loose_match = any(
                    s.split(".")[-1].lower() == loose_key for s in source_fields
                )

                if direct_match or loose_match:
                    src_name = sf_field_name if direct_match else next(
                        (s for s in source_fields if s.split(".")[-1].lower() == loose_key), sf_field_name
                    )
                    src_field = source_fields.get(src_name)
                    src_type = src_field.field_type if src_field else "string"
                    if src_type != sf_field_type and sf_field_type not in FLEXIBLE_TYPES:
                        status = "type_mismatch"
                        suggestion = (
                            f"Source type '{src_type}' may need coercion to Salesforce "
                            f"type '{sf_field_type}'."
                        )
                        mismatches += 1
                    else:
                        status = "mapped"
                        suggestion = ""
                        mapped += 1
                    field_results.append(FieldResult(
                        source_field=src_name,
                        destination_field=sf_field_name,
                        status=status,
                        source_type=src_type,
                        destination_type=sf_field_type,
                        suggestion=suggestion,
                    ))
                else:
                    # Check if the source has a similar field to suggest a rename
                    suggestion_src = _find_similar(sf_field_name, list(source_fields.keys()))
                    if suggestion_src:
                        suggestion = (
                            f"No direct mapping found. Consider mapping source field "
                            f"'{suggestion_src}' → '{sf_field_name}'."
                        )
                    else:
                        suggestion = (
                            f"Field '{sf_field_name}' has no equivalent in the source. "
                            "It may need to be set via a constant or custom transformation."
                        )
                    missing_in_source += 1
                    field_results.append(FieldResult(
                        source_field="",
                        destination_field=sf_field_name,
                        status="missing_in_source",
                        source_type="",
                        destination_type=sf_field_type,
                        suggestion=suggestion,
                    ))

        total = len(field_results) or 1
        compat_pct = round(mapped / total * 100, 1)

        blockers: list[str] = []
        if not sf_fields:
            blockers.append(
                f"Could not describe Salesforce object '{sf_object}'. "
                "Check Salesforce credentials and object permissions."
            )

        results.append(ObjectCompatibility(
            object_name=obj_name,
            source_label=obj_meta.label,
            salesforce_object=sf_object,
            total_fields=total,
            mapped_fields=mapped,
            missing_fields=missing_in_source,
            type_mismatches=mismatches,
            compatibility_pct=compat_pct,
            fields=field_results,
            blockers=blockers,
        ))

    overall = round(sum(o.compatibility_pct for o in results) / max(len(results), 1), 1)
    summary = (
        f"Checked {len(results)} object(s). "
        f"Overall compatibility: {overall}%. "
        f"Objects with 100% mapping: {sum(1 for o in results if o.compatibility_pct == 100.0)}."
    )

    return CompatibilityReport(
        source_type=source_type,
        overall_pct=overall,
        objects=results,
        summary=summary,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

# Transformation functions that imply a specific Salesforce field type
_TRANSFORM_TO_SF_TYPE: dict[str, str] = {
    "convert_to_sf_time_format": "DateTime",
    "convert_to_boolean": "Checkbox",
    "check_body_length_limit": "LongTextArea",
    "check_header_length_limit": "LongTextArea",
}

# Destination field name suffixes/keywords → Salesforce field type (checked lower-case)
_NAME_SUFFIX_TO_SF_TYPE: list[tuple[str, str]] = [
    # DateTime patterns
    ("_on__c", "DateTime"),
    ("_at__c", "DateTime"),
    ("_date__c", "DateTime"),
    ("_datetime__c", "DateTime"),
    ("_time__c", "DateTime"),
    # Date-only patterns (no __c suffix — standard fields)
    ("date", "Date"),
    # Checkbox patterns
    ("_active__c", "Checkbox"),
    ("_enabled__c", "Checkbox"),
    ("_flag__c", "Checkbox"),
    ("_boolean__c", "Checkbox"),
    # Number patterns
    ("_count__c", "Number"),
    ("_number__c", "Number"),
    ("_size__c", "Number"),
    ("_amount__c", "Number"),
    # Long text patterns
    ("_notes__c", "LongTextArea"),
    ("_body__c", "LongTextArea"),
    ("_description__c", "LongTextArea"),
    ("_comments__c", "LongTextArea"),
]


def _infer_field_type(dest_field: str, transformation_function: str = "") -> str:
    """Infer the best Salesforce custom field type from the transformation
    function (most reliable) and the destination field name (fallback).

    Returns a Salesforce field type string compatible with the Tooling API:
    Text | DateTime | Date | Checkbox | Number | LongTextArea
    """
    if transformation_function and transformation_function in _TRANSFORM_TO_SF_TYPE:
        return _TRANSFORM_TO_SF_TYPE[transformation_function]

    dest_lower = dest_field.lower()
    for suffix, sf_type in _NAME_SUFFIX_TO_SF_TYPE:
        if dest_lower.endswith(suffix):
            return sf_type

    return "Text"


def _suggest_custom_field_api_name(field_name: str) -> str:
    """Suggest a Salesforce custom field API name from a destination field name.

    If the field already ends with __c, it's already a custom field name.
    Otherwise, convert it to a valid custom field API name.
    """
    if field_name.endswith("__c"):
        return field_name
    # Convert to custom field format
    name = re.sub(r"[^A-Za-z0-9_]", "_", field_name)
    name = re.sub(r"_+", "_", name).strip("_")
    parts = name.split("_")
    name = "_".join(p.capitalize() for p in parts if p)
    if not name.endswith("__c"):
        name = name + "__c"
    return name


def _find_similar(target: str, candidates: list[str]) -> str | None:
    """Find the most similar candidate to the target using difflib."""
    target_key = target.replace("__c", "").replace("_", "").lower()
    candidate_keys = {c: c.split(".")[-1].replace("_", "").lower() for c in candidates}
    matches = difflib.get_close_matches(
        target_key,
        list(candidate_keys.values()),
        n=1,
        cutoff=0.5,
    )
    if not matches:
        return None
    # Map the matched key back to the original candidate name
    for orig, key in candidate_keys.items():
        if key == matches[0]:
            return orig
    return None
