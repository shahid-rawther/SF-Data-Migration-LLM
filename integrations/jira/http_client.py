"""Jira / JSM HTTP client.

Thread-safe wrapper around ``requests.Session`` for the Jira Cloud REST API
(v3) and the JSM Service Desk REST API.  Mirrors the design of the ServiceNow
HTTP client so the extraction module can use the same patterns.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Optional

import requests
from requests.auth import HTTPBasicAuth

from common.logger import logger

_ENV_BASE_URL = os.getenv("JSM_BASE_URL", "")
_ENV_EMAIL = os.getenv("JSM_EMAIL", "")
_ENV_API_TOKEN = os.getenv("JSM_API_TOKEN", "")


class JiraClient:
    def __init__(self) -> None:
        self.base_url: str = _ENV_BASE_URL.rstrip("/")
        self._email: str = _ENV_EMAIL
        self._api_token: str = _ENV_API_TOKEN
        self._local = threading.local()

    def configure(
        self,
        base_url: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
    ) -> None:
        if base_url:
            self.base_url = base_url.rstrip("/")
        if email:
            self._email = email
        if api_token:
            self._api_token = api_token
        self._local = threading.local()
        logger.info("Jira client configured for: %s", self.base_url)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _session(self) -> requests.Session:
        session: Optional[requests.Session] = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.auth = HTTPBasicAuth(self._email, self._api_token)
            session.headers.update({
                "Accept": "application/json",
                "Content-Type": "application/json",
            })
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=5, pool_maxsize=20, max_retries=2,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.session = session
        return session

    def _get(self, path: str, params: dict | None = None, timeout: int = 60) -> dict:
        url = f"{self.base_url}{path}"
        resp = self._session().get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def _get_raw(self, path: str, timeout: int = 120) -> requests.Response:
        url = f"{self.base_url}{path}"
        resp = self._session().get(url, timeout=timeout, stream=True)
        resp.raise_for_status()
        return resp

    # ------------------------------------------------------------------
    # Jira Core REST API v3
    # ------------------------------------------------------------------

    def fetch_issues(
        self,
        jql: str,
        fields: str = "*all",
        start_at: int = 0,
        max_results: int = 100,
    ) -> dict:
        return self._get("/rest/api/3/search", params={
            "jql": jql,
            "startAt": start_at,
            "maxResults": max_results,
            "fields": fields,
        })

    def get_issue_count(self, jql: str) -> int:
        data = self._get("/rest/api/3/search", params={
            "jql": jql, "maxResults": 0,
        })
        return data.get("total", 0)

    def fetch_all_fields(self) -> list[dict]:
        return self._get("/rest/api/3/field")

    def fetch_issue_types(self) -> list[dict]:
        return self._get("/rest/api/3/issuetype")

    def fetch_projects(self) -> list[dict]:
        return self._get("/rest/api/3/project/search", params={
            "maxResults": 200,
        }).get("values", [])

    def fetch_users(
        self, start_at: int = 0, max_results: int = 200,
    ) -> list[dict]:
        return self._get("/rest/api/3/users/search", params={
            "startAt": start_at, "maxResults": max_results,
        })

    def fetch_myself(self) -> dict:
        return self._get("/rest/api/3/myself", timeout=10)

    def fetch_issue_comments(
        self, issue_key: str, start_at: int = 0, max_results: int = 100,
    ) -> dict:
        return self._get(
            f"/rest/api/3/issue/{issue_key}/comment",
            params={"startAt": start_at, "maxResults": max_results},
        )

    # ------------------------------------------------------------------
    # JSM Service Desk API
    # ------------------------------------------------------------------

    def fetch_service_desks(
        self, start: int = 0, limit: int = 100,
    ) -> dict:
        return self._get("/rest/servicedeskapi/servicedesk", params={
            "start": start, "limit": limit,
        })

    def fetch_organizations(
        self, start: int = 0, limit: int = 50,
    ) -> dict:
        return self._get("/rest/servicedeskapi/organization", params={
            "start": start, "limit": limit,
        })

    def fetch_organization(self, org_id: int) -> dict:
        return self._get(f"/rest/servicedeskapi/organization/{org_id}")

    def fetch_request_comments(
        self, issue_key: str, start: int = 0, limit: int = 100,
    ) -> dict:
        return self._get(
            f"/rest/servicedeskapi/request/{issue_key}/comment",
            params={"start": start, "limit": limit},
        )

    def fetch_request_attachments(
        self, issue_key: str, start: int = 0, limit: int = 100,
    ) -> dict:
        return self._get(
            f"/rest/servicedeskapi/request/{issue_key}/attachment",
            params={"start": start, "limit": limit},
        )

    def fetch_request_sla(
        self, issue_key: str, start: int = 0, limit: int = 50,
    ) -> dict:
        return self._get(
            f"/rest/servicedeskapi/request/{issue_key}/sla",
            params={"start": start, "limit": limit},
        )

    def fetch_customers(
        self, service_desk_id: int | str, start: int = 0, limit: int = 100,
    ) -> dict:
        return self._get(
            f"/rest/servicedeskapi/servicedesk/{service_desk_id}/customer",
            params={"start": start, "limit": limit},
        )

    def fetch_knowledgebase_articles(
        self, query: str = "", start: int = 0, limit: int = 100,
    ) -> dict:
        return self._get("/rest/servicedeskapi/knowledgebase/article", params={
            "query": query or "*",
            "highlight": False,
            "start": start,
            "limit": limit,
        })

    # ------------------------------------------------------------------
    # Attachment binary download
    # ------------------------------------------------------------------

    def download_attachment(self, attachment_id: str) -> requests.Response:
        return self._get_raw(f"/rest/api/3/attachment/content/{attachment_id}")

    # ------------------------------------------------------------------
    # Row-count helper (entity-aware)
    # ------------------------------------------------------------------

    def get_row_count(self, entity: str, jql: str = "", project_key: str = "") -> int:
        if entity == "issues":
            full_jql = jql
            if project_key:
                full_jql = f"project = {project_key}" + (f" AND {jql}" if jql else "")
            return self.get_issue_count(full_jql or "order by created")
        if entity == "users":
            users = self.fetch_users(start_at=0, max_results=1)
            return len(users) if users else 0
        if entity == "organizations":
            data = self.fetch_organizations(start=0, limit=1)
            return data.get("size", 0)
        return -1


jira_client = JiraClient()
