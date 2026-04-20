"""Compatibility engine.

Analyses how well a source ITSM system's objects/fields map onto the
Salesforce data model for the selected objects, returning a structured
report with compatible fields, suggestions for unmatched fields, and
overall compatibility scores.
"""
from __future__ import annotations

import json
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


def _load_mapping_file(object_name: str) -> dict:
    path: Path = PATH_TO_MAPPING / f"{object_name}.json"
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
                if isinstance(src, list):
                    src = ", ".join(src)

                dest_type = sf_fields.get(dest, "")
                has_dest = bool(dest_type or dest in sf_fields)

                if not src and fm.get("use_constant"):
                    # Constant injection – always mapped
                    status = "mapped"
                    suggestion = "Constant value injected."
                    mapped += 1
                elif not has_dest:
                    status = "no_mapping"
                    suggestion = (
                        f"Destination field '{dest}' not found in Salesforce {sf_object}. "
                        "Verify the field exists and is accessible by the integration user."
                    )
                    missing_in_source += 1
                else:
                    status = "mapped"
                    suggestion = ""
                    mapped += 1

                field_results.append(FieldResult(
                    source_field=src,
                    destination_field=dest,
                    status=status,
                    source_type="",
                    destination_type=dest_type,
                    suggestion=suggestion,
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
                    if src_type != sf_field_type and sf_field_type not in ("", "string"):
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

def _find_similar(target: str, candidates: list[str]) -> str | None:
    """Find the most similar candidate to the target using a simple heuristic."""
    target_lower = target.replace("__c", "").replace("_", "").lower()
    best: str | None = None
    best_score = 0
    for c in candidates:
        c_base = c.split(".")[-1].replace("_", "").lower()
        # Longest common prefix length as a quick similarity measure
        score = sum(a == b for a, b in zip(target_lower, c_base))
        if score > best_score and score >= 3:
            best_score = score
            best = c
    return best
