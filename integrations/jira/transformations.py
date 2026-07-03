"""JSM-specific transformation functions.

Each function follows the same contract used by the ServiceNow transforms:

    def some_transform(destination_field: str, *source_values, params=None) -> dict

It returns ``{destination_field: transformed_value}``.  The transformation
service resolves function names from the mapping JSON and calls them with
the correct source field values.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Union

import pandas as pd

from common.logger import logger
from services.internal import mappings_service
from tools.cache import execution_cache


# ── Datetime helpers ───────────────────────────────────────────────────

def convert_jira_datetime(
    destination_field: str,
    date_time: Union[pd.Timestamp, datetime, str, None],
) -> dict:
    """Convert Jira ISO-8601 datetime to Salesforce-compatible format."""
    if date_time is None or (isinstance(date_time, float) and pd.isna(date_time)) or date_time == "":
        return {destination_field: ""}
    if isinstance(date_time, pd.Timestamp):
        dt = date_time.to_pydatetime()
    elif isinstance(date_time, datetime):
        dt = date_time
    else:
        raw = str(date_time).strip()
        if not raw:
            return {destination_field: ""}
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return {destination_field: raw}

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return {destination_field: dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000")}


def convert_jira_date(
    destination_field: str,
    date_str: str | None,
) -> dict:
    """Convert a Jira date (YYYY-MM-DD) to Salesforce date."""
    if not date_str or (isinstance(date_str, float) and pd.isna(date_str)):
        return {destination_field: ""}
    return {destination_field: str(date_str).strip()[:10]}


# ── Status / priority / impact / urgency mappings ─────────────────────

_STATUS_MAP = {
    "open": "New",
    "new": "New",
    "in progress": "In Progress",
    "work in progress": "In Progress",
    "waiting for support": "On Hold",
    "waiting for customer": "On Hold",
    "pending": "On Hold",
    "on hold": "On Hold",
    "escalated": "Escalated",
    "resolved": "Resolved",
    "closed": "Closed",
    "canceled": "Closed",
    "cancelled": "Closed",
    "done": "Closed",
    "declined": "Closed",
}

_PRIORITY_MAP = {
    "highest": "Critical",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "lowest": "Low",
    "blocker": "Critical",
    "critical": "Critical",
    "major": "High",
    "minor": "Low",
    "trivial": "Low",
}

_IMPACT_MAP = {
    "highest": "1 - Extensive",
    "high": "2 - Significant",
    "medium": "3 - Moderate",
    "low": "4 - Minor",
    "lowest": "4 - Minor",
}

_URGENCY_MAP = {
    "highest": "1 - Critical",
    "high": "2 - High",
    "medium": "3 - Medium",
    "low": "4 - Low",
    "lowest": "4 - Low",
}


def look_up_jsm_status(destination_field: str, value: str | None) -> dict:
    if not value or (isinstance(value, float) and pd.isna(value)):
        return {destination_field: ""}
    mapped = _STATUS_MAP.get(str(value).strip().lower(), str(value).strip())
    return {destination_field: mapped}


def look_up_jsm_priority(destination_field: str, value: str | None) -> dict:
    if not value or (isinstance(value, float) and pd.isna(value)):
        return {destination_field: ""}
    mapped = _PRIORITY_MAP.get(str(value).strip().lower(), str(value).strip())
    return {destination_field: mapped}


def look_up_jsm_impact(destination_field: str, value: str | None) -> dict:
    if not value or (isinstance(value, float) and pd.isna(value)):
        return {destination_field: ""}
    mapped = _IMPACT_MAP.get(str(value).strip().lower(), "3 - Moderate")
    return {destination_field: mapped}


def look_up_jsm_urgency(destination_field: str, value: str | None) -> dict:
    if not value or (isinstance(value, float) and pd.isna(value)):
        return {destination_field: ""}
    mapped = _URGENCY_MAP.get(str(value).strip().lower(), "3 - Medium")
    return {destination_field: mapped}


def look_up_jsm_category(destination_field: str, value: str | None) -> dict:
    """Pass through the Jira issue type as a category label."""
    if not value or (isinstance(value, float) and pd.isna(value)):
        return {destination_field: ""}
    return {destination_field: str(value).strip()}


def look_up_jsm_resolution(destination_field: str, value: str | None) -> dict:
    _RESOLUTION_MAP = {
        "done": "Resolved",
        "won't do": "Closed",
        "duplicate": "Duplicate",
        "cannot reproduce": "Cannot Reproduce",
        "fixed": "Resolved",
        "incomplete": "Closed",
    }
    if not value or (isinstance(value, float) and pd.isna(value)):
        return {destination_field: ""}
    mapped = _RESOLUTION_MAP.get(str(value).strip().lower(), str(value).strip())
    return {destination_field: mapped}


# ── User / Account lookups ────────────────────────────────────────────

def look_up_jsm_user(destination_field: str, account_id: str | None) -> dict:
    """Resolve a Jira accountId to a Salesforce Contact/User Id."""
    if not account_id or (isinstance(account_id, float) and pd.isna(account_id)):
        return {destination_field: ""}

    def _load():
        rows = mappings_service.get_sales_force_id_mappings_for_tables(
            ["users_Account_JSMUser"]
        )
        return {row[1]: row[0] for row in rows}

    cache_key = "ID_MAPPING::JSM_USER"
    mapping: dict[str, str] = execution_cache.get_or_set(cache_key, _load)
    sf_id = mapping.get(str(account_id).strip(), "")
    return {destination_field: sf_id}


def look_up_jsm_organization(destination_field: str, org_id: str | None) -> dict:
    """Resolve a JSM organization id to a Salesforce Account Id."""
    if not org_id or (isinstance(org_id, float) and pd.isna(org_id)):
        return {destination_field: ""}

    def _load():
        rows = mappings_service.get_sales_force_id_mappings_for_tables(
            ["organizations_Account_JSMOrganization"]
        )
        return {row[1]: row[0] for row in rows}

    cache_key = "ID_MAPPING::JSM_ORG"
    mapping: dict[str, str] = execution_cache.get_or_set(cache_key, _load)
    sf_id = mapping.get(str(org_id).strip(), "")
    return {destination_field: sf_id}


# ── Text transforms ──────────────────────────────────────────────────

def convert_adf_to_text(destination_field: str, value) -> dict:
    """Convert Atlassian Document Format JSON to plain text."""
    return {destination_field: _adf_to_text(value)}


def _adf_to_text(adf) -> str:
    if adf is None:
        return ""
    if isinstance(adf, str):
        return adf
    if isinstance(adf, float) and pd.isna(adf):
        return ""
    parts: list[str] = []
    if isinstance(adf, dict):
        for item in adf.get("content", []):
            parts.append(_adf_to_text(item))
        text = adf.get("text")
        if text:
            parts.append(text)
    return " ".join(p for p in parts if p)


def populate_jsm_subject(
    destination_field: str, key: str | None, summary: str | None,
) -> dict:
    """Combine issue key and summary into a subject line."""
    parts = []
    if key and not (isinstance(key, float) and pd.isna(key)):
        parts.append(str(key).strip())
    if summary and not (isinstance(summary, float) and pd.isna(summary)):
        parts.append(str(summary).strip())
    return {destination_field: " - ".join(parts) if parts else ""}


def jsm_choose_one(destination_field: str, *args) -> dict:
    """Return the first non-empty value from a list of source fields."""
    for arg in args:
        if arg and not (isinstance(arg, float) and pd.isna(arg)):
            val = str(arg).strip()
            if val:
                return {destination_field: val}
    return {destination_field: ""}


def jsm_mask_email(destination_field: str, value: str | None) -> dict:
    env = os.getenv("ENVIRONMENT")
    email = ""
    if value and not (isinstance(value, float) and pd.isna(value)):
        email = str(value).strip()
    if env != "PROD" and email:
        email += ".invalid"
    return {destination_field: email}


def jsm_labels_to_text(destination_field: str, value: str | None) -> dict:
    """Convert comma-separated label string to semicolons for Salesforce."""
    if not value or (isinstance(value, float) and pd.isna(value)):
        return {destination_field: ""}
    return {destination_field: str(value).replace(",", ";")}


def jsm_remove_html(destination_field: str, value: str | None) -> dict:
    """Strip HTML tags from a string value."""
    if not value or (isinstance(value, float) and pd.isna(value)):
        return {destination_field: ""}
    clean = re.sub(r"<[^>]+>", "", str(value))
    return {destination_field: clean.strip()}


def jsm_truncate(
    destination_field: str, value: str | None, params: dict | None = None,
) -> dict:
    """Truncate a value to a max length (default 255)."""
    max_len = (params or {}).get("max_length", 255)
    if not value or (isinstance(value, float) and pd.isna(value)):
        return {destination_field: ""}
    return {destination_field: str(value)[:max_len]}


def jsm_constant(destination_field: str, *args, params: dict | None = None) -> dict:
    """Return a constant value from params."""
    return {destination_field: (params or {}).get("value", "")}


# ── Name splitting (Jira only has displayName, no first/last) ─────────

def jsm_extract_first_name(destination_field: str, display_name: str | None, params: dict | None = None) -> dict:
    """Extract first name from a display name like 'Mia Krystof' -> 'Mia'.

    Handles single-word names (returns empty), multi-word names (returns
    everything except the last word), and empty/null values.
    """
    if not display_name or (isinstance(display_name, float) and pd.isna(display_name)):
        return {destination_field: ""}
    parts = str(display_name).strip().split()
    if len(parts) <= 1:
        return {destination_field: ""}
    return {destination_field: " ".join(parts[:-1])}


def jsm_extract_last_name(destination_field: str, display_name: str | None, params: dict | None = None) -> dict:
    """Extract last name from a display name like 'Mia Krystof' -> 'Krystof'.

    For single-word names the entire name is treated as the last name
    (Salesforce Account.LastName is required).
    """
    if not display_name or (isinstance(display_name, float) and pd.isna(display_name)):
        return {destination_field: ""}
    parts = str(display_name).strip().split()
    return {destination_field: parts[-1] if parts else ""}
