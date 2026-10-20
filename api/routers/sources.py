"""Sources router — lists available source providers and their objects."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api import sources as source_registry

router = APIRouter(prefix="/sources", tags=["sources"])


class CredentialValidationRequest(BaseModel):
    credentials: dict


class RowCountRequest(BaseModel):
    object_name: str
    credentials: dict = {}
    filters: dict = {}


@router.get("/")
def list_sources():
    return [
        {
            "id": p.source_id,
            "name": p.display_name,
            "description": p.description,
            "credentials_schema": p.get_credentials_schema(),
        }
        for p in source_registry.get_all()
    ]


@router.get("/{source_id}")
def get_source(source_id: str):
    provider = source_registry.get(source_id)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found")
    return {
        "id": provider.source_id,
        "name": provider.display_name,
        "description": provider.description,
        "credentials_schema": provider.get_credentials_schema(),
    }


@router.get("/{source_id}/objects")
def list_objects(source_id: str):
    provider = source_registry.get(source_id)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found")
    objs = provider.get_objects()
    return [
        {
            "name": o.name,
            "label": o.label,
            "category": o.category,
            "destination_object": o.salesforce_object,
            "source_tables": o.servicenow_tables,
            "salesforce_object": o.salesforce_object,
            "servicenow_tables": o.servicenow_tables,
            "dependencies": o.dependencies,
            "priority": o.priority,
        }
        for o in objs
    ]


@router.get("/{source_id}/objects/{object_name}")
def get_object(source_id: str, object_name: str):
    provider = source_registry.get(source_id)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found")
    objs = {o.name: o for o in provider.get_objects()}
    obj = objs.get(object_name)
    if not obj:
        raise HTTPException(status_code=404, detail=f"Object '{object_name}' not found")
    return {
        "name": obj.name,
        "label": obj.label,
        "category": obj.category,
        "destination_object": obj.salesforce_object,
        "source_tables": obj.servicenow_tables,
        "salesforce_object": obj.salesforce_object,
        "servicenow_tables": obj.servicenow_tables,
        "dependencies": obj.dependencies,
        "priority": obj.priority,
    }


@router.post("/{source_id}/validate-credentials")
def validate_credentials(source_id: str, body: CredentialValidationRequest):
    provider = source_registry.get(source_id)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found")
    check = getattr(provider, "credential_check", None)
    if callable(check):
        result = check(body.credentials)
        return {"valid": bool(result.get("valid")), "message": result.get("message") or ""}
    return {"valid": bool(provider.validate_credentials(body.credentials)), "message": ""}


@router.post("/{source_id}/row-count")
def get_row_count(source_id: str, body: RowCountRequest):
    provider = source_registry.get(source_id)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found")
    count = provider.get_row_count(body.object_name, body.credentials, body.filters)
    return {"object_name": body.object_name, "row_count": count}
