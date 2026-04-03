import os
import time
import requests
from functools import wraps
from typing import Any, Callable, Dict, Optional
import base64
import gzip

from common.logger import logger

SERVICE_NOW_BASE_URL = os.getenv("SERVICE_NOW_BASE_URL")
SERVICE_NOW_USERNAME = os.getenv("SERVICE_NOW_USERNAME")
SERVICE_NOW_PASSWORD = os.getenv("SERVICE_NOW_PASSWORD")


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
        logger.info(f"Servicenow base URL: {SERVICE_NOW_BASE_URL}")

        self.access_token: Optional[str] = None
        self.instance_url: str = SERVICE_NOW_BASE_URL

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.auth = (SERVICE_NOW_USERNAME, SERVICE_NOW_PASSWORD)
        session.headers.update(self._headers())
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
