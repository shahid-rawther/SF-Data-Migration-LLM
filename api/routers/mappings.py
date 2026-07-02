"""Mappings router — CRUD + auto-suggest for dynamic field mappings.

Mappings are stored in the mapping_configs DB table (seeded from the shipped
JSON files). This router lets the Mapping Editor list, view, create, update,
delete, validate and auto-generate mappings without code changes.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api import connections_db
from api.compat_engine import auto_suggest_mapping, get_destination_fields
from services.internal import mappings_service

router = APIRouter(prefix="/mappings", tags=["mappings"])


def _resolve_credentials(credentials: dict | None, connection_id: str | None) -> dict:
    if connection_id:
        conn = connections_db.get_connection(connection_id)
        if not conn:
            raise HTTPException(status_code=404, detail="Connection not found")
        return conn.get("credentials", {})
    return credentials or {}


class SaveMappingRequest(BaseModel):
    config: dict
    source_type: str = "servicenow"
    priority: int | None = None


class CreateMappingRequest(BaseModel):
    name: str
    config: dict
    source_type: str = "servicenow"
    priority: int | None = None


class AutoSuggestRequest(BaseModel):
    source_type: str = "servicenow"
    source_table: str
    salesforce_object: str
    source_credentials: dict = {}
    source_connection_id: str | None = None
    destination_credentials: dict = {}
    destination_connection_id: str | None = None
    other_mappings: list[dict] = []


class ValidateRequest(BaseModel):
    destination_credentials: dict = {}
    destination_connection_id: str | None = None


class ImportRequest(BaseModel):
    overwrite: bool = False


# ── Static routes (declared before /{name} to avoid path collisions) ────────────

@router.get("/")
def list_mappings(source_type: str | None = None):
    return mappings_service.list_mapping_configs(source_type=source_type)


@router.post("/auto-suggest")
def auto_suggest(body: AutoSuggestRequest):
    src_creds = _resolve_credentials(body.source_credentials, body.source_connection_id)
    dest_creds = _resolve_credentials(body.destination_credentials, body.destination_connection_id)
    draft = auto_suggest_mapping(
        source_type=body.source_type,
        source_table=body.source_table,
        salesforce_object=body.salesforce_object,
        source_credentials=src_creds,
        destination_credentials=dest_creds,
        other_mappings=body.other_mappings,
    )
    return draft


@router.post("/import-from-json")
def import_from_json(body: ImportRequest):
    return mappings_service.import_json_mappings(overwrite=body.overwrite)


# ── CRUD ─────────────────────────────────────────────────────────────────────

@router.get("/{name}")
def get_mapping(name: str):
    config = mappings_service.get_mapping_config(name)
    if config is None:
        raise HTTPException(status_code=404, detail=f"Mapping '{name}' not found")
    return {"name": name, "config": config}


@router.post("/", status_code=201)
def create_mapping(body: CreateMappingRequest):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Name is required")
    existing = mappings_service.get_mapping_config(body.name)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Mapping '{body.name}' already exists")
    return mappings_service.save_mapping_config(
        name=body.name.strip(),
        config=body.config,
        source_type=body.source_type,
        priority=body.priority,
    )


@router.put("/{name}")
def update_mapping(name: str, body: SaveMappingRequest):
    return mappings_service.save_mapping_config(
        name=name,
        config=body.config,
        source_type=body.source_type,
        priority=body.priority,
    )


@router.delete("/{name}")
def delete_mapping(name: str):
    try:
        deleted = mappings_service.delete_mapping_config(name)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Mapping '{name}' not found")
    return {"ok": True}


@router.post("/{name}/validate")
def validate_mapping(name: str, body: ValidateRequest):
    config = mappings_service.get_mapping_config(name)
    if config is None:
        raise HTTPException(status_code=404, detail=f"Mapping '{name}' not found")

    sf_object = config.get("salesforce_object", "")
    dest_creds = _resolve_credentials(body.destination_credentials, body.destination_connection_id)
    dest_fields = {f["name"] for f in get_destination_fields(sf_object, dest_creds)}

    errors: list[str] = []
    warnings: list[str] = []

    if not sf_object:
        errors.append("Mapping is missing a 'salesforce_object'.")
    if not config.get("fields"):
        errors.append("Mapping has no field definitions.")
    if config.get("operation") not in ("insert", "update", "upsert", None):
        errors.append(f"Invalid operation '{config.get('operation')}'.")

    for fm in config.get("fields", []):
        dest = fm.get("destination_field", "")
        if not dest:
            warnings.append("A field mapping has no destination_field.")
            continue
        # Only validate against the org when we successfully described it.
        if dest_fields and dest not in dest_fields:
            warnings.append(
                f"Destination field '{dest}' not found on {sf_object} "
                "(may be a custom field that needs creating)."
            )

    return {
        "name": name,
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }
