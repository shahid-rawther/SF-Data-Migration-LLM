"""Zendesk source provider.

Connects to the Zendesk REST API v2 and exposes tickets, users, and
organisations as migratable objects targeting Salesforce.
"""
from __future__ import annotations

from typing import Iterator

import requests

from api.sources.base import MigratableObject, SourceField, SourceProvider

_ZENDESK_OBJECTS: list[dict] = [
    {
        "name": "zendesk_tickets",
        "label": "Tickets",
        "category": "ITSM Core Records",
        "salesforce_object": "Case",
        "source_entity": "tickets",
        "priority": 10,
    },
    {
        "name": "zendesk_users",
        "label": "Users",
        "category": "Setup & Administration",
        "salesforce_object": "Contact",
        "source_entity": "users",
        "priority": 1,
    },
    {
        "name": "zendesk_organizations",
        "label": "Organizations",
        "category": "Setup & Administration",
        "salesforce_object": "Account",
        "source_entity": "organizations",
        "priority": 2,
    },
    {
        "name": "zendesk_ticket_comments",
        "label": "Ticket Comments",
        "category": "ITSM Core Records",
        "salesforce_object": "CaseComment",
        "source_entity": "ticket_comments",
        "priority": 50,
    },
]

_TICKET_FIELDS: list[tuple[str, str, str]] = [
    ("subject", "Subject", "string"),
    ("description", "Description", "string"),
    ("status", "Status", "string"),
    ("priority", "Priority", "string"),
    ("type", "Type", "string"),
    ("created_at", "Created Date", "datetime"),
    ("updated_at", "Updated Date", "datetime"),
    ("solved_at", "Solved Date", "datetime"),
    ("assignee_id", "Assignee", "reference"),
    ("requester_id", "Requester", "reference"),
    ("organization_id", "Organization", "reference"),
    ("tags", "Tags", "array"),
]


class ZendeskProvider(SourceProvider):
    @property
    def source_id(self) -> str:
        return "zendesk"

    @property
    def display_name(self) -> str:
        return "Zendesk"

    @property
    def description(self) -> str:
        return "Migrate ITSM records from Zendesk to Salesforce"

    def get_credentials_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["subdomain", "email", "api_token"],
            "properties": {
                "subdomain": {
                    "type": "string",
                    "title": "Subdomain",
                    "description": "Your Zendesk subdomain, e.g. 'mycompany' for mycompany.zendesk.com",
                },
                "email": {
                    "type": "string",
                    "title": "Agent Email",
                },
                "api_token": {
                    "type": "string",
                    "title": "API Token",
                    "format": "password",
                    "description": "Generate at Admin → Apps & Integrations → Zendesk API",
                },
            },
        }

    def _base(self, credentials: dict) -> str:
        subdomain = credentials.get("subdomain", "").strip().rstrip("/")
        if subdomain.startswith("https://"):
            return subdomain.rstrip("/")
        return f"https://{subdomain}.zendesk.com"

    def _auth(self, credentials: dict):
        email = credentials.get("email", "")
        token = credentials.get("api_token", "")
        return (f"{email}/token", token)

    def validate_credentials(self, credentials: dict) -> bool:
        if not (credentials.get("subdomain") and credentials.get("email") and credentials.get("api_token")):
            return False
        try:
            resp = requests.get(
                f"{self._base(credentials)}/api/v2/users/me.json",
                auth=self._auth(credentials),
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
            for obj in _ZENDESK_OBJECTS
        ]

    def get_row_count(self, object_name: str, credentials: dict, filters: dict) -> int:
        meta = next((o for o in _ZENDESK_OBJECTS if o["name"] == object_name), None)
        if not meta:
            return 0
        base = self._base(credentials)
        entity = meta["source_entity"]
        try:
            if entity == "tickets":
                resp = requests.get(
                    f"{base}/api/v2/tickets/count.json",
                    auth=self._auth(credentials),
                    timeout=15,
                )
                resp.raise_for_status()
                return resp.json().get("count", {}).get("value", 0)
            if entity == "users":
                resp = requests.get(
                    f"{base}/api/v2/users/count.json",
                    auth=self._auth(credentials),
                    timeout=15,
                )
                resp.raise_for_status()
                return resp.json().get("count", {}).get("value", 0)
            if entity == "organizations":
                resp = requests.get(
                    f"{base}/api/v2/organizations/count.json",
                    auth=self._auth(credentials),
                    timeout=15,
                )
                resp.raise_for_status()
                return resp.json().get("count", {}).get("value", 0)
            return -1
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
        meta = next((o for o in _ZENDESK_OBJECTS if o["name"] == object_name), None)
        if not meta:
            return
        base = self._base(credentials)
        entity = meta["source_entity"]
        if entity not in ("tickets", "users", "organizations"):
            return
        start_date = options.get("start_date", "")
        url = f"{base}/api/v2/{entity}.json"
        params: dict = {"per_page": 100, "page": 1}
        if start_date and entity == "tickets":
            params["query"] = f"type:ticket created>={start_date}"
            url = f"{base}/api/v2/search.json"
        while url:
            try:
                resp = requests.get(url, auth=self._auth(credentials), params=params, timeout=60)
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                break
            records = data.get(entity, data.get("results", []))
            if not records:
                break
            yield [_flatten_zendesk_record(r, entity) for r in records]
            url = data.get("next_page")
            params = {}  # next_page already contains pagination params


def _flatten_zendesk_record(record: dict, entity: str) -> dict:
    if entity == "tickets":
        return {
            "id": record.get("id"),
            "subject": record.get("subject", ""),
            "description": record.get("description", ""),
            "status": record.get("status", ""),
            "priority": record.get("priority", ""),
            "type": record.get("type", ""),
            "created_at": record.get("created_at", ""),
            "updated_at": record.get("updated_at", ""),
            "assignee_id": record.get("assignee_id", ""),
            "requester_id": record.get("requester_id", ""),
            "organization_id": record.get("organization_id", ""),
            "tags": ",".join(record.get("tags", [])),
        }
    return record
