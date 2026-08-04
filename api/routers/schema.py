"""Schema discovery router — introspect source and destination schemas.

Powers the Mapping Editor: lists raw source tables, the fields of a source
table (or existing mapping object), and the fields of a Salesforce object.
All endpoints accept either an inline `credentials` dict or a `connection_id`
referencing a saved connection.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api import sources as source_registry
from api import connections_db
from api.compat_engine import get_destination_fields, get_destination_records
from services.internal import prerequisites_service, schema_cache

router = APIRouter(prefix="/schema", tags=["schema"])


def _resolve_credentials(credentials: dict | None, connection_id: str | None) -> dict:
    """Return credentials from an inline dict or a saved connection."""
    if connection_id:
        conn = connections_db.get_connection(connection_id)
        if not conn:
            raise HTTPException(status_code=404, detail="Connection not found")
        return conn.get("credentials", {})
    return credentials or {}


class SourceTablesRequest(BaseModel):
    source_type: str = "servicenow"
    credentials: dict = {}
    connection_id: str | None = None


class SourceFieldsRequest(BaseModel):
    source_type: str = "servicenow"
    credentials: dict = {}
    connection_id: str | None = None
    # Provide either an existing mapping object_name OR a raw source table name.
    object_name: str | None = None
    table: str | None = None
    # Bypass the cached describe and re-read the table from the source.
    refresh: bool = False


class PrerequisitesRequest(BaseModel):
    # The Salesforce object being migrated into.
    object_name: str
    credentials: dict = {}
    connection_id: str | None = None
    # Narrows the mappings consulted to one source pack; blank means all.
    source_type: str | None = None
    # Salesforce objects already covered by the run, so the answer can say
    # which prerequisites are handled and which are still outstanding.
    selected_objects: list[str] = []
    # The mapping being edited; the fields it populates override anything stored.
    draft_config: dict | None = None
    max_depth: int = 3
    max_nodes: int = 25


class SourceValuesRequest(BaseModel):
    source_type: str = "servicenow"
    credentials: dict = {}
    connection_id: str | None = None
    table: str
    field: str
    limit: int = 500


class DestinationObjectsRequest(BaseModel):
    destination_type: str = "salesforce"
    credentials: dict = {}
    connection_id: str | None = None


class DestinationFieldsRequest(BaseModel):
    object_name: str
    credentials: dict = {}
    connection_id: str | None = None


class DestinationRecordsRequest(BaseModel):
    object_name: str
    name_field: str = "Name"
    limit: int = 200
    credentials: dict = {}
    connection_id: str | None = None


@router.post("/source-tables")
def source_tables(body: SourceTablesRequest):
    provider = source_registry.get(body.source_type)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Source '{body.source_type}' not found")
    creds = _resolve_credentials(body.credentials, body.connection_id)
    try:
        tables = provider.get_source_tables(creds)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"source_type": body.source_type, "tables": tables}


@router.post("/source-fields")
def source_fields(body: SourceFieldsRequest):
    provider = source_registry.get(body.source_type)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Source '{body.source_type}' not found")
    if not (body.object_name or body.table):
        raise HTTPException(status_code=400, detail="Provide either 'object_name' or 'table'")
    creds = _resolve_credentials(body.credentials, body.connection_id)
    if body.table:
        fields = schema_cache.get_source_table_fields(
            body.source_type, body.table, creds, refresh=body.refresh
        )
    else:
        fields = provider.get_source_fields(body.object_name, creds)
    return {
        "source_type": body.source_type,
        "fields": [
            {
                "name": f.name,
                "label": f.label,
                "field_type": f.field_type,
                "required": f.required,
                "example": f.example,
                "reference_table": getattr(f, "reference_table", ""),
            }
            for f in fields
        ],
    }


@router.post("/prerequisites")
def prerequisites(body: PrerequisitesRequest):
    """Return the Salesforce objects to load before this one, in order."""
    if not body.object_name:
        raise HTTPException(status_code=400, detail="Provide 'object_name'")
    creds = _resolve_credentials(body.credentials, body.connection_id)
    return prerequisites_service.build_prerequisites(
        body.object_name,
        creds,
        source_type=body.source_type,
        selected_objects=body.selected_objects,
        draft_config=body.draft_config,
        max_depth=body.max_depth,
        max_nodes=body.max_nodes,
    )


@router.post("/source-values")
def source_values(body: SourceValuesRequest):
    """List a source field's known values (e.g. ServiceNow choices) for the value-map editor."""
    provider = source_registry.get(body.source_type)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Source '{body.source_type}' not found")
    if not (body.table and body.field):
        raise HTTPException(status_code=400, detail="Provide both 'table' and 'field'")
    creds = _resolve_credentials(body.credentials, body.connection_id)
    values = provider.get_field_values(body.table, body.field, creds, limit=body.limit)
    return {"source_type": body.source_type, "values": values}


@router.post("/destination-objects")
def destination_objects(body: DestinationObjectsRequest):
    dest = source_registry.get_destination(body.destination_type)
    if not dest:
        raise HTTPException(status_code=404, detail=f"Destination '{body.destination_type}' not found")
    creds = _resolve_credentials(body.credentials, body.connection_id)
    objects = dest.list_objects(creds)
    return {"destination_type": body.destination_type, "objects": objects}


@router.post("/destination-fields")
def destination_fields(body: DestinationFieldsRequest):
    creds = _resolve_credentials(body.credentials, body.connection_id)
    fields = get_destination_fields(body.object_name, creds)
    return {"object_name": body.object_name, "fields": fields}


@router.post("/destination-records")
def destination_records(body: DestinationRecordsRequest):
    """List records of a destination object (Id + name) for the reference picker."""
    creds = _resolve_credentials(body.credentials, body.connection_id)
    records = get_destination_records(
        body.object_name, creds, name_field=body.name_field, limit=body.limit
    )
    return {"object_name": body.object_name, "records": records}
