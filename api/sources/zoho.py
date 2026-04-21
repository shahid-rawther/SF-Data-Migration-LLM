"""Zoho Desk source provider.

Connects to the Zoho Desk REST API and exposes tickets, contacts,
accounts and departments as migratable objects targeting Salesforce.
"""
from __future__ import annotations

from typing import Iterator

import requests

from api.sources.base import MigratableObject, SourceField, SourceProvider

_ZOHO_OBJECTS: list[dict] = [
    {
        "name": "zoho_tickets",
        "label": "Tickets",
        "category": "ITSM Core Records",
        "salesforce_object": "Case",
        "source_entity": "tickets",
        "priority": 10,
    },
    {
        "name": "zoho_contacts",
        "label": "Contacts",
        "category": "Setup & Administration",
        "salesforce_object": "Contact",
        "source_entity": "contacts",
        "priority": 1,
    },
    {
        "name": "zoho_accounts",
        "label": "Accounts",
        "category": "Setup & Administration",
        "salesforce_object": "Account",
        "source_entity": "accounts",
        "priority": 2,
    },
    {
        "name": "zoho_agents",
        "label": "Agents",
        "category": "Setup & Administration",
        "salesforce_object": "User",
        "source_entity": "agents",
        "priority": 3,
    },
    {
        "name": "zoho_departments",
        "label": "Departments",
        "category": "Setup & Administration",
        "salesforce_object": "Group",
        "source_entity": "departments",
        "priority": 4,
    },
]

_TICKET_FIELDS: list[tuple[str, str, str]] = [
    ("subject", "Subject", "string"),
    ("description", "Description", "string"),
    ("status", "Status", "string"),
    ("priority", "Priority", "string"),
    ("channel", "Channel", "string"),
    ("category", "Category", "string"),
    ("createdTime", "Created Date", "datetime"),
    ("modifiedTime", "Modified Date", "datetime"),
    ("closedTime", "Closed Date", "datetime"),
    ("assigneeId", "Assignee", "reference"),
    ("contactId", "Contact", "reference"),
    ("accountId", "Account", "reference"),
    ("departmentId", "Department", "reference"),
]


class ZohoProvider(SourceProvider):
    @property
    def source_id(self) -> str:
        return "zoho"

    @property
    def display_name(self) -> str:
        return "Zoho Desk"

    @property
    def description(self) -> str:
        return "Migrate ITSM records from Zoho Desk to Salesforce"

    def get_credentials_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["client_id", "client_secret", "refresh_token"],
            "properties": {
                "region": {
                    "type": "string",
                    "title": "Data Center Region",
                    "description": "Zoho DC: com, eu, in, com.au, jp (default: com)",
                },
                "client_id": {
                    "type": "string",
                    "title": "OAuth Client ID",
                },
                "client_secret": {
                    "type": "string",
                    "title": "OAuth Client Secret",
                    "format": "password",
                },
                "refresh_token": {
                    "type": "string",
                    "title": "OAuth Refresh Token",
                    "format": "password",
                    "description": "Generate via Zoho API Console with Desk.tickets.READ scope",
                },
                "org_id": {
                    "type": "string",
                    "title": "Organization ID (optional)",
                    "description": "Scope to a specific Zoho Desk organization",
                },
            },
        }

    def _region(self, credentials: dict) -> str:
        return credentials.get("region", "com").strip().lower()

    def _token_url(self, credentials: dict) -> str:
        return f"https://accounts.zoho.{self._region(credentials)}/oauth/v2/token"

    def _api_base(self, credentials: dict) -> str:
        return f"https://desk.zoho.{self._region(credentials)}/api/v1"

    def _get_access_token(self, credentials: dict) -> str | None:
        try:
            resp = requests.post(
                self._token_url(credentials),
                data={
                    "grant_type": "refresh_token",
                    "client_id": credentials.get("client_id", ""),
                    "client_secret": credentials.get("client_secret", ""),
                    "refresh_token": credentials.get("refresh_token", ""),
                },
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json().get("access_token")
        except Exception:
            return None

    def _headers(self, access_token: str, credentials: dict) -> dict:
        h = {"Authorization": f"Zoho-oauthtoken {access_token}"}
        org_id = credentials.get("org_id", "")
        if org_id:
            h["orgId"] = org_id
        return h

    def validate_credentials(self, credentials: dict) -> bool:
        if not (credentials.get("client_id") and credentials.get("client_secret") and credentials.get("refresh_token")):
            return False
        token = self._get_access_token(credentials)
        if not token:
            return False
        try:
            resp = requests.get(
                f"{self._api_base(credentials)}/myProfile",
                headers=self._headers(token, credentials),
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def get_objects(self) -> list[MigratableObject]:
        return [
            MigratableObject(
                name=obj["name"],
                label=obj["label"],
                category=obj["category"],
                salesforce_object=obj["salesforce_object"],
                servicenow_tables=[obj["source_entity"]],
                priority=obj["priority"],
            )
            for obj in _ZOHO_OBJECTS
        ]

    def get_row_count(self, object_name: str, credentials: dict, filters: dict) -> int:
        meta = next((o for o in _ZOHO_OBJECTS if o["name"] == object_name), None)
        if not meta:
            return 0
        token = self._get_access_token(credentials)
        if not token:
            return -1
        entity = meta["source_entity"]
        base = self._api_base(credentials)
        try:
            resp = requests.get(
                f"{base}/{entity}/count",
                headers=self._headers(token, credentials),
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json().get("count", 0)
            # Fallback — fetch 1 record and read header
            resp2 = requests.get(
                f"{base}/{entity}",
                headers=self._headers(token, credentials),
                params={"limit": 1},
                timeout=15,
            )
            resp2.raise_for_status()
            return int(resp2.headers.get("X-Zoho-Count", resp2.headers.get("x-zoho-count", -1)))
        except Exception:
            return -1

    def get_source_fields(self, object_name: str, credentials: dict) -> list[SourceField]:
        return [
            SourceField(name=name, label=label, field_type=ftype)
            for name, label, ftype in _TICKET_FIELDS
        ]

    def extract_records(
        self,
        object_name: str,
        credentials: dict,
        options: dict,
    ) -> Iterator[list[dict]]:
        meta = next((o for o in _ZOHO_OBJECTS if o["name"] == object_name), None)
        if not meta:
            return
        token = self._get_access_token(credentials)
        if not token:
            return
        entity = meta["source_entity"]
        base = self._api_base(credentials)
        limit = 100
        from_index = 1
        while True:
            params: dict = {"limit": limit, "from": from_index}
            start_date = options.get("start_date", "")
            if start_date and entity == "tickets":
                params["createdTimeRange"] = f"{start_date}T00:00:00.000Z,"
            try:
                resp = requests.get(
                    f"{base}/{entity}",
                    headers=self._headers(token, credentials),
                    params=params,
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                records = data.get("data", [])
            except Exception:
                break
            if not records:
                break
            yield [_normalise(r, entity) for r in records]
            if len(records) < limit:
                break
            from_index += limit


def _normalise(record: dict, entity: str) -> dict:
    if entity == "tickets":
        return {
            "id": record.get("id"),
            "subject": record.get("subject", ""),
            "description": record.get("description", ""),
            "status": record.get("status", ""),
            "priority": record.get("priority", ""),
            "channel": record.get("channel", ""),
            "category": record.get("category", ""),
            "createdTime": record.get("createdTime", ""),
            "modifiedTime": record.get("modifiedTime", ""),
            "closedTime": record.get("closedTime", ""),
            "assigneeId": record.get("assigneeId", ""),
            "contactId": record.get("contactId", ""),
            "accountId": record.get("accountId", ""),
            "departmentId": record.get("departmentId", ""),
        }
    return record
