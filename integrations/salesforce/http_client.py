import csv
import io
import os
import re
import sys
import time
import requests
from functools import wraps
from typing import Any, Callable, Dict, Optional
from pathlib import Path

from common.logger import logger
from config.settings import HASH_KEY_NAME, SERVICE_NOW_ID_COLUMN


DEFAULT_SALESFORCE_API_VERSION = os.getenv("SALESFORCE_API_VERSION")
DEFAULT_SALESFORCE_CLIENT_ID = os.getenv("SALESFORCE_CLIENT_ID")
DEFAULT_SALESFORCE_CLIENT_SECRET = os.getenv("SALESFORCE_CLIENT_SECRET")
DEFAULT_SALESFORCE_BASE_URL = os.getenv("SALESFORCE_BASE_URL")
csv.field_size_limit(2**31 - 1)

# ── Rate-limit / transient retry (EC-06) ────────────────────────────────────
_SF_RETRY_ATTEMPTS = 5
_SF_RETRY_BASE_DELAY = 2.0  # seconds; doubled each attempt (capped)
_SF_RETRY_MAX_DELAY = 60.0
_SF_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


_SF_ID_RE = re.compile(r"^[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?$")


def _is_valid_salesforce_id(value: str) -> bool:
    if not value:
        return False
    return bool(_SF_ID_RE.fullmatch(str(value).strip()))



def ensure_token(func: Callable) -> Callable:
    """
    Decorator that ensures we always have a valid access token.
    If expired or missing → refresh it.
    """

    @wraps(func)
    def wrapper(self: "SalesforceClient", *args: Any, **kwargs: Any) -> Any:
        # Refresh when missing, or proactively just before a known expiry.
        if not self.access_token or (self.token_expiry and time.time() >= self.token_expiry):
            logger.info("Salesforce token missing or expired. Refreshing access token.")
            self.refresh_access_token()
        try:
            return func(self, *args, **kwargs)
        except requests.exceptions.HTTPError as exc:
            # Token may have been revoked/expired server-side: refresh once and retry.
            if exc.response is not None and exc.response.status_code == 401:
                logger.info("Salesforce returned 401 — refreshing token and retrying once.")
                self.refresh_access_token()
                return func(self, *args, **kwargs)
            raise

    return wrapper


class SalesforceClient:
    def __init__(self) -> None:
        logger.info("Initializing SalesforceClient.")
        logger.info(f"Salesforce base URL: {DEFAULT_SALESFORCE_BASE_URL}")

        self.access_token: Optional[str] = None
        self.instance_url: str = DEFAULT_SALESFORCE_BASE_URL
        self.client_id: str = DEFAULT_SALESFORCE_CLIENT_ID
        self.client_secret: str = DEFAULT_SALESFORCE_CLIENT_SECRET
        self.api_version: str = DEFAULT_SALESFORCE_API_VERSION
        self.token_expiry: float = 0.0
        self._describe_cache: Dict[str, set] = {}
        self._picklist_cache: Dict[str, Dict[str, set]] = {}  # object -> field -> valid values

    def configure(
        self,
        base_url: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        api_version: Optional[str] = None,
    ) -> None:
        """Configure credentials/endpoint per migration run and reset token."""
        self.instance_url = (base_url or self.instance_url or "").rstrip("/")
        self.client_id = client_id or self.client_id
        self.client_secret = client_secret or self.client_secret
        self.api_version = api_version or self.api_version
        self.access_token = None
        self._describe_cache = {}
        self._picklist_cache = {}
        logger.info(f"Salesforce client configured for base URL: {self.instance_url}")

    def refresh_access_token(self) -> None:
        logger.info("Refreshing Salesforce access token...")

        token_url = self.instance_url + "/services/oauth2/token"

        if not (self.instance_url and self.client_id and self.client_secret):
            raise ValueError("Salesforce credentials are incomplete for token refresh")

        payload: Dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
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

        # Record expiry so ensure_token can refresh proactively. Salesforce
        # client_credentials responses may omit expires_in; when absent we leave
        # token_expiry at 0.0 and rely on 401-triggered refresh instead.
        expires_in = response_data.get("expires_in")
        try:
            self.token_expiry = time.time() + float(expires_in) - 60 if expires_in else 0.0
        except (TypeError, ValueError):
            self.token_expiry = 0.0

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

    @staticmethod
    def _extract_salesforce_error(response: requests.Response) -> str:
        try:
            data = response.json()
            if isinstance(data, list) and data:
                first = data[0] or {}
                message = first.get("message")
                code = first.get("errorCode")
                if message and code:
                    return f"{code}: {message.strip()}"
                if message:
                    return str(message).strip()
            if isinstance(data, dict):
                message = data.get("error_description") or data.get("error")
                if message:
                    return str(message)
        except ValueError:
            pass
        return response.text[:500]

    def _send_with_retry(
        self, do_request: Callable[[], requests.Response], what: str = "request"
    ) -> requests.Response:
        """Execute an HTTP call, retrying on 429/5xx with exponential backoff.

        Honors the ``Retry-After`` header when Salesforce provides it. Returns
        the final response (caller still calls ``raise_for_status``); on a hard
        rate-limit that never clears, the last response is returned so the
        existing error handling reports it.
        """
        delay = _SF_RETRY_BASE_DELAY
        response: Optional[requests.Response] = None
        for attempt in range(1, _SF_RETRY_ATTEMPTS + 1):
            response = do_request()
            if response.status_code not in _SF_TRANSIENT_STATUS:
                return response
            if attempt >= _SF_RETRY_ATTEMPTS:
                return response
            retry_after = response.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else delay
            except (TypeError, ValueError):
                wait = delay
            wait = min(wait, _SF_RETRY_MAX_DELAY)
            logger.warning(
                "Salesforce %s returned HTTP %d (attempt %d/%d) — backing off %.1fs",
                what, response.status_code, attempt, _SF_RETRY_ATTEMPTS, wait,
            )
            time.sleep(wait)
            delay = min(delay * 2, _SF_RETRY_MAX_DELAY)
        return response

    @ensure_token
    def describe_object(self, object_api_name: str) -> set:
        """Return the set of field API names that are createable on a Salesforce object (cached).
        Also populates the picklist cache for restricted picklist validation."""
        if object_api_name in self._describe_cache:
            return self._describe_cache[object_api_name]

        url = (
            f"{self.instance_url}/services/data/{self.api_version}"
            f"/sobjects/{object_api_name}/describe"
        )
        session = self._session()
        session.headers.update({"Content-Type": "application/json"})
        response = session.get(url)
        response.raise_for_status()

        data = response.json()
        fields = set()
        picklists: Dict[str, set] = {}
        for f in data.get("fields", []):
            # Only include fields that are createable (writable on insert)
            if f.get("createable", False):
                fields.add(f["name"])
            if f.get("restrictedPicklist"):
                valid_vals = {pv["value"] for pv in f.get("picklistValues", []) if pv.get("active")}
                if valid_vals:
                    picklists[f["name"]] = valid_vals

        self._describe_cache[object_api_name] = fields
        self._picklist_cache[object_api_name] = picklists
        logger.info(f"Described '{object_api_name}': {len(fields)} createable fields found, {len(picklists)} restricted picklists")
        return fields

    def get_picklist_values(self, object_api_name: str) -> Dict[str, set]:
        """Return restricted picklist fields and their valid values (requires describe_object first)."""
        if object_api_name not in self._picklist_cache:
            self.describe_object(object_api_name)
        return self._picklist_cache.get(object_api_name, {})

    @ensure_token
    def create_job(
        self,
        object_api_name: str,
        operation: str = "insert",
        line_ending: str = "CRLF",
        external_id_field: str = None,
    ):
        url = f"{self.instance_url}/services/data/{self.api_version}/jobs/ingest"
        session = self._session()
        body = {
            "object": object_api_name,
            "operation": operation,  # insert | update | upsert | delete
            "contentType": "CSV",
            "lineEnding": line_ending,  # "LF" or "CRLF" - must match your CSV
        }
        if operation == "upsert":
            if not external_id_field:
                raise ValueError(
                    "Salesforce upsert requires an external_id_field to key on"
                )
            # Bulk API needs the external-id field name to dedup/match records.
            body["externalIdFieldName"] = external_id_field
        response = self._send_with_retry(
            lambda: session.post(url=url, json=body), what="create_job"
        )
        response.raise_for_status()
        return response.json()

    @ensure_token
    def upload_csv(self, job_id: str, csv_path: Path, valid_fields: set = None, picklist_values: dict = None, force_values: dict = None):
        url = f"{self.instance_url}/services/data/{self.api_version}/jobs/ingest/{job_id}/batches"
        session = self._session()
        session.headers.update({"Content-Type": "text/csv"})

        remove_columns = {SERVICE_NOW_ID_COLUMN, HASH_KEY_NAME}

        output = io.StringIO()

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            # Keep all columns except the ones to remove
            fieldnames = [col for col in reader.fieldnames if col not in remove_columns]

            # Filter out fields that don't exist on the Salesforce object.
            # Always keep "Id" — it's required for update/upsert operations
            # but is not createable, so describe_object won't include it.
            if valid_fields:
                keep_always = {"Id"}
                invalid = [col for col in fieldnames if col not in valid_fields and col not in keep_always]
                if invalid:
                    logger.warning(f"Removing fields not found on Salesforce object: {invalid}")
                    remove_columns.update(invalid)
                    fieldnames = [col for col in fieldnames if col not in remove_columns]

            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()

            # Track how many values we blank per picklist field so the operator
            # gets a warning summary instead of silent data loss (EC-08).
            blanked_counts: Dict[str, int] = {}
            blanked_samples: Dict[str, set] = {}

            for row in reader:
                for col in remove_columns:
                    row.pop(col, None)

                # Fallback: when OwnerId carries source-system IDs (common in JSM),
                # Salesforce rejects the row with MALFORMED_ID. Clearing it lets SF
                # assign the default owner for inserts.
                owner_id = row.get("OwnerId")
                if owner_id and not _is_valid_salesforce_id(owner_id):
                    logger.warning(
                        "Clearing invalid OwnerId before upload: %s",
                        owner_id,
                    )
                    row["OwnerId"] = ""

                # Blank out invalid restricted picklist values
                if picklist_values:
                    for field_name, allowed in picklist_values.items():
                        if field_name in row and row[field_name] and row[field_name] not in allowed:
                            blanked_counts[field_name] = blanked_counts.get(field_name, 0) + 1
                            samples = blanked_samples.setdefault(field_name, set())
                            if len(samples) < 10:
                                samples.add(row[field_name])
                            row[field_name] = ""
                # Override forced values (e.g. Status="New" for insert)
                if force_values:
                    for field_name, forced_val in force_values.items():
                        if field_name in row:
                            row[field_name] = forced_val
                writer.writerow(row)

            if blanked_counts:
                for field_name, count in blanked_counts.items():
                    logger.warning(
                        "Blanked %d invalid restricted-picklist value(s) for field '%s' "
                        "before upload (rejected values e.g. %s). Map these in the wizard "
                        "to avoid data loss.",
                        count, field_name, sorted(blanked_samples.get(field_name, set())),
                    )

        csv_bytes = output.getvalue().encode("utf-8")

        r = self._send_with_retry(
            lambda: session.put(url=url, data=csv_bytes), what="upload_csv"
        )
        r.raise_for_status()

        return r.status_code == 201  # created

    @ensure_token
    def upload_csv_data(self, job_id: str, csv_content: str):
        """Upload raw CSV string content to a bulk job."""
        url = f"{self.instance_url}/services/data/{self.api_version}/jobs/ingest/{job_id}/batches"
        session = self._session()
        session.headers.update({"Content-Type": "text/csv"})
        csv_bytes = csv_content.encode("utf-8")
        r = session.put(url=url, data=csv_bytes)
        r.raise_for_status()
        return r.status_code == 201

    @ensure_token
    def mark_upload_complete(self, job_id):
        url = f"{self.instance_url}/services/data/{self.api_version}/jobs/ingest/{job_id}"
        body = {"state": "UploadComplete"}
        session = self._session()
        session.headers.update({"Content-Type": "application/json"})
        r = self._send_with_retry(
            lambda: session.patch(url=url, json=body), what="mark_upload_complete"
        )
        r.raise_for_status()
        return r.json()

    @ensure_token
    def get_job(self, job_id: str):
        url = f"{self.instance_url}/services/data/{self.api_version}/jobs/ingest/{job_id}"
        session = self._session()
        session.headers.update({"Content-Type": "application/json"})
        r = self._send_with_retry(lambda: session.get(url), what="get_job")
        r.raise_for_status()
        return r.json()

    @ensure_token
    def get_results(self, job_id, kind="successfulResults"):
        """
        kind: successfulResults | failedResults | unprocessedRecords
        """
        url = f"{self.instance_url}/services/data/{self.api_version}/jobs/ingest/{job_id}/{kind}"
        session = self._session()
        session.headers.update({"Content-Type": "application/json"})
        response = self._send_with_retry(lambda: session.get(url), what="get_results")
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
            f"{self.api_version}/composite/sobjects"
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
                f"{self.instance_url}/services/data/{self.api_version}/query?q="
                + query
            )
        else:
            url = f"{self.instance_url}/{next_records_url}"
        session = self._session()
        session.headers.update({"Content-Type": "application/json"})
        response = session.get(url)
        if not response.ok:
            detail = self._extract_salesforce_error(response)
            raise RuntimeError(f"Salesforce query failed for '{object_name}': {detail}")
        return response.json()

    @ensure_token
    def query_records(
        self, object_api_name: str, name_field: str = "Name", limit: int = 200
    ) -> list[dict]:
        """Return up to ``limit`` records of an object as ``[{id, name}]``.

        Used by the wizard's reference picker so a user can choose an org record
        (e.g. a Profile) by name and store its Id as a per-field constant.
        """
        safe_limit = max(1, min(int(limit), 2000))
        query = f"SELECT Id, {name_field} FROM {object_api_name} ORDER BY {name_field} LIMIT {safe_limit}"
        url = f"{self.instance_url}/services/data/{self.api_version}/query?q={query}"
        session = self._session()
        session.headers.update({"Content-Type": "application/json"})
        response = self._send_with_retry(lambda: session.get(url), what="query_records")
        if not response.ok:
            detail = self._extract_salesforce_error(response)
            raise RuntimeError(f"Salesforce query failed for '{object_api_name}': {detail}")
        records = response.json().get("records", [])
        return [
            {"id": r.get("Id", ""), "name": r.get(name_field, "")}
            for r in records
        ]

    @ensure_token
    def get_row_count(
        self, object_name: str, start_date: str = None, end_date: str = None
    ) -> int:
        query = f"SELECT COUNT() FROM {object_name}"
        url = (
            f"{self.instance_url}/services/data/{self.api_version}/query?q="
            + query
        )
        session = self._session()
        session.headers.update({"Content-Type": "application/json"})
        response = session.get(url)
        if not response.ok:
            detail = self._extract_salesforce_error(response)
            raise RuntimeError(f"Salesforce row count failed for '{object_name}': {detail}")
        res = response.json()
        return int(res["totalSize"])


salesforce_client = SalesforceClient()
