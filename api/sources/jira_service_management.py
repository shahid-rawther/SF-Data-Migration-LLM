"""Jira Service Management (JSM) source provider.

Connects to Atlassian's Jira REST API v3 and exposes JSM service-desk
requests, incidents, problems and changes as migratable objects.
"""
from __future__ import annotations

import re
from typing import Iterator

import requests

from api.sources.base import MigratableObject, SourceField, SourceProvider

_JSM_OBJECTS: list[dict] = [
    {
        "name": "jsm_incidents",
        "label": "Incidents",
        "category": "ITSM Core Records",
        "salesforce_object": "Case",
        "source_entity": "issues",
        "jql": "issuetype = Incident",
        "priority": 10,
    },
    {
        "name": "jsm_service_requests",
        "label": "Service Requests",
        "category": "ITSM Core Records",
        "salesforce_object": "Case",
        "source_entity": "issues",
        "jql": "issuetype in (\"Service Request\", \"Service Request with Approvals\")",
        "priority": 20,
    },
    {
        "name": "jsm_problems",
        "label": "Problems",
        "category": "ITSM Core Records",
        "salesforce_object": "Case",
        "source_entity": "issues",
        "jql": "issuetype = Problem",
        "priority": 30,
    },
    {
        "name": "jsm_changes",
        "label": "Changes",
        "category": "ITSM Core Records",
        "salesforce_object": "Case",
        "source_entity": "issues",
        "jql": "issuetype in (Change, \"Change Request\")",
        "priority": 40,
    },
    {
        "name": "jsm_users",
        "label": "Users",
        "category": "Setup & Administration",
        "salesforce_object": "Contact",
        "source_entity": "users",
        "jql": "",
        "priority": 1,
    },
    {
        "name": "jsm_organizations",
        "label": "Organizations",
        "category": "Setup & Administration",
        "salesforce_object": "Account",
        "source_entity": "organizations",
        "jql": "",
        "priority": 2,
    },
]

# Representative fields for JSM issues
_ISSUE_FIELDS: list[tuple[str, str, str]] = [
    ("summary", "Summary", "string"),
    ("description", "Description", "string"),
    ("status.name", "Status", "string"),
    ("priority.name", "Priority", "string"),
    ("issuetype.name", "Issue Type", "string"),
    ("created", "Created Date", "datetime"),
    ("updated", "Updated Date", "datetime"),
    ("resolutiondate", "Resolution Date", "datetime"),
    ("reporter.displayName", "Reporter", "reference"),
    ("assignee.displayName", "Assignee", "reference"),
    ("comment.comments", "Comments", "array"),
]


class JSMProvider(SourceProvider):
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
            for obj in _JSM_OBJECTS
        ]

    def get_row_count(self, object_name: str, credentials: dict, filters: dict) -> int:
        meta = next((o for o in _JSM_OBJECTS if o["name"] == object_name), None)
        if not meta:
            return 0
        base = self._base(credentials)
        project_key = credentials.get("project_key", "")
        jql = meta.get("jql", "")
        if project_key:
            jql = f"project = {project_key}" + (f" AND {jql}" if jql else "")
        try:
            if meta["source_entity"] == "issues":
                resp = requests.get(
                    f"{base}/rest/api/3/search",
                    auth=self._auth(credentials),
                    params={"jql": jql, "maxResults": 0},
                    timeout=15,
                )
                resp.raise_for_status()
                return resp.json().get("total", 0)
            return -1
        except Exception:
            return -1

    def get_source_fields(self, object_name: str, credentials: dict) -> list[SourceField]:
        return [
            SourceField(name=name, label=label, field_type=ftype)
            for name, label, ftype in _ISSUE_FIELDS
        ]

    def extract_records(
        self,
        object_name: str,
        credentials: dict,
        options: dict,
    ) -> Iterator[list[dict]]:
        meta = next((o for o in _JSM_OBJECTS if o["name"] == object_name), None)
        if not meta or meta["source_entity"] != "issues":
            return
        base = self._base(credentials)
        project_key = credentials.get("project_key", "")
        jql = meta.get("jql", "")
        if project_key:
            jql = f"project = {project_key}" + (f" AND {jql}" if jql else "")
        start_date = options.get("start_date", "")
        end_date = options.get("end_date", "")
        # Only append date filters if the value matches YYYY-MM-DD
        _DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        if start_date and _DATE_RE.match(start_date):
            jql += f" AND created >= '{start_date}'"
        if end_date and _DATE_RE.match(end_date):
            jql += f" AND created <= '{end_date}'"
        batch_size = 100
        start_at = 0
        while True:
            try:
                resp = requests.get(
                    f"{base}/rest/api/3/search",
                    auth=self._auth(credentials),
                    params={
                        "jql": jql,
                        "startAt": start_at,
                        "maxResults": batch_size,
                        "fields": "summary,description,status,priority,issuetype,created,updated,reporter,assignee,comment,resolutiondate",
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
            # Flatten into normalised dicts
            records = []
            for issue in issues:
                f = issue.get("fields", {})
                records.append({
                    "id": issue.get("id"),
                    "key": issue.get("key"),
                    "summary": f.get("summary", ""),
                    "description": _adf_to_text(f.get("description")),
                    "status": (f.get("status") or {}).get("name", ""),
                    "priority": (f.get("priority") or {}).get("name", ""),
                    "issuetype": (f.get("issuetype") or {}).get("name", ""),
                    "created": f.get("created", ""),
                    "updated": f.get("updated", ""),
                    "resolutiondate": f.get("resolutiondate", ""),
                    "reporter_email": ((f.get("reporter") or {}).get("emailAddress", "")),
                    "assignee_email": ((f.get("assignee") or {}).get("emailAddress", "")),
                })
            yield records
            if len(issues) < batch_size:
                break
            start_at += batch_size


def _adf_to_text(adf: object) -> str:
    """Best-effort conversion of Atlassian Document Format (ADF) to plain text."""
    if adf is None:
        return ""
    if isinstance(adf, str):
        return adf
    parts: list[str] = []
    if isinstance(adf, dict):
        for item in adf.get("content", []):
            parts.append(_adf_to_text(item))
        text = adf.get("text")
        if text:
            parts.append(text)
    return " ".join(p for p in parts if p)
