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

    def count_rows(self, table_name: str, credentials: dict, query: str = "") -> int:
        """Count rows through the Aggregate API, so nothing has to be extracted."""
        if not table_name:
            return -1
        base_url = credentials.get("base_url", os.getenv("SERVICE_NOW_BASE_URL", "")).rstrip("/")
        username = credentials.get("username", os.getenv("SERVICE_NOW_USERNAME", ""))
        password = credentials.get("password", os.getenv("SERVICE_NOW_PASSWORD", ""))
        if not base_url or not username:
            return -1
        params = {"sysparm_count": "true"}
        if query:
            params["sysparm_query"] = query
        try:
            resp = requests.get(
                f"{base_url}/api/now/stats/{table_name}",
                auth=(username, password),
                headers={"Accept": "application/json"},
                params=params,
                timeout=20,
                allow_redirects=False,
            )
            if resp.status_code != 200 or "application/json" not in resp.headers.get(
                "content-type", ""
            ):
                return -1
            count = resp.json().get("result", {}).get("stats", {}).get("count", "0")
            return int(count)
        except Exception:
            logger.debug("Could not count %s (%s)", table_name, query, exc_info=True)
            return -1

    def count_empty(self, table_name: str, field_name: str, credentials: dict) -> int:
        element = (field_name or "").split(".")[-1]
        if not element:
            return -1
        return self.count_rows(table_name, credentials, f"{element}ISEMPTY")

    @staticmethod
    def _connection(credentials: dict) -> tuple[str, tuple[str, str]]:
        base_url = credentials.get("base_url", os.getenv("SERVICE_NOW_BASE_URL", "")).rstrip("/")
        username = credentials.get("username", os.getenv("SERVICE_NOW_USERNAME", ""))
        password = credentials.get("password", os.getenv("SERVICE_NOW_PASSWORD", ""))
        if not base_url or not username:
            raise ValueError("ServiceNow credentials are missing")
        return base_url, (username, password)

    def find_record_ids(
        self, table_name: str, credentials: dict, query: str, limit: int
    ) -> list[str]:
        base_url, auth = self._connection(credentials)
        ids: list[str] = []
        page = min(limit, 1000)
        while len(ids) < limit:
            resp = requests.get(
                f"{base_url}/api/now/table/{table_name}",
                auth=auth,
                headers={"Accept": "application/json"},
                params={
                    "sysparm_query": f"{query}^ORDERBYsys_id" if query else "ORDERBYsys_id",
                    "sysparm_fields": "sys_id",
                    "sysparm_limit": min(page, limit - len(ids)),
                    "sysparm_offset": len(ids),
                    "sysparm_no_count": "true",
                },
                timeout=60,
                allow_redirects=False,
            )
            resp.raise_for_status()
            batch = [r["sys_id"] for r in resp.json().get("result", []) if r.get("sys_id")]
            ids.extend(batch)
            if len(batch) < page:
                break
        return ids[:limit]

    def query_records(
        self, table_name: str, credentials: dict, query: str, fields: list[str], limit: int
    ) -> list[dict]:
        """A few records of a table, with display values, for a person or agent to choose from."""
        base_url, auth = self._connection(credentials)
        resp = requests.get(
            f"{base_url}/api/now/table/{table_name}",
            auth=auth,
            headers={"Accept": "application/json"},
            params={
                "sysparm_query": query,
                "sysparm_fields": ",".join(["sys_id", *fields]),
                "sysparm_limit": limit,
                "sysparm_display_value": "true",
                "sysparm_exclude_reference_link": "true",
                "sysparm_no_count": "true",
            },
            timeout=30,
            allow_redirects=False,
        )
        resp.raise_for_status()
        return resp.json().get("result", [])

    def update_record(
        self, table_name: str, record_id: str, values: dict, credentials: dict
    ) -> None:
        base_url, auth = self._connection(credentials)
        resp = requests.patch(
            f"{base_url}/api/now/table/{table_name}/{record_id}",
            auth=auth,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            # Keep sys_updated_on/by as they were: the migration carries them over.
            params={"sysparm_suppress_auto_sys_field": "true", "sysparm_fields": "sys_id"},
            json=values,
            timeout=30,
            allow_redirects=False,
        )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", {}).get("message") or resp.text[:300]
            except ValueError:
                detail = resp.text[:300]
            raise RuntimeError(f"ServiceNow refused the update of {table_name}/{record_id}: {detail}")

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
        base_url = credentials.get("base_url", os.getenv("SERVICE_NOW_BASE_URL", "")).rstrip("/")
        username = credentials.get("username", os.getenv("SERVICE_NOW_USERNAME", ""))
        password = credentials.get("password", os.getenv("SERVICE_NOW_PASSWORD", ""))
        if not base_url or not username:
            return []
        try:
            resp = requests.get(
                f"{base_url}/api/now/table/sys_db_object",
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
            if resp.status_code in (301, 302, 303, 307, 308):
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
            logger.info("Fetched %d source tables from ServiceNow", len(names))
            return names
        except Exception:
            logger.exception("Failed to fetch source tables from ServiceNow")
            return []

    def _resolve_table_hierarchy(self, table_name: str, base_url: str, auth: tuple) -> list[str]:
        """Walk the sys_db_object parent chain to collect the full table hierarchy.

        Returns a list starting with *table_name* followed by its ancestors
        (e.g. ``["change_request", "task"]``).  Stops after 5 levels to avoid
        runaway loops.
        """
        tables = [table_name]
        current = table_name
        for _ in range(5):
            try:
                resp = requests.get(
                    f"{base_url}/api/now/table/sys_db_object",
                    auth=auth,
                    headers={"Accept": "application/json"},
                    params={
                        "sysparm_query": f"name={current}",
                        "sysparm_fields": "super_class",
                        "sysparm_limit": 1,
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                rows = resp.json().get("result", [])
                if not rows:
                    break
                sc = rows[0].get("super_class") or {}
                parent_id = sc.get("value", "") if isinstance(sc, dict) else str(sc)
                if not parent_id:
                    break
                # Resolve the parent sys_id to a table name
                resp2 = requests.get(
                    f"{base_url}/api/now/table/sys_db_object/{parent_id}",
                    auth=auth,
                    headers={"Accept": "application/json"},
                    params={"sysparm_fields": "name"},
                    timeout=10,
                )
                resp2.raise_for_status()
                parent_name = resp2.json().get("result", {}).get("name", "")
                if not parent_name or parent_name in tables:
                    break
                tables.append(parent_name)
                current = parent_name
            except Exception:
                break
        return tables

    def get_table_fields(self, table_name: str, credentials: dict) -> list[SourceField]:
        """Return all fields of a raw ServiceNow table (including inherited) via sys_dictionary."""
        if not table_name:
            return []
        base_url = credentials.get("base_url", os.getenv("SERVICE_NOW_BASE_URL", "")).rstrip("/")
        username = credentials.get("username", os.getenv("SERVICE_NOW_USERNAME", ""))
        password = credentials.get("password", os.getenv("SERVICE_NOW_PASSWORD", ""))
        auth = (username, password)
        try:
            hierarchy = self._resolve_table_hierarchy(table_name, base_url, auth)
            query_filter = "nameIN" + ",".join(hierarchy) + "^active=true"
            resp = requests.get(
                f"{base_url}/api/now/table/sys_dictionary",
                auth=auth,
                headers={"Accept": "application/json"},
                params={
                    "sysparm_query": query_filter,
                    "sysparm_fields": "element,column_label,internal_type,mandatory,reference",
                    "sysparm_limit": 2000,
                },
                timeout=30,
            )
            resp.raise_for_status()
            rows = resp.json().get("result", [])
            seen_elements: set[str] = set()
            fields: list[SourceField] = []
            for row in rows:
                element = row.get("element", "")
                if not element or element in seen_elements:
                    continue
                seen_elements.add(element)
                raw_type = row.get("internal_type") or {}
                field_type = raw_type.get("value", raw_type) if isinstance(raw_type, dict) else str(raw_type) or "string"
                raw_mandatory = row.get("mandatory") or "false"
                required = str(raw_mandatory.get("value", raw_mandatory) if isinstance(raw_mandatory, dict) else raw_mandatory).lower() == "true"
                raw_label = row.get("column_label") or element
                label = raw_label.get("display_value", element) if isinstance(raw_label, dict) else str(raw_label)
                ref_table = ""
                if field_type == "reference":
                    ref_raw = row.get("reference") or {}
                    ref_table = ref_raw.get("value", "") if isinstance(ref_raw, dict) else str(ref_raw)
                fields.append(SourceField(
                    name=f"{table_name}.{element}",
                    label=label,
                    field_type=field_type,
                    required=required,
                    reference_table=ref_table,
                ))
            return sorted(fields, key=lambda f: f.name)
        except Exception:
            return []

    def get_field_values(
        self, table_name: str, field_name: str, credentials: dict, limit: int = 500
    ) -> list[dict]:
        """Return a field's ServiceNow choice values as ``[{value, label}]``.

        Queries ``sys_choice`` for the field's element across the table's full
        inheritance chain (choices are often defined on a parent table such as
        ``task``). Returns [] on any error or when the field has no choices, so
        the wizard can fall back to manual value entry.
        """
        if not table_name or not field_name:
            return []
        # Accept either "element" or "table.element".
        element = field_name.split(".")[-1]
        base_url = credentials.get("base_url", os.getenv("SERVICE_NOW_BASE_URL", "")).rstrip("/")
        username = credentials.get("username", os.getenv("SERVICE_NOW_USERNAME", ""))
        password = credentials.get("password", os.getenv("SERVICE_NOW_PASSWORD", ""))
        auth = (username, password)
        try:
            hierarchy = self._resolve_table_hierarchy(table_name, base_url, auth)
            query = (
                "nameIN" + ",".join(hierarchy)
                + f"^element={element}^inactive=false^language=en"
            )
            resp = requests.get(
                f"{base_url}/api/now/table/sys_choice",
                auth=auth,
                headers={"Accept": "application/json"},
                params={
                    "sysparm_query": query,
                    "sysparm_fields": "value,label",
                    "sysparm_limit": limit,
                },
                timeout=30,
            )
            resp.raise_for_status()
            rows = resp.json().get("result", [])
            values: list[dict] = []
            seen: set[str] = set()
            for row in rows:
                value = str(row.get("value", "")).strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                label = str(row.get("label", "") or value).strip()
                values.append({"value": value, "label": label})
            return values
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

    def extract_tables(
        self,
        job_id: int,
        tables: list[str],
        record_limit: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> int:
        from integrations.servicenow.extraction import extract_servicenow_tables

        return extract_servicenow_tables(
            job_id, tables, record_limit=record_limit,
            start_date=start_date, end_date=end_date,
        )

    def extraction_errors(self) -> dict[str, str]:
        """Tables the most recent extract failed on, with the error each raised."""
        from integrations.servicenow.extraction import last_errors

        return dict(last_errors)

    def extract_attachment_files(self, job_id: int, table_mapping: dict) -> None:
        from integrations.servicenow.extraction import extract_servicenow_files

        extract_servicenow_files(job_id, table_mapping)
