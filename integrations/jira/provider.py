"""Jira Service Management (JSM) source plugin.

Full ``SourcePlugin`` implementation that connects to Atlassian Jira Cloud
and JSM Service Desk APIs, performs bulk CSV extraction, and integrates with
the generic ETL pipeline (transform + load) identically to ServiceNow.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterator

import requests

from integrations.base import MigratableObject, SourceField, SourcePlugin

logger = logging.getLogger(__name__)

_CATEGORY_ISSUE_TYPES = {
    "JSMIncident", "JSMServiceRequest", "JSMProblem", "JSMChange",
    "JSMComment", "JSMAttachment",
}
_SETUP_NAMES = {"JSMUser", "JSMOrganization"}

_JSM_ENTITIES = [
    "jsm_incidents", "jsm_service_requests", "jsm_problems", "jsm_changes",
    "users", "organizations",
]


def _category_for(name: str) -> str:
    if name in _CATEGORY_ISSUE_TYPES or name.startswith("JSMComment") or name.startswith("JSMAttach"):
        return "ITSM Core Records"
    if name in _SETUP_NAMES:
        return "Setup & Administration"
    return "ITSM Core Records"


def _load_mapping(name: str) -> dict | None:
    try:
        from services.internal import mappings_service
        return mappings_service.get_mapping_config(name)
    except Exception:
        return None


class JSMProvider(SourcePlugin):
    @property
    def source_id(self) -> str:
        return "jsm"

    @property
    def display_name(self) -> str:
        return "Jira Service Management"

    @property
    def description(self) -> str:
        return "Migrate ITSM records from Atlassian Jira Service Management (JSM) to Salesforce"

    def get_credentials_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["base_url", "email", "api_token"],
            "properties": {
                "base_url": {
                    "type": "string",
                    "title": "Atlassian Cloud URL",
                    "description": "e.g. https://yourorg.atlassian.net",
                },
                "email": {
                    "type": "string",
                    "title": "Atlassian Account Email",
                },
                "api_token": {
                    "type": "string",
                    "title": "API Token",
                    "format": "password",
                    "description": "Create at https://id.atlassian.com/manage-profile/security/api-tokens",
                },
                "project_key": {
                    "type": "string",
                    "title": "Project Key (optional)",
                    "description": "Scope to a specific JSM project, e.g. SD",
                },
            },
        }

    def _auth(self, credentials: dict):
        return (credentials.get("email", ""), credentials.get("api_token", ""))

    def _base(self, credentials: dict) -> str:
        return credentials.get("base_url", "").rstrip("/")

    # ── Credential validation ──────────────────────────────────────────

    def validate_credentials(self, credentials: dict) -> bool:
        base = self._base(credentials)
        if not (base and credentials.get("email") and credentials.get("api_token")):
            return False
        try:
            resp = requests.get(
                f"{base}/rest/api/3/myself",
                auth=self._auth(credentials),
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return False

    # ── Object catalogue (DB-driven) ───────────────────────────────────

    def get_objects(self) -> list[MigratableObject]:
        objects = self._get_objects_from_db()
        for obj in objects:
            obj.category = _category_for(obj.name)
        return objects

    # ── Row count ──────────────────────────────────────────────────────

    def get_row_count(self, object_name: str, credentials: dict, filters: dict) -> int:
        mapping = _load_mapping(object_name)
        base = self._base(credentials)
        project_key = credentials.get("project_key", "")
        if mapping:
            from services.internal.mappings_service import cfg_source_tables
            tables = cfg_source_tables(mapping)
            jql = mapping.get("jql_filter", "")
            entity = tables[0] if tables else "issues"
        else:
            jql = ""
            entity = "issues"

        if entity in ("users", "organizations"):
            try:
                if entity == "users":
                    resp = requests.get(
                        f"{base}/rest/api/3/users/search",
                        auth=self._auth(credentials),
                        params={"maxResults": 0},
                        timeout=15,
                    )
                    return -1
                else:
                    resp = requests.get(
                        f"{base}/rest/servicedeskapi/organization",
                        auth=self._auth(credentials),
                        params={"limit": 1},
                        timeout=15,
                    )
                    resp.raise_for_status()
                    return resp.json().get("size", 0)
            except Exception:
                return -1

        if project_key:
            jql = f"project = {project_key}" + (f" AND {jql}" if jql else "")
        try:
            resp = requests.get(
                f"{base}/rest/api/3/search",
                auth=self._auth(credentials),
                params={"jql": jql or "order by created", "maxResults": 0},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json().get("total", 0)
        except Exception:
            return -1

    # ── Schema discovery ───────────────────────────────────────────────

    def get_source_fields(self, object_name: str, credentials: dict) -> list[SourceField]:
        """Return Jira fields via the /rest/api/3/field metadata endpoint."""
        base = self._base(credentials)
        try:
            resp = requests.get(
                f"{base}/rest/api/3/field",
                auth=self._auth(credentials),
                timeout=15,
            )
            resp.raise_for_status()
            raw_fields = resp.json()
            fields: list[SourceField] = []
            for f in raw_fields:
                fid = f.get("id", "")
                name = f.get("name", fid)
                schema = f.get("schema") or {}
                field_type = schema.get("type", "string")
                fields.append(SourceField(
                    name=fid,
                    label=name,
                    field_type=field_type,
                ))
            return fields
        except Exception:
            return []

    def get_source_tables(self, credentials: dict) -> list[str]:
        """Return available entity names for extraction.

        For JSM these are logical tables derived from issue types plus
        the special ``users`` and ``organizations`` entities.
        """
        base = self._base(credentials)
        tables = ["users", "organizations"]
        try:
            resp = requests.get(
                f"{base}/rest/api/3/issuetype",
                auth=self._auth(credentials),
                timeout=15,
            )
            resp.raise_for_status()
            for it in resp.json():
                name = it.get("name", "")
                if name:
                    tables.append(name)
        except Exception:
            logger.exception("Failed to fetch Jira issue types")
        return sorted(set(tables))

    def get_table_fields(self, table_name: str, credentials: dict) -> list[SourceField]:
        """Return fields for a given logical table.

        Jira issues share a common set of system+custom fields fetched from
        the ``/rest/api/3/field`` metadata endpoint.  The ``users`` and
        ``organizations`` entities are not issues, so we return their
        known fields directly (they mirror ``_flatten_user`` /
        ``_flatten_organization`` in the extraction module).
        """
        if table_name == "users":
            return [
                SourceField(name="accountId",    label="Account ID",    field_type="string"),
                SourceField(name="displayName",  label="Display Name",  field_type="string"),
                SourceField(name="emailAddress", label="Email Address", field_type="string"),
                SourceField(name="active",       label="Active",        field_type="boolean"),
                SourceField(name="accountType",  label="Account Type",  field_type="string"),
                SourceField(name="locale",       label="Locale",        field_type="string"),
                SourceField(name="timeZone",     label="Time Zone",     field_type="string"),
            ]
        if table_name == "organizations":
            return [
                SourceField(name="id",   label="Organization ID",   field_type="string"),
                SourceField(name="name", label="Organization Name", field_type="string"),
            ]
        return self.get_source_fields(table_name, credentials)

    # ── SourcePlugin: extraction hooks ─────────────────────────────────

    def configure_client(self, credentials: dict) -> None:
        from integrations.jira.http_client import jira_client

        jira_client.configure(
            base_url=credentials.get("base_url"),
            email=credentials.get("email"),
            api_token=credentials.get("api_token"),
        )

    def get_download_path(self) -> Path:
        from config.settings import PATH_TO_DOWNLOADS
        return PATH_TO_DOWNLOADS / "jsm"

    def extract_tables(self, job_id: int, tables: list[str]) -> int:
        from integrations.jira.extraction import extract_jsm_tables
        return extract_jsm_tables(job_id, tables)

    def extract_attachment_files(self, job_id: int, table_mapping: dict) -> None:
        from integrations.jira.extraction import extract_jsm_attachment_files
        extract_jsm_attachment_files(job_id, table_mapping)

    # ── Streaming extraction (alternative path) ────────────────────────

    def extract_records(
        self,
        object_name: str,
        credentials: dict,
        options: dict,
    ) -> Iterator[list[dict]]:
        import re as _re
        _DATE_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")

        mapping = _load_mapping(object_name)
        jql = ""
        if mapping:
            jql = mapping.get("jql_filter", "")

        base = self._base(credentials)
        project_key = credentials.get("project_key", "")
        if project_key:
            jql = f"project = {project_key}" + (f" AND {jql}" if jql else "")

        start_date = options.get("start_date", "")
        end_date = options.get("end_date", "")
        if start_date and _DATE_RE.match(start_date):
            jql += f" AND created >= '{start_date}'"
        if end_date and _DATE_RE.match(end_date):
            jql += f" AND created <= '{end_date}'"
        jql = jql.strip()
        if jql.startswith("AND "):
            jql = jql[4:]

        batch_size = 100
        start_at = 0
        while True:
            try:
                resp = requests.get(
                    f"{base}/rest/api/3/search",
                    auth=self._auth(credentials),
                    params={
                        "jql": jql or "order by created",
                        "startAt": start_at,
                        "maxResults": batch_size,
                        "fields": "summary,description,status,priority,issuetype,"
                                  "created,updated,reporter,assignee,comment,"
                                  "resolutiondate,resolution,project,labels,"
                                  "components,duedate,creator",
                    },
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                issues = data.get("issues", [])
            except Exception:
                break
            if not issues:
                break
            from integrations.jira.extraction import _flatten_issue
            records = [_flatten_issue(i) for i in issues]
            yield records
            if len(issues) < batch_size:
                break
            start_at += batch_size
