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


# ── Type coercion engine ───────────────────────────────────────────────────────
# Maps (normalised_source_type, normalised_dest_type) → (transform_fn, params)
# or None when the pair is truly incompatible and no auto-fix is possible.

TYPE_COERCION_MAP: dict[tuple[str, str], tuple[str, dict] | None] = {
    ("integer", "string"):   ("generic_truncate", {"max_length": 255}),
    ("number", "string"):    ("generic_truncate", {"max_length": 255}),
    ("boolean", "string"):   ("generic_value_map", {"map": {"true": "true", "false": "false"}, "default": ""}),
    ("date", "datetime"):    ("generic_format_datetime", {"input_format": "%Y-%m-%d", "output_format": "%Y-%m-%dT00:00:00.000+0000"}),
    ("datetime", "date"):    ("generic_format_datetime", {"input_format": "%Y-%m-%d %H:%M:%S", "output_format": "%Y-%m-%d"}),
    ("string", "boolean"):   ("generic_value_map", {"map": {"true": "true", "1": "true"}, "default": "false"}),
    ("integer", "boolean"):  ("generic_value_map", {"map": {"1": "true", "0": "false"}, "default": "false"}),
    ("reference", "string"): ("generic_truncate", {"max_length": 255}),
    ("string", "integer"):   None,
    ("string", "number"):    None,
    ("string", "date"):      None,
    ("string", "datetime"):  None,
    ("boolean", "integer"):  None,
    ("boolean", "number"):   None,
    ("boolean", "date"):     None,
    ("boolean", "datetime"): None,
}


def get_type_coercion(source_type: str, destination_type: str) -> dict:
    """Check type compatibility and return coercion info.

    Returns a dict with:
      - ``compatible: True`` when types match or dest is flexible.
      - ``compatible: False, coercible: True, transform, params, warning``
        when an automatic coercion exists.
      - ``compatible: False, coercible: False, warning`` when no auto-fix
        is possible.
    """
    src_norm = _norm_source_type(source_type)
    dest_norm = _norm_sf_type(destination_type)

    if src_norm == dest_norm or dest_norm in FLEXIBLE_TYPES:
        return {"compatible": True}

    coercion = TYPE_COERCION_MAP.get((src_norm, dest_norm))
    if coercion is not None:
        transform_fn, params = coercion
        return {
            "compatible": False,
            "coercible": True,
            "transform": transform_fn,
            "params": params,
            "warning": (
                f"Type mismatch: source '{source_type}' ({src_norm}) → "
                f"destination '{destination_type}' ({dest_norm}). "
                f"Auto-applying '{transform_fn}' to coerce."
            ),
        }

    if (src_norm, dest_norm) in TYPE_COERCION_MAP:
        return {
            "compatible": False,
            "coercible": False,
            "warning": (
                f"Incompatible types: source '{source_type}' ({src_norm}) cannot be "
                f"automatically converted to destination '{destination_type}' ({dest_norm})."
            ),
        }

    return {
        "compatible": False,
        "coercible": False,
        "warning": (
            f"Incompatible types: source '{source_type}' ({src_norm}) → "
            f"destination '{destination_type}' ({dest_norm}). "
            "No automatic coercion available."
        ),
    }


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
class DependencyEdge:
    from_mapping: str
    to_mapping: str
    field: str
    required: bool


@dataclass
class DependencyWarning:
    mapping: str
    field: str
    references: str
    reason: str


@dataclass
class CompatibilityReport:
    source_type: str
    overall_pct: float
    objects: list[ObjectCompatibility] = field(default_factory=list)
    summary: str = ""
    dependency_order: list[str] = field(default_factory=list)
    dependency_edges: list[DependencyEdge] = field(default_factory=list)
    dependency_warnings: list[DependencyWarning] = field(default_factory=list)
    dependency_missing_parents: list[DependencyWarning] = field(default_factory=list)
    dependency_has_cycle: bool = False
    # Hard blockers that must be resolved before launch: required destination
    # fields with no value, and reference fields mapped as raw source copies.
    blocking_required_fields: list[dict] = field(default_factory=list)
    # Non-blocking warnings: picklist destinations with no/partial value map.
    picklist_mapping_warnings: list[dict] = field(default_factory=list)


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
    id_lookup, reference_to, picklist_values}. Used by the schema-discovery and
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
            # Usable as an upsert key: External Id or a unique/standard id field.
            "id_lookup": bool(f.get("idLookup", False)),
            "reference_to": f.get("referenceTo", []),
            "picklist_values": [
                pv.get("value", "")
                for pv in f.get("picklistValues", [])
                if pv.get("active", True)
            ],
        })
    return sorted(fields, key=lambda x: x["name"])


def get_destination_records(
    sf_object: str, sf_credentials: dict, name_field: str = "Name", limit: int = 200
) -> list[dict]:
    """Return up to ``limit`` records of a Salesforce object as ``[{id, name}]``.

    Powers the wizard reference picker (e.g. choose a Profile by name and store
    its Id as a per-field constant). Returns [] on any error so the UI degrades
    gracefully. Mirrors the self-contained token+request flow of ``_sf_describe``.
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

        safe_limit = max(1, min(int(limit), 2000))
        safe_name = "".join(c for c in name_field if c.isalnum() or c == "_") or "Name"
        query = (
            f"SELECT Id, {safe_name} FROM {sf_object} "
            f"ORDER BY {safe_name} LIMIT {safe_limit}"
        )
        query_resp = requests.get(
            f"{instance_url}/services/data/{api_version}/query",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"q": query},
            timeout=20,
        )
        if query_resp.status_code != 200:
            return []
        records = query_resp.json().get("records", [])
        return [
            {"id": r.get("Id", ""), "name": r.get(safe_name, "")}
            for r in records
        ]
    except Exception:
        return []


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
            coercion = get_type_coercion(src_field.field_type, dest.get("type", ""))
            if coercion.get("coercible"):
                transformation_function = coercion["transform"]
                transformation_params = coercion["params"]
                confidence = round(confidence * 0.8, 2)
                match_reason += "; auto-coerced type"
            else:
                confidence = round(confidence * 0.7, 2)
                match_reason += "; type may need coercion"

        type_warning = ""
        if src_norm_type != dest_norm_type and dest_norm_type not in ("", "string"):
            coercion_info = get_type_coercion(src_field.field_type, dest.get("type", ""))
            if not coercion_info.get("compatible"):
                type_warning = coercion_info.get("warning", "")

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
        if type_warning:
            field_entry["type_warning"] = type_warning
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
    """Run a full compatibility analysis and return a structured report.

    Loads mapping configs from the database first (where the wizard saves
    them in Step 4), falling back to static JSON files for backward
    compatibility.
    """
    import services.internal.mappings_service as mappings_service

    results: list[ObjectCompatibility] = []
    blocking_required: list[dict] = []
    picklist_warnings: list[dict] = []

    for obj_name in selected_objects:
        config = mappings_service.get_mapping_config(obj_name)
        if not config:
            continue

        sf_object = config.get("destination_object", "")
        if not sf_object:
            continue

        source_tables = config.get("source_tables", [])
        source_label = source_tables[0] if source_tables else obj_name

        sf_fields = _get_sf_fields(sf_object, destination_credentials)
        explicit_mappings: list[dict] = config.get("fields", [])

        field_results: list[FieldResult] = []
        mapped = 0
        mismatches = 0
        missing_in_source = 0

        if explicit_mappings:
            # Load source field type info for type-mismatch detection
            src_field_types: dict[str, str] = {}
            try:
                from api import sources as source_registry
                provider = source_registry.get(source_type)
                if provider and source_tables:
                    src_field_types = {
                        f.name: f.field_type
                        for f in provider.get_table_fields(source_tables[0], source_credentials)
                    }
            except Exception:
                pass

            for fm in explicit_mappings:
                dest = fm.get("destination_field", "")
                src = fm.get("source_field", "")
                transform_fn = fm.get("transformation_function", "")
                if isinstance(src, list):
                    src_type_raw = src_field_types.get(src[0], "")
                    src = ", ".join(src)
                else:
                    src_type_raw = src_field_types.get(src, "") if src else ""

                dest_type = sf_fields.get(dest, "")
                has_dest = bool(dest_type or dest in sf_fields)

                if not src and (fm.get("constant_value") is not None or fm.get("use_constant")):
                    status = "mapped"
                    suggestion = "Constant value injected."
                    mapped += 1
                    suggested_name = ""
                    inferred_type = "Text"
                elif not has_dest:
                    status = "no_mapping"
                    if not dest.endswith("__c"):
                        suggested_name = ""
                        inferred_type = "Text"
                        suggestion = (
                            f"Standard field '{dest}' not found on {sf_object}. "
                            "Check field-level security or object permissions — "
                            "do not create a custom field for this."
                        )
                    else:
                        suggested_name = _suggest_custom_field_api_name(dest)
                        inferred_type = _infer_field_type(dest, transform_fn)
                        suggestion = (
                            f"Destination field '{dest}' not found in Salesforce {sf_object}. "
                            f"You can create this as a custom {inferred_type} field in Salesforce."
                        )
                    missing_in_source += 1
                else:
                    # Type-mismatch check (skip if a transform is already set)
                    if src_type_raw and dest_type and not transform_fn:
                        coercion = get_type_coercion(src_type_raw, dest_type)
                        if not coercion.get("compatible"):
                            status = "type_mismatch"
                            suggestion = coercion.get("warning", "Type mismatch detected.")
                            mismatches += 1
                            suggested_name = ""
                            inferred_type = "Text"
                        else:
                            status = "mapped"
                            suggestion = ""
                            mapped += 1
                            suggested_name = ""
                            inferred_type = "Text"
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
                    source_type=src_type_raw,
                    destination_type=dest_type,
                    suggestion=suggestion,
                    suggested_custom_field=suggested_name if status == "no_mapping" else "",
                    suggested_field_type=inferred_type if status == "no_mapping" else "Text",
                ))
        else:
            # Generic analysis: compare source fields to SF fields by name similarity.
            # Load source fields on-demand via the provider when available.
            source_fields: dict = {}
            try:
                from api import sources as source_registry
                provider = source_registry.get(source_type)
                if provider and source_tables:
                    source_fields = {
                        f.name: f
                        for f in provider.get_table_fields(source_tables[0], source_credentials)
                    }
            except Exception:
                pass
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

        # ── Launch-blocking gate ────────────────────────────────────────────────
        # Only meaningful for wizard mappings (explicit field list) and when the
        # describe succeeded (empty dest_meta → degrade gracefully, no blocking).
        if explicit_mappings:
            dest_meta = {
                f["name"]: f
                for f in get_destination_fields(sf_object, destination_credentials)
            }

            def _has_value(fm: dict) -> bool:
                return (
                    bool(fm.get("source_field"))
                    or fm.get("constant_value") is not None
                    or bool(fm.get("use_constant"))
                )

            covered = {
                fm.get("destination_field", "")
                for fm in explicit_mappings
                if _has_value(fm)
            }

            # 1) Required + createable destination fields with no value at all.
            for dest_name, meta in dest_meta.items():
                if meta.get("required") and meta.get("createable") and dest_name not in covered:
                    blocking_required.append({
                        "mapping": obj_name,
                        "field": dest_name,
                        "reason": (
                            f"Required field '{dest_name}' on {sf_object} has no source, "
                            "constant, or lookup value. Set it before launching."
                        ),
                    })

            # 2) Reference/lookup destinations mapped as a raw source copy
            #    (no transform, no constant) → guaranteed MALFORMED_ID.
            for fm in explicit_mappings:
                dest_name = fm.get("destination_field", "")
                meta = dest_meta.get(dest_name)
                if not meta or not meta.get("reference_to"):
                    continue
                raw_copy = (
                    bool(fm.get("source_field"))
                    and not fm.get("transformation_function")
                    and fm.get("constant_value") is None
                    and not fm.get("use_constant")
                )
                if raw_copy:
                    blocking_required.append({
                        "mapping": obj_name,
                        "field": dest_name,
                        "reason": (
                            f"Reference field '{dest_name}' is mapped to a raw source value. "
                            "Use a Lookup transform or a Constant record to avoid MALFORMED_ID."
                        ),
                    })

            # 3) Picklist destinations with no/partial value map (non-blocking).
            #    A plain copy or an empty generic_value_map means source values that
            #    don't match the Salesforce picklist get blanked at load time.
            for fm in explicit_mappings:
                dest_name = fm.get("destination_field", "")
                meta = dest_meta.get(dest_name)
                if not meta or not meta.get("picklist_values"):
                    continue
                # Skip constants (a fixed valid value is chosen) and reference fields.
                if fm.get("constant_value") is not None or fm.get("use_constant") or meta.get("reference_to"):
                    continue
                if not fm.get("source_field"):
                    continue
                transform_fn = fm.get("transformation_function", "")
                params = fm.get("transformation_params") or {}
                value_map = params.get("map") if isinstance(params, dict) else None
                if transform_fn == "generic_value_map":
                    if not value_map:
                        picklist_warnings.append({
                            "mapping": obj_name,
                            "field": dest_name,
                            "reason": (
                                f"Picklist '{dest_name}' has an empty value map — source "
                                "values will be blanked. Map values in Step 4."
                            ),
                        })
                    else:
                        unmapped = [k for k, v in value_map.items() if not v]
                        if unmapped:
                            picklist_warnings.append({
                                "mapping": obj_name,
                                "field": dest_name,
                                "reason": (
                                    f"Picklist '{dest_name}' has {len(unmapped)} unmapped "
                                    "source value(s); those rows will be blanked."
                                ),
                            })
                elif not transform_fn:
                    picklist_warnings.append({
                        "mapping": obj_name,
                        "field": dest_name,
                        "reason": (
                            f"Picklist '{dest_name}' is a raw source copy — values that "
                            "don't match the Salesforce picklist will be blanked. Add a "
                            "value map in Step 4."
                        ),
                    })

        results.append(ObjectCompatibility(
            object_name=obj_name,
            source_label=source_label,
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

    # Resolve dependency ordering from Salesforce schema
    dep_result = resolve_dependency_order(selected_objects, destination_credentials)

    return CompatibilityReport(
        source_type=source_type,
        overall_pct=overall,
        objects=results,
        summary=summary,
        dependency_order=dep_result.get("order", []),
        dependency_edges=[
            DependencyEdge(
                from_mapping=e["from"],
                to_mapping=e["to"],
                field=e["field"],
                required=e["required"],
            )
            for e in dep_result.get("edges", [])
        ],
        dependency_warnings=[
            DependencyWarning(**w) for w in dep_result.get("warnings", [])
        ],
        dependency_missing_parents=[
            DependencyWarning(**mp) for mp in dep_result.get("missing_parents", [])
        ],
        dependency_has_cycle=dep_result.get("has_cycle", False),
        blocking_required_fields=blocking_required,
        picklist_mapping_warnings=picklist_warnings,
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


def resolve_dependency_order(
    mapping_names: list[str],
    sf_credentials: dict,
    draft_configs: dict[str, dict] | None = None,
    destination_fields: dict[str, list[dict]] | None = None,
) -> dict:
    """Build a dependency graph from mapped reference fields and topological-sort it.

    For each mapping, describes the Salesforce destination object and inspects
    the user's mapped fields.  When a mapped destination field is a reference
    (``type == "reference"``) whose ``reference_to`` targets another mapping's
    destination object, an edge is recorded.

    ``draft_configs`` maps mapping name -> config and takes precedence over the
    stored configs, so a candidate set of changes can be ordered and validated
    before anything is written to the database.

    ``destination_fields`` maps a destination object to its already-described
    fields; supplying it avoids describing the same objects a second time when the
    caller has them cached.

    Returns a dict::

        {
            "order":           [...],   # topologically sorted mapping names
            "edges":           [...],   # {from, to, field, required}
            "warnings":        [...],   # self-refs, cycles, etc.
            "missing_parents": [...],   # required lookups to unmapped objects
            "has_cycle":       bool,
        }
    """
    import services.internal.mappings_service as mappings_service

    # 1. Load configs and build sf_object -> mapping_name lookup
    drafts = draft_configs or {}
    configs: dict[str, dict] = {}
    sf_obj_to_name: dict[str, str] = {}
    for name in mapping_names:
        cfg = drafts.get(name) or mappings_service.get_mapping_config(name)
        if not cfg:
            continue
        configs[name] = cfg
        dest_obj = cfg.get("destination_object") or cfg.get("salesforce_object", "")
        if dest_obj:
            sf_obj_to_name[dest_obj] = name

    # 2. Describe each unique SF object and index fields by name
    cached_fields = destination_fields or {}
    sf_field_index: dict[str, dict[str, dict]] = {}  # sf_object -> {field_name -> field_meta}
    for sf_obj in sf_obj_to_name:
        if sf_obj not in sf_field_index:
            dest_fields = cached_fields.get(sf_obj)
            if dest_fields is None:
                dest_fields = get_destination_fields(sf_obj, sf_credentials)
            sf_field_index[sf_obj] = {f["name"]: f for f in dest_fields}

    # 3. Walk each mapping's fields and detect reference edges
    edges: list[dict] = []
    warnings: list[dict] = []
    missing_parents: list[dict] = []
    adjacency: dict[str, set[str]] = {n: set() for n in configs}

    for name, cfg in configs.items():
        dest_obj = cfg.get("destination_object") or cfg.get("salesforce_object", "")
        field_meta_map = sf_field_index.get(dest_obj, {})
        mapped_fields = cfg.get("fields", [])

        mapped_dest_names = {
            f.get("destination_field", "") for f in mapped_fields
        }

        for mf in mapped_fields:
            dest_field_name = mf.get("destination_field", "")
            meta = field_meta_map.get(dest_field_name)
            if not meta:
                continue
            if meta.get("type") != "reference":
                continue

            ref_to_list = meta.get("reference_to") or []
            is_required = meta.get("required", False)

            for ref_obj in ref_to_list:
                if ref_obj == dest_obj:
                    warnings.append({
                        "mapping": name,
                        "field": dest_field_name,
                        "references": ref_obj,
                        "reason": (
                            f"Self-referencing lookup — parent {ref_obj} "
                            "records must already exist or a two-pass "
                            "approach is needed"
                        ),
                    })
                    continue

                target_name = sf_obj_to_name.get(ref_obj)
                if target_name and target_name in configs:
                    edges.append({
                        "from": name,
                        "to": target_name,
                        "field": dest_field_name,
                        "required": is_required,
                    })
                    adjacency.setdefault(name, set()).add(target_name)
                elif is_required:
                    missing_parents.append({
                        "mapping": name,
                        "field": dest_field_name,
                        "references": ref_obj,
                        "reason": (
                            f"Required lookup to {ref_obj} but no mapping "
                            "targeting that object is included in this migration"
                        ),
                    })

        # Also check unmapped *required* reference fields for missing-parent warnings
        for field_name, meta in field_meta_map.items():
            if field_name in mapped_dest_names:
                continue
            if meta.get("type") != "reference":
                continue
            if not meta.get("required", False):
                continue
            if not meta.get("createable", False):
                continue
            ref_to_list = meta.get("reference_to") or []
            for ref_obj in ref_to_list:
                if ref_obj != dest_obj and ref_obj not in sf_obj_to_name:
                    missing_parents.append({
                        "mapping": name,
                        "field": field_name,
                        "references": ref_obj,
                        "reason": (
                            f"Required reference field '{field_name}' targets "
                            f"{ref_obj} which is not mapped (field is also not "
                            "included in field mappings)"
                        ),
                    })

    # 4. Topological sort (Kahn's algorithm)
    # adjacency[A] = {B} means "A depends on B" (B must be migrated first).
    # reverse_adj[B] = {A} lets us find dependents when B is scheduled.
    reverse_adj: dict[str, set[str]] = {n: set() for n in configs}
    for name, deps in adjacency.items():
        for dep in deps:
            if dep in configs:
                reverse_adj[dep].add(name)

    in_deg: dict[str, int] = {
        n: len(adjacency.get(n, set()) & set(configs.keys()))
        for n in configs
    }

    queue: list[str] = sorted(n for n, d in in_deg.items() if d == 0)
    topo_order: list[str] = []
    while queue:
        node = queue.pop(0)
        topo_order.append(node)
        for dependent in sorted(reverse_adj.get(node, set())):
            in_deg[dependent] -= 1
            if in_deg[dependent] == 0:
                queue.append(dependent)

    has_cycle = len(topo_order) != len(configs)
    if has_cycle:
        warnings.append({
            "mapping": "",
            "field": "",
            "references": "",
            "reason": "Circular dependency detected — falling back to card order",
        })
        topo_order = list(configs.keys())

    return {
        "order": topo_order,
        "edges": edges,
        "warnings": warnings,
        "missing_parents": missing_parents,
        "has_cycle": has_cycle,
    }


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
