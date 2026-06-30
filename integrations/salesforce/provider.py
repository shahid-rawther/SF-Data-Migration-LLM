"""Salesforce destination plugin.

Wraps the Salesforce HTTP client and the loading orchestration so the generic
ETL pipeline can drive Salesforce through the ``DestinationPlugin`` contract
without importing Salesforce-specific modules directly.
"""
from __future__ import annotations

import requests

from integrations.base import DestinationPlugin


class SalesforcePlugin(DestinationPlugin):
    @property
    def destination_id(self) -> str:
        return "salesforce"

    @property
    def display_name(self) -> str:
        return "Salesforce"

    def get_routers(self) -> list:
        from integrations.salesforce.routes import routers

        return routers

    def configure_client(self, credentials: dict) -> None:
        from integrations.salesforce.http_client import salesforce_client

        salesforce_client.configure(
            base_url=credentials.get("base_url"),
            client_id=credentials.get("client_id"),
            client_secret=credentials.get("client_secret"),
            api_version=credentials.get("api_version"),
        )

    def validate_credentials(self, credentials: dict) -> bool:
        base_url = (credentials.get("base_url") or "").rstrip("/")
        client_id = credentials.get("client_id") or ""
        client_secret = credentials.get("client_secret") or ""
        if not (base_url and client_id and client_secret):
            return False
        try:
            resp = requests.post(
                f"{base_url}/services/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=12,
            )
            return resp.status_code == 200 and bool(resp.json().get("access_token"))
        except Exception:
            return False

    def list_objects(self, credentials: dict) -> list[dict]:
        base_url = (credentials.get("base_url") or "").rstrip("/")
        client_id = credentials.get("client_id") or ""
        client_secret = credentials.get("client_secret") or ""
        api_version = credentials.get("api_version", "v60.0")
        if not (base_url and client_id and client_secret):
            return []
        try:
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
            resp = requests.get(
                f"{instance_url}/services/data/{api_version}/sobjects",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=20,
            )
            resp.raise_for_status()
            result = []
            for item in resp.json().get("sobjects", []):
                if item.get("createable"):
                    result.append({
                        "name": item.get("name", ""),
                        "label": item.get("label", item.get("name", "")),
                        "createable": True,
                    })
            return sorted(result, key=lambda x: x["name"])
        except Exception:
            return []

    def describe_object(self, object_name: str):
        from integrations.salesforce.http_client import salesforce_client

        try:
            return salesforce_client.describe_object(object_name)
        except Exception:
            return None

    def start_loading(self, job_id: int, mappings_file_names: list[str]) -> dict:
        from controllers import loading_controller

        return loading_controller.start_loading(job_id, mappings_file_names)

    def start_attachment_loading(self, job_id: int, mappings_file_names: list[str]) -> None:
        from controllers import loading_controller

        loading_controller.start_attachment_loading(job_id, mappings_file_names)
