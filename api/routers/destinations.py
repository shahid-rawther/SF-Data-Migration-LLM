"""Destination router — validate destination credentials such as Salesforce."""

import json
from pathlib import Path

import requests
from fastapi import APIRouter
from pydantic import BaseModel

from config.settings import PATH_TO_MAPPING

router = APIRouter(prefix="/destinations", tags=["destinations"])


class CredentialValidationRequest(BaseModel):
    credentials: dict


class SalesforceDiagnosticsRequest(BaseModel):
    credentials: dict
    required_objects: list[str] = []
    selected_objects: list[str] = []


def _mapping_required_objects(selected_objects: list[str]) -> list[str]:
    required: set[str] = set()
    for mapping_name in selected_objects:
        path: Path = PATH_TO_MAPPING / f"{mapping_name}.json"
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                mapping = json.load(f)
        except Exception:
            continue

        primary = mapping.get("salesforce_object")
        if primary:
            required.add(primary)

        for item in mapping.get("salesforce_objects", []):
            object_name = (item or {}).get("object_name")
            if object_name:
                required.add(object_name)

    return sorted(required)


def _extract_auth_error_message(response: requests.Response) -> str:
    message = "Salesforce authentication failed."
    try:
        data = response.json()
        error_code = (data.get("error") or "").lower()
        detail = data.get("error_description") or data.get("error")

        if error_code == "invalid_grant" and detail:
            lowered = detail.lower()
            if "no client credentials user enabled" in lowered:
                detail = (
                    "Connected App is not configured for Client Credentials run-as user. "
                    "In Salesforce Connected App policies, enable Client Credentials flow and set a Run As user."
                )
            elif "request not supported on this domain" in lowered:
                detail = (
                    "Client Credentials grant is not supported on this domain. "
                    "Use your Salesforce My Domain token endpoint for this connected app."
                )

        if detail:
            message = f"Salesforce authentication failed: {detail}"
    except ValueError:
        pass
    return message


@router.post("/salesforce/validate-credentials")
def validate_salesforce_credentials(body: CredentialValidationRequest):
    creds = body.credentials or {}
    base_url = (creds.get("base_url") or "").rstrip("/")
    client_id = creds.get("client_id") or ""
    client_secret = creds.get("client_secret") or ""

    if not (base_url and client_id and client_secret):
        return {
            "valid": False,
            "message": "Please provide Salesforce base URL, client ID, and client secret.",
        }

    token_url = f"{base_url}/services/oauth2/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }

    try:
        response = requests.post(token_url, data=payload, timeout=12)
    except requests.RequestException as exc:
        return {
            "valid": False,
            "message": f"Could not reach Salesforce: {exc}",
        }

    if response.status_code != 200:
        return {"valid": False, "message": _extract_auth_error_message(response)}

    try:
        data = response.json()
    except ValueError:
        return {
            "valid": False,
            "message": "Salesforce returned a non-JSON response during authentication.",
        }

    if not data.get("access_token"):
        return {
            "valid": False,
            "message": "Salesforce did not return an access token.",
        }

    return {"valid": True, "message": "Connection successful."}


@router.post("/salesforce/diagnostics")
def salesforce_diagnostics(body: SalesforceDiagnosticsRequest):
    creds = body.credentials or {}
    base_url = (creds.get("base_url") or "").rstrip("/")
    client_id = creds.get("client_id") or ""
    client_secret = creds.get("client_secret") or ""
    api_version = creds.get("api_version") or "v66.0"

    required_objects = body.required_objects or _mapping_required_objects(body.selected_objects)

    if not (base_url and client_id and client_secret):
        return {
            "valid": False,
            "message": "Please provide Salesforce base URL, client ID, and client secret.",
            "run_as_user_id": None,
            "required_objects": required_objects,
            "visibility": {},
            "missing_objects": required_objects,
        }

    token_url = f"{base_url}/services/oauth2/token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }

    try:
        token_resp = requests.post(token_url, data=payload, timeout=12)
    except requests.RequestException as exc:
        return {
            "valid": False,
            "message": f"Could not reach Salesforce: {exc}",
            "run_as_user_id": None,
            "required_objects": required_objects,
            "visibility": {},
            "missing_objects": required_objects,
        }

    if token_resp.status_code != 200:
        return {
            "valid": False,
            "message": _extract_auth_error_message(token_resp),
            "run_as_user_id": None,
            "required_objects": required_objects,
            "visibility": {},
            "missing_objects": required_objects,
        }

    try:
        token_data = token_resp.json()
    except ValueError:
        return {
            "valid": False,
            "message": "Salesforce returned a non-JSON response during authentication.",
            "run_as_user_id": None,
            "required_objects": required_objects,
            "visibility": {},
            "missing_objects": required_objects,
        }

    access_token = token_data.get("access_token")
    if not access_token:
        return {
            "valid": False,
            "message": "Salesforce did not return an access token.",
            "run_as_user_id": None,
            "required_objects": required_objects,
            "visibility": {},
            "missing_objects": required_objects,
        }

    run_as_user_id = None
    id_url = token_data.get("id")
    if isinstance(id_url, str):
        parts = [p for p in id_url.split("/") if p]
        if parts:
            run_as_user_id = parts[-1]

    instance_url = (token_data.get("instance_url") or base_url).rstrip("/")
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        sobjects_resp = requests.get(
            f"{instance_url}/services/data/{api_version}/sobjects",
            headers=headers,
            timeout=20,
        )
        sobjects_resp.raise_for_status()
        object_names = {
            item.get("name")
            for item in sobjects_resp.json().get("sobjects", [])
            if item.get("name")
        }
    except Exception as exc:
        return {
            "valid": False,
            "message": f"Authenticated, but failed to describe Salesforce objects: {exc}",
            "run_as_user_id": run_as_user_id,
            "required_objects": required_objects,
            "visibility": {},
            "missing_objects": required_objects,
        }

    visibility = {name: (name in object_names) for name in required_objects}
    missing_objects = [name for name, is_visible in visibility.items() if not is_visible]

    return {
        "valid": True,
        "message": "Diagnostics completed.",
        "run_as_user_id": run_as_user_id,
        "required_objects": required_objects,
        "visibility": visibility,
        "missing_objects": missing_objects,
    }
