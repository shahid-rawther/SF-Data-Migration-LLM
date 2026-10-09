"""What the destination org already holds, read before anything is migrated.

The readiness check needs three facts no field describe gives: whether an
object can be created through the API at all, which records of it already
exist, and — for users — whether the org has the licence seats to take them.
Everything here is a read except :func:`insert_record` and
:func:`delete_record`, which only the approved readiness actions call.
"""
from __future__ import annotations

import time

from common.logger import logger

# The global describe is large and changes rarely, so it is kept for a few
# minutes per org rather than fetched for every object checked.
_CATALOG_TTL_SECONDS = 600
_catalog_cache: dict[str, tuple[float, dict[str, dict]]] = {}

MAX_SAMPLE = 50


def _session(credentials: dict):
    from api.compat_engine import _sf_session

    return _sf_session(credentials or {})


def _identifier(name: str) -> str:
    from api.compat_engine import _safe_identifier

    return _safe_identifier(name or "")


def _literal(value: str) -> str:
    """Escape a value for an equality comparison (not LIKE) in SOQL."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def object_catalog(credentials: dict) -> dict[str, dict]:
    """Every object visible to the integration user, with its API capabilities.

    Unlike the destination object picker this keeps objects that cannot be
    created, because "this parent can never be inserted" is exactly what the
    readiness check has to be able to say.
    """
    key = (credentials or {}).get("base_url", "")
    cached = _catalog_cache.get(key)
    if cached and time.monotonic() - cached[0] < _CATALOG_TTL_SECONDS:
        return cached[1]

    session = _session(credentials)
    if not session:
        return {}
    try:
        import requests

        resp = requests.get(session.data_url("sobjects"), headers=session.headers, timeout=30)
        if resp.status_code != 200:
            return {}
        catalog = {
            item["name"]: {
                "label": item.get("label", item["name"]),
                "createable": bool(item.get("createable")),
                "queryable": bool(item.get("queryable")),
                "custom": bool(item.get("custom")),
            }
            for item in resp.json().get("sobjects", [])
            if item.get("name")
        }
    except Exception:
        logger.debug("Could not read the Salesforce object catalog", exc_info=True)
        return {}
    _catalog_cache[key] = (time.monotonic(), catalog)
    return catalog


def object_info(object_name: str, credentials: dict) -> dict:
    """Capabilities of one object; empty when it is unknown or unreadable."""
    return object_catalog(credentials).get(object_name, {})


def _query(soql: str, credentials: dict) -> dict | None:
    session = _session(credentials)
    if not session:
        return None
    try:
        import requests

        resp = requests.get(
            session.data_url("query"),
            headers=session.headers,
            params={"q": soql},
            timeout=30,
        )
        if resp.status_code != 200:
            logger.debug("SOQL refused (%s): %s", resp.status_code, resp.text[:300])
            return None
        return resp.json()
    except Exception:
        logger.debug("SOQL failed: %s", soql, exc_info=True)
        return None


def name_field(object_name: str, credentials: dict) -> str:
    """The field Salesforce treats as the record's name, e.g. Subject or CaseNumber."""
    from api.compat_engine import _sf_describe

    for entry in _sf_describe(object_name, credentials):
        if entry.get("nameField"):
            return entry.get("name", "Name")
    return "Name"


def count_records(object_name: str, credentials: dict) -> int:
    """How many records the object holds, or -1 when it cannot be counted."""
    safe = _identifier(object_name)
    if not safe:
        return -1
    result = _query(f"SELECT COUNT() FROM {safe}", credentials)
    return int(result.get("totalSize", 0)) if result else -1


def sample_records(
    object_name: str,
    credentials: dict,
    *,
    search: str = "",
    limit: int = 20,
) -> dict:
    """Id and name of existing records, optionally narrowed by a name search."""
    safe = _identifier(object_name)
    if not safe:
        return {"records": [], "total": -1, "name_field": ""}
    label = _identifier(name_field(object_name, credentials)) or "Name"
    limit = max(1, min(int(limit), MAX_SAMPLE))
    where = ""
    if search:
        from api.compat_engine import _soql_literal

        where = f" WHERE {label} LIKE '%{_soql_literal(search)}%'"
    result = _query(
        f"SELECT Id, {label} FROM {safe}{where} ORDER BY {label} LIMIT {limit}",
        credentials,
    )
    if result is None:
        return {"records": [], "total": -1, "name_field": label}
    records = [
        {"id": row.get("Id", ""), "name": str(row.get(label) or "")}
        for row in result.get("records", [])
    ]
    return {"records": records, "total": count_records(object_name, credentials), "name_field": label}


def existing_ids(object_name: str, ids: list[str], credentials: dict) -> set[str] | None:
    """Which of ``ids`` are real records of ``object_name``; None when unknowable."""
    safe = _identifier(object_name)
    wanted = sorted({str(i) for i in ids if i})
    if not safe or not wanted:
        return set()
    quoted = ", ".join(f"'{_literal(i)}'" for i in wanted[:200])
    result = _query(f"SELECT Id FROM {safe} WHERE Id IN ({quoted})", credentials)
    if result is None:
        return None
    found = {row.get("Id", "") for row in result.get("records", [])}
    # A 15-character id matches its 18-character form, so compare on the prefix.
    short = {value[:15] for value in found}
    return {value for value in wanted if value in found or value[:15] in short}


def user_license_capacity(credentials: dict) -> list[dict]:
    """Seats per user licence: total, used and still available."""
    result = _query(
        "SELECT Id, Name, TotalLicenses, UsedLicenses, Status FROM UserLicense "
        "WHERE Status = 'Active' ORDER BY Name",
        credentials,
    )
    if not result:
        return []
    capacity = []
    for row in result.get("records", []):
        total = int(row.get("TotalLicenses") or 0)
        used = int(row.get("UsedLicenses") or 0)
        capacity.append(
            {
                "id": row.get("Id", ""),
                "name": row.get("Name", ""),
                "total": total,
                "used": used,
                "available": max(total - used, 0),
            }
        )
    return capacity


def profile_licenses(credentials: dict) -> dict[str, str]:
    """Profile id -> the user licence it consumes."""
    result = _query("SELECT Id, UserLicense.Name FROM Profile", credentials)
    if not result:
        return {}
    return {
        row.get("Id", ""): ((row.get("UserLicense") or {}).get("Name") or "")
        for row in result.get("records", [])
    }


# ── writes (approved readiness actions only) ──────────────────────────────────


class SalesforceWriteError(RuntimeError):
    """Salesforce refused a write the person approved."""


def _error_text(resp) -> str:
    try:
        body = resp.json()
        if isinstance(body, list) and body:
            return "; ".join(
                f"{item.get('errorCode', '')}: {item.get('message', '')}".strip(": ")
                for item in body
            )
        if isinstance(body, dict):
            return str(body.get("message") or body)
    except Exception:
        pass
    return resp.text[:500]


def insert_record(object_name: str, values: dict, credentials: dict) -> str:
    """Create one record and return its id."""
    session = _session(credentials)
    if not session:
        raise SalesforceWriteError("No usable Salesforce credentials")
    safe = _identifier(object_name)
    import requests

    resp = requests.post(
        session.data_url(f"sobjects/{safe}"),
        headers={**session.headers, "Content-Type": "application/json"},
        json=values,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise SalesforceWriteError(f"{object_name} was not created: {_error_text(resp)}")
    record_id = (resp.json() or {}).get("id", "")
    if not record_id:
        raise SalesforceWriteError(f"{object_name} create returned no id")
    return record_id


def delete_record(object_name: str, record_id: str, credentials: dict) -> None:
    session = _session(credentials)
    if not session:
        raise SalesforceWriteError("No usable Salesforce credentials")
    import requests

    resp = requests.delete(
        session.data_url(f"sobjects/{_identifier(object_name)}/{_identifier(record_id)}"),
        headers=session.headers,
        timeout=30,
    )
    if resp.status_code not in (200, 204, 404):
        raise SalesforceWriteError(f"{object_name} {record_id} was not deleted: {_error_text(resp)}")
