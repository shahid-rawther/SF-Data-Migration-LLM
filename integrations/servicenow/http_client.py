import os
import threading
import time
import requests
from functools import wraps
from typing import Any, Callable, Dict, Optional
import base64
import gzip

from common.logger import logger

_ENV_BASE_URL = os.getenv("SERVICE_NOW_BASE_URL", "")
_ENV_USERNAME = os.getenv("SERVICE_NOW_USERNAME", "")
_ENV_PASSWORD = os.getenv("SERVICE_NOW_PASSWORD", "")


def _create_query(start_date: str, end_date: str):
    queries = []
    if start_date:
        queries.append(f"sys_created_on>={start_date}")
    if end_date:
        queries.append(f"sys_created_on<={end_date}")
    return "^".join(queries)


class ServiceNowClient:
    def __init__(self) -> None:
        logger.info("Initializing Service now client.")
        self.instance_url: str = _ENV_BASE_URL
        self._username: str = _ENV_USERNAME
        self._password: str = _ENV_PASSWORD
        # One session per thread — avoids TCP setup overhead on every call while
        # remaining safe under concurrent use by multiple worker threads.
        self._local = threading.local()
        logger.info(f"Servicenow base URL: {self.instance_url}")

    def configure(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        """Override credentials per migration run (e.g. from saved connections)."""
        if base_url:
            self.instance_url = base_url.rstrip("/")
        if username:
            self._username = username
        if password:
            self._password = password
        # Reset any cached sessions so they pick up the new credentials.
        self._local = threading.local()
        logger.info(f"ServiceNow client configured for: {self.instance_url}")

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _session(self) -> requests.Session:
        """Return a per-thread persistent session, creating one if needed."""
        session: Optional[requests.Session] = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.auth = (self._username, self._password)
            session.headers.update(self._headers())
            # Keep up to 20 connections alive in the pool per host.
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=5,
                pool_maxsize=20,
                max_retries=0,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.session = session
        return session
    
    def get_all_table_list(self):
        session = self._session()
        url = self.instance_url + "/api/now/table/sys_db_object"
        params = {"sysparm_no_count": "true"}
        response = session.get(url=url, params=params)
        response.raise_for_status()
        data = response.json()
        return [row["name"] for row in data["result"]]

    def get_attachment_row_count(self, start_date=None, end_date=None):
        session = self._session()
        queries = []
        if start_date:
            queries.append(f"sys_created_on>={start_date}")
        if end_date:
            queries.append(f"sys_created_on<={end_date}")
        url = self.instance_url + "/api/now/table/sys_attachment"
        params = {"sysparm_no_count": "true", "sysparm_query": "^".join(queries)}
        response = session.get(url=url, params=params)
        response.raise_for_status()
        count = response.json()
        return count

    def get_attachment_meta(
        self, offset=0, batch_size=10000, start_date=None, end_date=None
    ):
        session = self._session()
        queries = []
        if start_date:
            queries.append(f"sys_created_on>={start_date}")
        if end_date:
            queries.append(f"sys_created_on<={end_date}")
        url = (
            self.instance_url
            + "/api/now/table/sys_attachment?sysparm_query="
            + "^".join(queries)
        )
        params = {
            "sysparm_limit": batch_size,
            "sysparm_offset": offset,
            "sysparm_exclude_reference_link": "true",
        }
        response = session.get(url=url, params=params)
        response.raise_for_status()
        attachment_meta = response.json()
        return attachment_meta["result"]

    def get_attachment_doc_base_64(self, attachment_sys_id: str):
        session = self._session()
        offset = 0
        rows = []

        while True:
            url = f"{self.instance_url}/api/now/table/sys_attachment_doc"
            params = {
                "sysparm_query": f"sys_attachment={attachment_sys_id}",
                "sysparm_limit": 500,
                "sysparm_offset": offset,
            }

            resp = session.get(url=url, params=params)
            resp.raise_for_status()
            result = resp.json().get("result", [])
            if not result:
                break

            rows.extend(result)

            if len(result) < 500:
                break

            offset += 500

        if not rows:
            return None

        # correct key (position or sequence)
        key = "position" if "position" in rows[0] else "sequence"
        rows = sorted(rows, key=lambda r: int(r[key]))

        # correct chunk field (data or compressed_data)
        if "compressed_data" in rows[0]:
            chunks = [
                gzip.decompress(base64.b64decode(r["compressed_data"].strip()))
                for r in rows
            ]
        else:
            chunks = [base64.b64decode(r["data"].strip()) for r in rows]

        combined = gzip.decompress(b"".join(chunks))
        return combined

    def get_attachment_doc(self, attachment_sys_id: str) -> requests.Response:
        """
        Downloads full attachment content using:
        GET /api/now/attachment/<sys_id>/file
        """

        session = self._session()

        url = f"{self.instance_url}/api/now/attachment/{attachment_sys_id}/file"

        resp = session.get(url)
        resp.raise_for_status()

        # This returns raw binary content
        return resp

    def get_row_count(
        self, table_name: str, start_date: str = None, end_date: str = None
    ) -> int:
        url = f"{self.instance_url}/api/now/stats/{table_name}"

        params = {
            "sysparm_count": "true",
            "sysparm_query": _create_query(start_date=start_date, end_date=end_date),
        }

        session = self._session()
        resp = session.get(url=url, params=params)
        resp.raise_for_status()
        count = resp.json().get("result", {}).get("stats", {}).get("count", "0")
        return int(count)

    def fetch_table(
        self,
        table_name,
        start_date: str = None,
        end_date: str = None,
        offset: int = 0,
        batch_size: str = 10000,
    ):
        session = self._session()
        url = self.instance_url + f"/api/now/table/{table_name}"
        params = {
            "sysparm_no_count": "true",
            "sysparm_query": _create_query(start_date=start_date, end_date=end_date),
            "sysparm_limit": batch_size,
            "sysparm_offset": offset,
            "sysparm_display_value": "false",
            "sysparm_exclude_reference_link": "true"
        }
        response = session.get(url=url, params=params)
        response.raise_for_status()
        result = response.json().get("result", [])
        return result


servicenow_client = ServiceNowClient()
