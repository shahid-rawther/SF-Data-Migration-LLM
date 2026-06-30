import logging
import os
import json
from typing import Iterator

import requests

from integrations.base import MigratableObject, SourceField, SourcePlugin

logger = logging.getLogger(__name__)
from services.internal.mappings_service import cfg_source_tables, cfg_destination_object

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
    """Load a mapping config from DB (primary source)."""
    try:
        from services.internal import mappings_service
        return mappings_service.get_mapping_config(name)
    except Exception:
        return None


class ServiceNowProvider(SourcePlugin):
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
                headers={"Accept": "application/json"},
                params={"sysparm_limit": 1, "sysparm_no_count": "true"},
                timeout=10,
            )
            ct = resp.headers.get("content-type", "")
            if "application/json" not in ct:
                return False
            return resp.status_code == 200
        except Exception:
            return False

    def get_objects(self) -> list[MigratableObject]:
        """DB-driven object list with ServiceNow-specific category labels."""
        objects = self._get_objects_from_db()
        for obj in objects:
            obj.category = _category_for(obj.name)
        return objects

    def get_row_count(self, object_name: str, credentials: dict, filters: dict) -> int:
        mapping = _load_mapping(object_name)
        if not mapping:
            return 0
        tables = cfg_source_tables(mapping)
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
                headers={"Accept": "application/json"},
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
        tables = cfg_source_tables(mapping)
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
                headers={"Accept": "application/json"},
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

    def get_source_tables(self, credentials: dict) -> list[str]:
        """Return all ServiceNow table names via the sys_db_object table."""
        import json as _json, time as _time, pathlib as _pathlib
        _dbg = _pathlib.Path("debug-7fb1c3.log")
        def _log(msg, data=None, hid=""):
            with open(_dbg, "a") as _f:
                _f.write(_json.dumps({"sessionId":"7fb1c3","hypothesisId":hid,"location":"provider.py:get_source_tables","message":msg,"data":data or {},"timestamp":int(_time.time()*1000)}) + "\n")
        # #region agent log
        _log("entry", {"creds_keys": list(credentials.keys()), "has_base_url": bool(credentials.get("base_url")), "has_username": bool(credentials.get("username")), "has_password": bool(credentials.get("password"))}, "H1")
        # #endregion
        base_url = credentials.get("base_url", os.getenv("SERVICE_NOW_BASE_URL", "")).rstrip("/")
        username = credentials.get("username", os.getenv("SERVICE_NOW_USERNAME", ""))
        password = credentials.get("password", os.getenv("SERVICE_NOW_PASSWORD", ""))
        if not base_url or not username:
            # #region agent log
            _log("missing_creds", {"base_url": base_url, "username": username}, "H1")
            # #endregion
            return []
        try:
            url = f"{base_url}/api/now/table/sys_db_object"
            # #region agent log
            _log("before_request", {"url": url, "username": username}, "H5")
            # #endregion
            resp = requests.get(
                url,
                auth=(username, password),
                headers={"Accept": "application/json"},
                params={
                    "sysparm_fields": "name",
                    "sysparm_no_count": "true",
                    "sysparm_limit": 10000,
                },
                timeout=60,
                allow_redirects=False,
            )
            # #region agent log
            _log("after_request", {
                "status": resp.status_code,
                "content_type": resp.headers.get("content-type", "?"),
                "body_len": len(resp.text),
                "body_preview": resp.text[:500],
                "is_redirect": resp.is_redirect,
                "redirect_url": resp.headers.get("location", ""),
                "resp_headers": dict(list(resp.headers.items())[:10]),
            }, "H2,H3,H4,H5")
            # #endregion
            if resp.status_code in (301, 302, 303, 307, 308):
                # #region agent log
                _log("redirect_detected", {"location": resp.headers.get("location", "")}, "H2")
                # #endregion
                return []
            ct = resp.headers.get("content-type", "")
            if "application/json" not in ct:
                body_lower = resp.text[:1000].lower()
                if "hibernat" in body_lower:
                    raise RuntimeError(
                        "ServiceNow instance is hibernating. "
                        "Wake it up at https://developer.servicenow.com and try again."
                    )
                raise RuntimeError(
                    f"ServiceNow returned {ct or 'unknown content type'} instead of JSON. "
                    "The instance may be hibernating or unreachable."
                )
            resp.raise_for_status()
            rows = resp.json().get("result", [])
            names = sorted({r.get("name", "") for r in rows if r.get("name")})
            # #region agent log
            _log("success", {"table_count": len(names), "first_5": names[:5]}, "H3")
            # #endregion
            return names
        except Exception as exc:
            # #region agent log
            _log("exception", {"error": str(exc), "type": type(exc).__name__}, "H4")
            # #endregion
            logger.exception("Failed to fetch source tables from ServiceNow")
            return []

    def get_table_fields(self, table_name: str, credentials: dict) -> list[SourceField]:
        """Return all fields of a raw ServiceNow table via sys_dictionary."""
        if not table_name:
            return []
        base_url = credentials.get("base_url", os.getenv("SERVICE_NOW_BASE_URL", "")).rstrip("/")
        username = credentials.get("username", os.getenv("SERVICE_NOW_USERNAME", ""))
        password = credentials.get("password", os.getenv("SERVICE_NOW_PASSWORD", ""))
        try:
            resp = requests.get(
                f"{base_url}/api/now/table/sys_dictionary",
                auth=(username, password),
                headers={"Accept": "application/json"},
                params={
                    "sysparm_query": f"name={table_name}^active=true",
                    "sysparm_fields": "element,column_label,internal_type,mandatory",
                    "sysparm_limit": 1000,
                },
                timeout=20,
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
                    name=f"{table_name}.{element}",
                    label=label,
                    field_type=field_type,
                    required=required,
                ))
            return sorted(fields, key=lambda f: f.name)
        except Exception:
            return []

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
        tables = cfg_source_tables(mapping)
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
                    headers={"Accept": "application/json"},
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

    # ── SourcePlugin: bulk extraction hooks used by the ETL pipeline ────────────

    def configure_client(self, credentials: dict) -> None:
        """Point the shared ServiceNow client at this run's instance."""
        from integrations.servicenow.http_client import servicenow_client

        servicenow_client.configure(
            base_url=credentials.get("base_url"),
            username=credentials.get("username"),
            password=credentials.get("password"),
        )

    def extract_tables(self, job_id: int, tables: list[str]) -> int:
        from integrations.servicenow.extraction import extract_servicenow_tables

        return extract_servicenow_tables(job_id, tables)

    def extract_attachment_files(self, job_id: int, table_mapping: dict) -> None:
        from integrations.servicenow.extraction import extract_servicenow_files

        extract_servicenow_files(job_id, table_mapping)
