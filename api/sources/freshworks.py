"""Freshworks Freshservice source provider.

Connects to the Freshservice REST API v2 and exposes tickets, problems,
changes and assets as migratable objects targeting Salesforce.
"""
from __future__ import annotations

from typing import Iterator

import requests

from api.sources.base import MigratableObject, SourceField, SourceProvider

_FW_OBJECTS: list[dict] = [
    {
        "name": "fw_tickets",
        "label": "Tickets (Incidents & Requests)",
        "category": "ITSM Core Records",
        "salesforce_object": "Case",
        "source_entity": "tickets",
        "priority": 10,
    },
    {
        "name": "fw_problems",
        "label": "Problems",
        "category": "ITSM Core Records",
        "salesforce_object": "Case",
        "source_entity": "problems",
        "priority": 30,
    },
    {
        "name": "fw_changes",
        "label": "Changes",
        "category": "ITSM Core Records",
        "salesforce_object": "Case",
        "source_entity": "changes",
        "priority": 40,
    },
    {
        "name": "fw_requesters",
        "label": "Requesters (End-users)",
        "category": "Setup & Administration",
        "salesforce_object": "Contact",
        "source_entity": "requesters",
        "priority": 1,
    },
    {
        "name": "fw_agents",
        "label": "Agents",
        "category": "Setup & Administration",
        "salesforce_object": "User",
        "source_entity": "agents",
        "priority": 2,
    },
    {
        "name": "fw_groups",
        "label": "Groups",
        "category": "Setup & Administration",
        "salesforce_object": "Group",
        "source_entity": "groups",
        "priority": 3,
    },
]

_TICKET_FIELDS: list[tuple[str, str, str]] = [
    ("subject", "Subject", "string"),
    ("description", "Description", "string"),
    ("status", "Status", "integer"),
    ("priority", "Priority", "integer"),
    ("type", "Type", "string"),
    ("category", "Category", "string"),
    ("created_at", "Created Date", "datetime"),
    ("updated_at", "Updated Date", "datetime"),
    ("resolved_at", "Resolved Date", "datetime"),
    ("responder_id", "Assigned Agent", "reference"),
    ("group_id", "Assigned Group", "reference"),
    ("requester_id", "Requester", "reference"),
    ("tags", "Tags", "array"),
]

# Freshservice ticket status → Salesforce Case Status
STATUS_MAP = {1: "New", 2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}
PRIORITY_MAP = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}


class FreshworksProvider(SourceProvider):
    @property
    def source_id(self) -> str:
        return "freshworks"

    @property
    def display_name(self) -> str:
        return "Freshworks Freshservice"

    @property
    def description(self) -> str:
        return "Migrate ITSM records from Freshworks Freshservice to Salesforce"

    def get_credentials_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["domain", "api_key"],
            "properties": {
                "domain": {
                    "type": "string",
                    "title": "Freshservice Domain",
                    "description": "e.g. mycompany.freshservice.com",
                },
                "api_key": {
                    "type": "string",
                    "title": "API Key",
                    "format": "password",
                    "description": "Found under Admin → Profile Settings → API Key",
                },
            },
        }

    def _base(self, credentials: dict) -> str:
        domain = credentials.get("domain", "").strip().rstrip("/")
        if domain.startswith("https://"):
            return domain
        return f"https://{domain}"

    def _auth(self, credentials: dict):
        return (credentials.get("api_key", ""), "X")

    def validate_credentials(self, credentials: dict) -> bool:
        if not (credentials.get("domain") and credentials.get("api_key")):
            return False
        try:
            resp = requests.get(
                f"{self._base(credentials)}/api/v2/agents/me",
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
            for obj in _FW_OBJECTS
        ]

    def get_row_count(self, object_name: str, credentials: dict, filters: dict) -> int:
        meta = next((o for o in _FW_OBJECTS if o["name"] == object_name), None)
        if not meta:
            return 0
        base = self._base(credentials)
        entity = meta["source_entity"]
        try:
            resp = requests.get(
                f"{base}/api/v2/{entity}",
                auth=self._auth(credentials),
                params={"per_page": 1, "page": 1},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            resp.raise_for_status()
            # Freshservice returns total count in the Link header or X-Total-Count
            total = resp.headers.get("X-Total-Count") or resp.headers.get("x-total-count")
            if total:
                return int(total)
            # Fall back: count from results
            return len(resp.json().get(entity, []))
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
        meta = next((o for o in _FW_OBJECTS if o["name"] == object_name), None)
        if not meta:
            return
        base = self._base(credentials)
        entity = meta["source_entity"]
        page = 1
        per_page = 100
        updated_since = options.get("start_date", "")
        while True:
            params: dict = {"per_page": per_page, "page": page}
            if updated_since and entity == "tickets":
                params["updated_since"] = updated_since
            try:
                resp = requests.get(
                    f"{base}/api/v2/{entity}",
                    auth=self._auth(credentials),
                    params=params,
                    headers={"Content-Type": "application/json"},
                    timeout=60,
                )
                resp.raise_for_status()
                records = resp.json().get(entity, [])
            except Exception:
                break
            if not records:
                break
            yield [_normalise_ticket(r, entity) for r in records]
            if len(records) < per_page:
                break
            page += 1


def _normalise_ticket(record: dict, entity: str) -> dict:
    if entity == "tickets":
        status_val = record.get("status", 0)
        priority_val = record.get("priority", 0)
        return {
            "id": record.get("id"),
            "subject": record.get("subject", ""),
            "description": record.get("description_text", record.get("description", "")),
            "status": STATUS_MAP.get(status_val, str(status_val)),
            "priority": PRIORITY_MAP.get(priority_val, str(priority_val)),
            "type": record.get("type", ""),
            "category": record.get("category", ""),
            "created_at": record.get("created_at", ""),
            "updated_at": record.get("updated_at", ""),
            "resolved_at": record.get("resolved_at", ""),
            "responder_id": record.get("responder_id", ""),
            "group_id": record.get("group_id", ""),
            "requester_id": record.get("requester_id", ""),
            "tags": ",".join(record.get("tags", [])),
        }
    return record
