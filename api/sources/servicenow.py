import os
import json
from typing import Iterator

import requests

from api.sources.base import MigratableObject, SourceField, SourceProvider
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

    def get_source_fields(self, object_name: str, credentials: dict) -> list[SourceField]:
        """Return the list of fields for the driving ServiceNow table via the Schema API."""
        mapping = _load_mapping(object_name)
        if not mapping:
            return []
        tables = mapping.get("servicenow_tables", [])
        if not tables:
            return []
        driving_table = tables[0]
        base_url = credentials.get("base_url", os.getenv("SERVICE_NOW_BASE_URL", "")).rstrip("/")
        username = credentials.get("username", os.getenv("SERVICE_NOW_USERNAME", ""))
        password = credentials.get("password", os.getenv("SERVICE_NOW_PASSWORD", ""))
        try:
            resp = requests.get(
                f"{base_url}/api/now/table/sys_dictionary",
                auth=(username, password),
                params={
                    "sysparm_query": f"name={driving_table}^active=true",
                    "sysparm_fields": "element,column_label,internal_type,mandatory",
                    "sysparm_limit": 500,
                },
                timeout=15,
            )
            resp.raise_for_status()
            rows = resp.json().get("result", [])
            fields: list[SourceField] = []
            for row in rows:
                element = row.get("element", "")
                if not element:
                    continue
                field_type = (row.get("internal_type") or {}).get("value", "string")
                required = str((row.get("mandatory") or {}).get("value", "false")).lower() == "true"
                label = (row.get("column_label") or {}).get("display_value", element)
                fields.append(SourceField(
                    name=f"{driving_table}.{element}",
                    label=label,
                    field_type=field_type,
                    required=required,
                ))
            return fields
        except Exception:
            # Fall back to fields declared in the mapping file
            fields_from_mapping: list[SourceField] = []
            for field_def in mapping.get("fields", []):
                source = field_def.get("source_field")
                if isinstance(source, list):
                    for s in source:
                        fields_from_mapping.append(SourceField(name=s, label=s, field_type="string"))
                elif isinstance(source, str):
                    fields_from_mapping.append(SourceField(name=source, label=source, field_type="string"))
            return fields_from_mapping

    def extract_records(
        self,
        object_name: str,
        credentials: dict,
        options: dict,
    ) -> Iterator[list[dict]]:
        """Yield pages of records for the given object from the ServiceNow API."""
        mapping = _load_mapping(object_name)
        if not mapping:
            return
        tables = mapping.get("servicenow_tables", [])
        if not tables:
            return
        driving_table = tables[0]
        base_url = credentials.get("base_url", os.getenv("SERVICE_NOW_BASE_URL", "")).rstrip("/")
        username = credentials.get("username", os.getenv("SERVICE_NOW_USERNAME", ""))
        password = credentials.get("password", os.getenv("SERVICE_NOW_PASSWORD", ""))
        batch_size = int(options.get("service_now_batch_size", 5000))
        start_date = options.get("start_date", "")
        end_date = options.get("end_date", "")

        query_parts = []
        if start_date:
            query_parts.append(f"sys_created_on>={start_date}")
        if end_date:
            query_parts.append(f"sys_created_on<={end_date}")
        query = "^".join(query_parts)

        offset = 0
        while True:
            params: dict = {
                "sysparm_limit": batch_size,
                "sysparm_offset": offset,
                "sysparm_no_count": "true",
                "sysparm_display_value": "false",
                "sysparm_exclude_reference_link": "true",
            }
            if query:
                params["sysparm_query"] = query
            try:
                resp = requests.get(
                    f"{base_url}/api/now/table/{driving_table}",
                    auth=(username, password),
                    params=params,
                    timeout=60,
                )
                resp.raise_for_status()
                records = resp.json().get("result", [])
            except Exception:
                break
            if not records:
                break
            yield records
            if len(records) < batch_size:
                break
            offset += batch_size
