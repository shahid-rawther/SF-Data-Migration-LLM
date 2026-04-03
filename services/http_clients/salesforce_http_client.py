import csv
import io
import os
import sys
import time
import requests
from functools import wraps
from typing import Any, Callable, Dict, Optional
from pathlib import Path

from common.logger import logger
from config.settings import HASH_KEY_NAME, SERVICE_NOW_ID_COLUMN


SALESFORCE_API_VERSION = os.getenv("SALESFORCE_API_VERSION")
SALESFORCE_CLIENT_ID = os.getenv("SALESFORCE_CLIENT_ID")
SALESFORCE_CLIENT_SECRET = os.getenv("SALESFORCE_CLIENT_SECRET")
SALESFORCE_BASE_URL = os.getenv("SALESFORCE_BASE_URL")
csv.field_size_limit(2**31 - 1)



def ensure_token(func: Callable) -> Callable:
    """
    Decorator that ensures we always have a valid access token.
    If expired or missing → refresh it.
    """

    @wraps(func)
    def wrapper(self: "SalesforceClient", *args: Any, **kwargs: Any) -> Any:
        if not self.access_token:
            logger.info("Salesforce token missing. Refreshing access token.")
            self.refresh_access_token()
        return func(self, *args, **kwargs)

    return wrapper


class SalesforceClient:
    def __init__(self) -> None:
        logger.info("Initializing SalesforceClient.")
        logger.info(f"Salesforce base URL: {SALESFORCE_BASE_URL}")

        self.access_token: Optional[str] = None
        self.instance_url: str = SALESFORCE_BASE_URL
        self.token_expiry: float = 0.0

    def refresh_access_token(self) -> None:
        logger.info("Refreshing Salesforce access token...")

        token_url = self.instance_url + "/services/oauth2/token"

        payload: Dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": SALESFORCE_CLIENT_ID,
            "client_secret": SALESFORCE_CLIENT_SECRET,
        }

        try:
            response: requests.Response = requests.post(token_url, data=payload)
            response.raise_for_status()
        except Exception as exc:
            logger.error("Failed to refresh Salesforce access token: %s", exc)
            logger.exception("Exception details:")
            raise

        response_data: Dict[str, Any] = response.json()

        self.access_token = response_data["access_token"]

        logger.info("Salesforce access token refreshed successfully.")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _session(self):
        session = requests.Session()
        session.headers.update(self._headers())
        return session

    @ensure_token
    def create_job(
        self, object_api_name: str, operation: str = "insert", line_ending: str = "CRLF"
    ):
        url = f"{self.instance_url}/services/data/{SALESFORCE_API_VERSION}/jobs/ingest"
        session = self._session()
        body = {
            "object": object_api_name,
            "operation": operation,  # insert | update | upsert | delete
            "contentType": "CSV",
            "lineEnding": line_ending,  # "LF" or "CRLF" - must match your CSV
        }
        response = session.post(url=url, json=body)
        response.raise_for_status()
        return response.json()

    @ensure_token
    def upload_csv(self, job_id: str, csv_path: Path):
        url = f"{self.instance_url}/services/data/{SALESFORCE_API_VERSION}/jobs/ingest/{job_id}/batches"
        session = self._session()
        session.headers.update({"Content-Type": "text/csv"})

        remove_columns = {SERVICE_NOW_ID_COLUMN, HASH_KEY_NAME}

        output = io.StringIO()

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            # Keep all columns except the ones to remove
            fieldnames = [col for col in reader.fieldnames if col not in remove_columns]

            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()

            for row in reader:
                for col in remove_columns:
                    row.pop(col, None)
                writer.writerow(row)

        csv_bytes = output.getvalue().encode("utf-8")

        r = session.put(url=url, data=csv_bytes)
        r.raise_for_status()

        return r.status_code == 201  # created

    @ensure_token
    def mark_upload_complete(self, job_id):
        url = f"{self.instance_url}/services/data/{SALESFORCE_API_VERSION}/jobs/ingest/{job_id}"
        body = {"state": "UploadComplete"}
        session = self._session()
        session.headers.update({"Content-Type": "application/json"})
        r = session.patch(url=url, json=body)
        r.raise_for_status()
        return r.json()

    @ensure_token
    def get_job(self, job_id: str):
        url = f"{self.instance_url}/services/data/{SALESFORCE_API_VERSION}/jobs/ingest/{job_id}"
        session = self._session()
        session.headers.update({"Content-Type": "application/json"})
        r = session.get(url)
        r.raise_for_status()
        return r.json()

    @ensure_token
    def get_results(self, job_id, kind="successfulResults"):
        """
        kind: successfulResults | failedResults | unprocessedRecords
        """
        url = f"{self.instance_url}/services/data/{SALESFORCE_API_VERSION}/jobs/ingest/{job_id}/{kind}"
        session = self._session()
        session.headers.update({"Content-Type": "application/json"})
        response = session.get(url)
        response.raise_for_status()
        content = response.text
        return content

    @ensure_token
    def post_attachments(
        self, data: list[dict]
    ):
        """
        Upload ContentVersion records using Salesforce Composite API.
        Max 25 records per call.
        """

        if not data:
            return None

        if len(data) > 25:
            raise ValueError("Composite API supports max 25 records per request")

        url = (
            f"{self.instance_url}/services/data/"
            f"{SALESFORCE_API_VERSION}/composite/sobjects"
        )

        records = []
        for row in data:
            record = {
                "attributes": {"type": "ContentVersion"},
                "Title": row["Title"],
                "PathOnClient": row["PathOnClient"],
                "VersionData": row["VersionData"],
                "FirstPublishLocationId": row["FirstPublishLocationId"],
            }
            records.append(record)

        payload = {
            "allOrNone": False,
            "records": records,
        }

        session = self._session()
        session.headers.update({"Content-Type": "application/json"})

        response = session.post(url, json=payload)
        response.raise_for_status()

        return response.json()

    @ensure_token
    def fetch_table(self, object_name: str, fields: list, next_records_url):
        if not next_records_url:
            fields = ",".join(fields)
            query = f"SELECT {fields} FROM {object_name}"
            url = (
                f"{self.instance_url}/services/data/{SALESFORCE_API_VERSION}/query?q="
                + query
            )
        else:
            url = f"{self.instance_url}/{next_records_url}"
        session = self._session()
        session.headers.update({"Content-Type": "application/json"})
        response = session.get(url)
        response.raise_for_status()
        return response.json()

    @ensure_token
    def get_row_count(
        self, object_name: str, start_date: str = None, end_date: str = None
    ) -> int:
        query = f"SELECT COUNT() FROM {object_name}"
        url = (
            f"{self.instance_url}/services/data/{SALESFORCE_API_VERSION}/query?q="
            + query
        )
        session = self._session()
        session.headers.update({"Content-Type": "application/json"})
        response = session.get(url)
        response.raise_for_status()
        res = response.json()
        return int(res["totalSize"])


salesforce_client = SalesforceClient()
