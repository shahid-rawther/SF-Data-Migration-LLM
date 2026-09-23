"""Custom field creation router — creates custom fields in Salesforce."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import requests
import re

from services.internal import mappings_service

router = APIRouter(prefix="/custom-fields", tags=["custom-fields"])

_PRODUCT_ID_MAPPING_TABLE = "sc_cat_item_Product2_Product"


class CreateCustomFieldRequest(BaseModel):
    destination_credentials: dict
    salesforce_object: str
    field_label: str
    field_api_name: str  # Must end with __c
    field_type: str = "Text"  # Text, Number, TextArea, Checkbox, Date, DateTime, LongTextArea
    length: int = 255
    description: str = ""


class CreateCustomFieldResponse(BaseModel):
    success: bool
    message: str
    field_api_name: str


class SuggestFieldNameRequest(BaseModel):
    source_field: str
    salesforce_object: str


class SuggestFieldNameResponse(BaseModel):
    suggested_label: str
    suggested_api_name: str
    suggested_type: str
    suggested_length: int


# ── Field type mapping from common source types ───────────────────────────────

_SOURCE_TYPE_TO_SF: dict[str, tuple[str, int]] = {
    "string": ("Text", 255),
    "text": ("Text", 255),
    "integer": ("Number", 18),
    "number": ("Number", 18),
    "boolean": ("Checkbox", 0),
    "date": ("Date", 0),
    "datetime": ("DateTime", 0),
    "reference": ("Text", 80),
    "url": ("Url", 255),
    "email": ("Email", 0),
    "phone": ("Phone", 0),
}


def _sanitize_api_name(name: str) -> str:
    """Convert a source field name into a valid Salesforce custom field API name."""
    # Remove dots and replace with underscores
    name = name.replace(".", "_")
    # Remove any non-alphanumeric except underscores
    name = re.sub(r"[^A-Za-z0-9_]", "_", name)
    # Collapse multiple underscores
    name = re.sub(r"_+", "_", name)
    # Remove leading/trailing underscores
    name = name.strip("_")
    # Convert to Title_Case
    parts = name.split("_")
    name = "_".join(p.capitalize() for p in parts if p)
    # Append __c suffix if not present
    if not name.endswith("__c"):
        name = name + "__c"
    return name


def _suggest_label(source_field: str) -> str:
    """Convert a source field name into a human-readable Salesforce label."""
    # Remove table prefix (e.g., "problem.service_offering" -> "service_offering")
    if "." in source_field:
        source_field = source_field.split(".")[-1]
    # Replace underscores and hyphens with spaces, then title-case
    label = source_field.replace("_", " ").replace("-", " ")
    return label.title()


@router.post("/suggest", response_model=SuggestFieldNameResponse)
def suggest_custom_field_name(body: SuggestFieldNameRequest):
    """Suggest a custom field name and type based on the source field."""
    from api.compat_engine import _infer_field_type
    label = _suggest_label(body.source_field)
    api_name = _sanitize_api_name(body.source_field)
    inferred_type = _infer_field_type(api_name)
    length = 32768 if inferred_type == "LongTextArea" else 255
    return SuggestFieldNameResponse(
        suggested_label=label,
        suggested_api_name=api_name,
        suggested_type=inferred_type,
        suggested_length=length,
    )


def _get_sf_token(credentials: dict) -> tuple[str, str]:
    """Authenticate and return (access_token, instance_url)."""
    base_url = (credentials.get("base_url") or "").rstrip("/")
    client_id = credentials.get("client_id", "")
    client_secret = credentials.get("client_secret", "")

    if not (base_url and client_id and client_secret):
        raise HTTPException(status_code=400, detail="Incomplete Salesforce credentials")

    token_resp = requests.post(
        f"{base_url}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15,
    )
    if token_resp.status_code != 200:
        raise HTTPException(
            status_code=401,
            detail="Failed to authenticate with Salesforce. Check credentials.",
        )
    data = token_resp.json()
    return data["access_token"], (data.get("instance_url") or base_url).rstrip("/")


# Salesforce Metadata field type to XML type mapping
_SF_FIELD_TYPE_MAP = {
    "Text": "Text",
    "Number": "Number",
    "TextArea": "TextArea",
    "LongTextArea": "LongTextArea",
    "Checkbox": "Checkbox",
    "Date": "Date",
    "DateTime": "DateTime",
    "Url": "Url",
    "Email": "Email",
    "Phone": "Phone",
}


@router.post("/create", response_model=CreateCustomFieldResponse)
def create_custom_field(body: CreateCustomFieldRequest):
    """Create a custom field on a Salesforce object using the Tooling API."""
    # Validate field API name
    if not body.field_api_name.endswith("__c"):
        raise HTTPException(status_code=400, detail="Field API name must end with '__c'")

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*__c", body.field_api_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid field API name. Must start with a letter and contain only letters, numbers, and underscores.",
        )

    access_token, instance_url = _get_sf_token(body.destination_credentials)
    api_version = body.destination_credentials.get("api_version", "v60.0")

    # Use the Tooling API to create a CustomField
    tooling_url = f"{instance_url}/services/data/{api_version}/tooling/sobjects/CustomField"

    # Build the metadata payload
    field_metadata: dict = {
        "FullName": f"{body.salesforce_object}.{body.field_api_name}",
        "Metadata": {
            "label": body.field_label,
            "type": _SF_FIELD_TYPE_MAP.get(body.field_type, "Text"),
        },
    }

    # Add type-specific attributes
    sf_type = body.field_type
    if sf_type in ("Text", "Url", "Email", "Phone"):
        field_metadata["Metadata"]["length"] = body.length or 255
    elif sf_type == "Number":
        field_metadata["Metadata"]["precision"] = 18
        field_metadata["Metadata"]["scale"] = 2
    elif sf_type == "LongTextArea":
        field_metadata["Metadata"]["length"] = 32768
        field_metadata["Metadata"]["visibleLines"] = 5
    elif sf_type == "TextArea":
        pass  # No extra attrs needed

    if body.description:
        field_metadata["Metadata"]["description"] = body.description

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(tooling_url, json=field_metadata, headers=headers, timeout=30)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Failed to reach Salesforce: {e}")

    if resp.status_code in (200, 201):
        # The object's describe is now out of date, and a stale one would hide
        # the field that was just created for the rest of the cache lifetime.
        from services.internal import schema_cache

        schema_cache.invalidate(
            schema_cache.SCOPE_DESTINATION,
            "salesforce",
            body.destination_credentials,
            body.salesforce_object,
        )
        return CreateCustomFieldResponse(
            success=True,
            message=f"Custom field '{body.field_label}' ({body.field_api_name}) created successfully on {body.salesforce_object}.",
            field_api_name=body.field_api_name,
        )
    else:
        # Extract error details
        try:
            err_data = resp.json()
            if isinstance(err_data, list) and err_data:
                msg = err_data[0].get("message", resp.text[:500])
            elif isinstance(err_data, dict):
                msg = err_data.get("message") or err_data.get("error", resp.text[:500])
            else:
                msg = resp.text[:500]
        except Exception:
            msg = resp.text[:500]

        # Treat "already exists" as success — the field is present, which is the goal.
        if "there is already a field named" in msg.lower():
            return CreateCustomFieldResponse(
                success=True,
                message=f"Field '{body.field_label}' already exists on {body.salesforce_object} — no action needed.",
                field_api_name=body.field_api_name,
            )

        return CreateCustomFieldResponse(
            success=False,
            message=f"Failed to create field: {msg}",
            field_api_name=body.field_api_name,
        )


# ── Product ID mapping sync ────────────────────────────────────────────────────


class SyncProductMappingsRequest(BaseModel):
    source_credentials: dict   # ServiceNow: base_url, username, password
    destination_credentials: dict  # Salesforce: base_url, client_id, client_secret, api_version


class SyncProductMappingsResponse(BaseModel):
    matched: int
    unmatched_sn: list[str]   # ServiceNow catalog item names with no Salesforce match
    unmatched_sf: list[str]   # Salesforce product names with no ServiceNow match
    message: str


@router.post("/sync-product-mappings", response_model=SyncProductMappingsResponse)
def sync_product_mappings(body: SyncProductMappingsRequest):
    """Populate id_mappings for sc_cat_item → Product2 by matching on Name.

    This is called automatically at migration start when InsertCase,
    ServiceRequest, or UpdateServiceRequestFinal are selected.  It can also
    be triggered manually here.
    """
    sn = body.source_credentials
    sf = body.destination_credentials

    sn_base = (sn.get("base_url") or "").rstrip("/")
    sn_user = sn.get("username") or sn.get("user") or ""
    sn_pass = sn.get("password") or ""

    try:
        result = mappings_service.sync_product_id_mappings(sn_base, sn_user, sn_pass, sf)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Sync failed: {e}")

    return SyncProductMappingsResponse(
        matched=result["matched"],
        unmatched_sn=result["unmatched_sn"],
        unmatched_sf=result["unmatched_sf"],
        message=(
            f"Synced {result['matched']} product mapping(s) into id_mappings. "
            f"{len(result['unmatched_sn'])} ServiceNow catalog item(s) had no matching Salesforce product."
        ),
    )
