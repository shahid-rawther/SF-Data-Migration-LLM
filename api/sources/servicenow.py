import os
import json

import requests

from api.sources.base import MigratableObject, SourceProvider
from config.settings import MAPPING_PRIORITY, ATTACHMENT_MAPPING_PRIORITY, PATH_TO_MAPPING

# Category labels for UI grouping
_ATTACHMENT_NAMES = {
    "AttachmentIncidents",
    "AttachmentProblem",
    "AttachmentChangeRequest",
    "AttachmentServiceRequest",
}
_SETUP_NAMES = {
    "Group",
    "QueueCase",
    "QueueCaseServiceProcess",
    "QueueChangeRequest",
    "QueueIncident",
    "QueueProblem",
    "QueueSvcCatalogRequest",
    "AdminUser",
    "Account",
    "CommunityUser",
    "GroupMember",
    "UpdateUserForManager",
    "ParentLocation",
    "Department",
    "Address",
    "Company",
    "ParentLocationUpdateForVisitorAddressId",
    "UpdateAddressForStreet",
    "UpdateUserForEmployeeNumber",
    "UpdateUserForFedID",
    "UpdateUserForFedIDEmailUN",
    "UpdateUserForDept",
    "Service",
    "ServiceOffering",
    "Location",
    "ChildLocation",
}


def _category_for(name: str) -> str:
    if name in _ATTACHMENT_NAMES:
        return "Attachments"
    if name in _SETUP_NAMES or name.startswith("Update") or name.startswith("Queue"):
        return "Setup & Administration"
    return "ITSM Core Records"


def _load_mapping(name: str) -> dict | None:
    path = PATH_TO_MAPPING / f"{name}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# Build a flat priority map from MAPPING_PRIORITY
def _build_priority_map() -> dict[str, int]:
    priority_map: dict[str, int] = {}
    all_priorities = MAPPING_PRIORITY + ATTACHMENT_MAPPING_PRIORITY
    for idx, group in enumerate(all_priorities):
        for name in group:
            if name not in priority_map:
                priority_map[name] = idx
    return priority_map


class ServiceNowProvider(SourceProvider):
    @property
    def source_id(self) -> str:
        return "servicenow"

    @property
    def display_name(self) -> str:
        return "ServiceNow"

    @property
    def description(self) -> str:
        return "Migrate ITSM records from ServiceNow to Salesforce"

    def get_credentials_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["base_url", "username", "password"],
            "properties": {
                "base_url": {
                    "type": "string",
                    "title": "Instance URL",
                    "description": "e.g. https://myinstance.service-now.com",
                },
                "username": {
                    "type": "string",
                    "title": "Username",
                },
                "password": {
                    "type": "string",
                    "title": "Password",
                    "format": "password",
                },
            },
        }

    def validate_credentials(self, credentials: dict) -> bool:
        base_url = credentials.get("base_url", "").rstrip("/")
        username = credentials.get("username", "")
        password = credentials.get("password", "")
        if not (base_url and username and password):
            return False
        try:
            resp = requests.get(
                f"{base_url}/api/now/table/sys_db_object",
                auth=(username, password),
                params={"sysparm_limit": 1, "sysparm_no_count": "true"},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def get_objects(self) -> list[MigratableObject]:
        priority_map = _build_priority_map()
        objects: list[MigratableObject] = []
        seen: set[str] = set()

        all_names = [name for group in (MAPPING_PRIORITY + ATTACHMENT_MAPPING_PRIORITY) for name in group]

        for name in all_names:
            if name in seen:
                continue
            seen.add(name)
            mapping = _load_mapping(name)
            if not mapping:
                continue
            objects.append(
                MigratableObject(
                    name=name,
                    label=name,
                    category=_category_for(name),
                    salesforce_object=mapping.get("salesforce_object", ""),
                    servicenow_tables=mapping.get("servicenow_tables", []),
                    dependencies=[],
                    priority=priority_map.get(name, 9999),
                )
            )

        objects.sort(key=lambda o: o.priority)
        return objects

    def get_row_count(self, object_name: str, credentials: dict, filters: dict) -> int:
        mapping = _load_mapping(object_name)
        if not mapping:
            return 0
        tables = mapping.get("servicenow_tables", [])
        if not tables:
            return 0
        driving_table = tables[0]
        base_url = credentials.get("base_url", os.getenv("SERVICE_NOW_BASE_URL", "")).rstrip("/")
        username = credentials.get("username", os.getenv("SERVICE_NOW_USERNAME", ""))
        password = credentials.get("password", os.getenv("SERVICE_NOW_PASSWORD", ""))
        try:
            resp = requests.get(
                f"{base_url}/api/now/stats/{driving_table}",
                auth=(username, password),
                params={"sysparm_count": "true"},
                timeout=15,
            )
            resp.raise_for_status()
            count = resp.json().get("result", {}).get("stats", {}).get("count", "0")
            return int(count)
        except Exception:
            return -1
