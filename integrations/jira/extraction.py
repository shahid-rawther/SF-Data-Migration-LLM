"""JSM extraction module.

Extracts data from Jira Service Management into CSV batches under
``data/downloads/jsm/{entity}/``, mirroring the ServiceNow extraction pattern
so the generic transformation pipeline can consume the output identically.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import tqdm

from common.logger import logger
from config.settings import MAX_WORKERS, PATH_TO_DOWNLOADS
from integrations.jira.http_client import jira_client
from services.internal import jobs_service
from tools.csv_manager import save_as_csv
from tools import file_manager

JSM_BATCH_SIZE = 100  # Jira search API caps at 100

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _flatten_issue(issue: dict) -> dict:
    """Flatten a Jira issue JSON into a flat dict suitable for CSV."""
    f = issue.get("fields", {})
    return {
        "id": issue.get("id", ""),
        "key": issue.get("key", ""),
        "summary": f.get("summary", ""),
        "description": _adf_to_text(f.get("description")),
        "status": (f.get("status") or {}).get("name", ""),
        "status_category": (
            ((f.get("status") or {}).get("statusCategory") or {}).get("name", "")
        ),
        "priority": (f.get("priority") or {}).get("name", ""),
        "issuetype": (f.get("issuetype") or {}).get("name", ""),
        "created": f.get("created", ""),
        "updated": f.get("updated", ""),
        "resolutiondate": f.get("resolutiondate", ""),
        "resolution": (f.get("resolution") or {}).get("name", ""),
        "reporter_accountid": ((f.get("reporter") or {}).get("accountId", "")),
        "reporter_displayname": ((f.get("reporter") or {}).get("displayName", "")),
        "reporter_email": ((f.get("reporter") or {}).get("emailAddress", "")),
        "assignee_accountid": ((f.get("assignee") or {}).get("accountId", "")),
        "assignee_displayname": ((f.get("assignee") or {}).get("displayName", "")),
        "assignee_email": ((f.get("assignee") or {}).get("emailAddress", "")),
        "creator_accountid": ((f.get("creator") or {}).get("accountId", "")),
        "project_key": ((f.get("project") or {}).get("key", "")),
        "project_name": ((f.get("project") or {}).get("name", "")),
        "labels": ",".join(f.get("labels") or []),
        "components": ",".join(
            c.get("name", "") for c in (f.get("components") or [])
        ),
        "duedate": f.get("duedate", ""),
    }


def _flatten_user(user: dict) -> dict:
    return {
        "accountId": user.get("accountId", ""),
        "displayName": user.get("displayName", ""),
        "emailAddress": user.get("emailAddress", ""),
        "active": str(user.get("active", True)),
        "accountType": user.get("accountType", ""),
        "locale": user.get("locale", ""),
        "timeZone": user.get("timeZone", ""),
    }


def _flatten_organization(org: dict) -> dict:
    return {
        "id": str(org.get("id", "")),
        "name": org.get("name", ""),
    }


def _adf_to_text(adf: object) -> str:
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


# ── Public extraction functions ────────────────────────────────────────


def extract_jsm_tables(job_id: int, tables: list[str], record_limit: int | None = None) -> int:
    _debug_log(f"[JSM_EXTRACT] extract_jsm_tables called: job_id={job_id}, tables={tables}, record_limit={record_limit}")
    executor = ThreadPoolExecutor(max_workers=min(5, len(tables) or 1))
    futures = []
    for table in tables:
        futures.append(executor.submit(extract_jsm_table, job_id, table, record_limit=record_limit))

    total_rows = 0
    for future in as_completed(futures):
        total_rows += future.result() or 0

    executor.shutdown(wait=True)
    _debug_log(f"[JSM_EXTRACT] extract_jsm_tables done: total_rows={total_rows}")
    return total_rows


def extract_jsm_table(
    job_id: int,
    entity_name: str,
    jql_filter: str = "",
    project_key: str = "",
    start_date: str = "",
    end_date: str = "",
    record_limit: int | None = None,
) -> int:
    job_table_id = jobs_service.get_or_create_job_table(job_id, entity_name)
    logger.info("JSM extraction: entity=%s job_table=%d record_limit=%s", entity_name, job_table_id, record_limit)

    try:
        if entity_name == "users":
            return _extract_users(job_table_id, record_limit=record_limit)
        if entity_name == "organizations":
            return _extract_organizations(job_table_id, record_limit=record_limit)

        jql = jql_filter or ""
        if project_key:
            jql = f"project = {project_key}" + (f" AND {jql}" if jql else "")
        if start_date and _DATE_RE.match(start_date):
            jql += f" AND created >= '{start_date}'"
        if end_date and _DATE_RE.match(end_date):
            jql += f" AND created <= '{end_date}'"
        jql = jql.strip()
        if jql.startswith("AND "):
            jql = jql[4:]

        row_count = jira_client.get_issue_count(jql or "order by created")
        logger.info("JSM extraction: %s has %d issues (jql=%s)", entity_name, row_count, jql)

        already_migrated_offset = 0
        if record_limit and record_limit > 0:
            from services.internal.mappings_service import count_migrated_for_source_table
            already_migrated_offset = count_migrated_for_source_table(entity_name)
            effective_total = min(row_count - already_migrated_offset, record_limit)
            if effective_total <= 0:
                logger.info("All issues from %s already migrated (%d in id_mappings)", entity_name, already_migrated_offset)
                jobs_service.update_job_table_status(job_table_id, "success")
                return 0
            row_count = already_migrated_offset + effective_total
            logger.info(
                "Record limit active: extracting %d issues from %s (offset=%d, already_migrated=%d)",
                effective_total, entity_name, already_migrated_offset, already_migrated_offset,
            )

        batch_size = min(JSM_BATCH_SIZE, row_count) if row_count > 0 else JSM_BATCH_SIZE
        executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        futures = []
        batch_no = 0
        while already_migrated_offset + (batch_no * batch_size) < row_count:
            futures.append(
                executor.submit(
                    _extract_issue_batch,
                    job_table_id, batch_no, entity_name, jql,
                    already_migrated_offset + (batch_no * batch_size),
                    batch_size,
                )
            )
            batch_no += 1

        for future in tqdm.tqdm(
            as_completed(futures), total=len(futures),
            desc=f"Ext(jsm): {entity_name}", ncols=120,
        ):
            future.result()
        executor.shutdown(wait=False)

    except Exception as e:
        logger.error("JSM extraction failed for %s: %s", entity_name, e)
        jobs_service.update_job_table_status(job_table_id, "failed")
        return 0

    jobs_service.update_job_table_status(job_table_id, "success")
    return row_count


def _extract_issue_batch(
    job_table_id: int,
    batch_no: int,
    entity_name: str,
    jql: str,
    start_at: int,
    batch_size: int = JSM_BATCH_SIZE,
) -> None:
    job_table_batch_id = jobs_service.get_or_create_job_table_batch(
        job_table_id, batch_size, batch_no, "extraction",
    )
    csv_path = PATH_TO_DOWNLOADS / "jsm" / entity_name / f"{batch_size}_{batch_no}.csv"
    if jobs_service.is_job_table_batch_success(job_table_batch_id) and csv_path.exists():
        return

    logger.info("JSM ext batch start: entity=%s batch=%d startAt=%d", entity_name, batch_no, start_at)
    try:
        api_limit = min(batch_size, JSM_BATCH_SIZE)
        data = jira_client.fetch_issues(
            jql=jql,
            fields="summary,description,status,priority,issuetype,created,updated,"
                   "reporter,assignee,creator,resolutiondate,resolution,project,"
                   "labels,components,duedate",
            start_at=start_at,
            max_results=api_limit,
        )
        issues = data.get("issues", [])
        if not issues:
            jobs_service.update_job_table_batch_status(job_table_batch_id, "success")
            return

        records = [_flatten_issue(i) for i in issues]
        path_csv = (
            PATH_TO_DOWNLOADS / "jsm" / entity_name
            / f"{batch_size}_{batch_no}.csv"
        )
        save_as_csv(path_csv, records)
        jobs_service.update_job_table_batch_status(job_table_batch_id, "success")
    except Exception as e:
        logger.error("JSM ext batch failed: entity=%s batch=%d err=%s", entity_name, batch_no, e)
        jobs_service.update_job_table_batch_status(job_table_batch_id, "failed")


def _debug_log(msg: str) -> None:
    import sys
    with open("jsm_extract_debug.log", "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _extract_users(job_table_id: int, record_limit: int | None = None) -> int:
    batch_no = 0
    total = 0
    batch_size = 200
    already_migrated_offset = 0
    if record_limit and record_limit > 0:
        from services.internal.mappings_service import count_migrated_for_source_table
        already_migrated_offset = count_migrated_for_source_table("users")
        batch_size = min(batch_size, record_limit)
    _debug_log(f"[JSM_EXTRACT] _extract_users called, job_table_id={job_table_id}, record_limit={record_limit}, offset={already_migrated_offset}")
    while True:
        if record_limit and record_limit > 0 and total >= record_limit:
            break
        job_table_batch_id = jobs_service.get_or_create_job_table_batch(
            job_table_id, batch_size, batch_no, "extraction",
        )
        csv_path = PATH_TO_DOWNLOADS / "jsm" / "users" / f"{batch_size}_{batch_no}.csv"
        batch_cached = jobs_service.is_job_table_batch_success(job_table_batch_id)
        file_exists = csv_path.exists()
        _debug_log(f"[JSM_EXTRACT] batch={batch_no} batch_id={job_table_batch_id} cached={batch_cached} file_exists={file_exists} path={csv_path}")
        if batch_cached and file_exists:
            batch_no += 1
            total += batch_size
            continue
        try:
            _debug_log(f"[JSM_EXTRACT] Fetching batch {batch_no}, startAt={already_migrated_offset + batch_no * batch_size}")
            users = jira_client.fetch_users(
                start_at=already_migrated_offset + batch_no * batch_size, max_results=batch_size,
            )
            _debug_log(f"[JSM_EXTRACT] Got {len(users)} users")
            if not users:
                jobs_service.update_job_table_batch_status(job_table_batch_id, "success")
                break
            records = [_flatten_user(u) for u in users]
            path_csv = (
                PATH_TO_DOWNLOADS / "jsm" / "users"
                / f"{batch_size}_{batch_no}.csv"
            )
            _debug_log(f"[JSM_EXTRACT] Writing CSV to {path_csv}")
            save_as_csv(path_csv, records)
            _debug_log(f"[JSM_EXTRACT] CSV written, exists={path_csv.exists()}, size={path_csv.stat().st_size if path_csv.exists() else 0}")
            total += len(users)
            jobs_service.update_job_table_batch_status(job_table_batch_id, "success")
            if len(users) < batch_size:
                break
        except Exception as e:
            import traceback
            _debug_log(f"[JSM_EXTRACT] ERROR: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            jobs_service.update_job_table_batch_status(job_table_batch_id, "failed")
            break
        batch_no += 1

    jobs_service.update_job_table_status(job_table_id, "success")
    _debug_log(f"[JSM_EXTRACT] Done. Total={total}")
    return total


def _extract_organizations(job_table_id: int, record_limit: int | None = None) -> int:
    batch_no = 0
    total = 0
    batch_size = 50
    already_migrated_offset = 0
    if record_limit and record_limit > 0:
        from services.internal.mappings_service import count_migrated_for_source_table
        already_migrated_offset = count_migrated_for_source_table("organizations")
        batch_size = min(batch_size, record_limit)
    while True:
        if record_limit and record_limit > 0 and total >= record_limit:
            break
        job_table_batch_id = jobs_service.get_or_create_job_table_batch(
            job_table_id, batch_size, batch_no, "extraction",
        )
        csv_path = PATH_TO_DOWNLOADS / "jsm" / "organizations" / f"{batch_size}_{batch_no}.csv"
        if jobs_service.is_job_table_batch_success(job_table_batch_id) and csv_path.exists():
            batch_no += 1
            total += batch_size
            continue
        try:
            data = jira_client.fetch_organizations(
                start=already_migrated_offset + batch_no * batch_size, limit=batch_size,
            )
            orgs = data.get("values", [])
            if not orgs:
                jobs_service.update_job_table_batch_status(job_table_batch_id, "success")
                break
            records = [_flatten_organization(o) for o in orgs]
            path_csv = (
                PATH_TO_DOWNLOADS / "jsm" / "organizations"
                / f"{batch_size}_{batch_no}.csv"
            )
            save_as_csv(path_csv, records)
            total += len(orgs)
            jobs_service.update_job_table_batch_status(job_table_batch_id, "success")
            if data.get("isLastPage", True):
                break
        except Exception as e:
            logger.error("JSM org extraction batch %d failed: %s", batch_no, e)
            jobs_service.update_job_table_batch_status(job_table_batch_id, "failed")
            break
        batch_no += 1

    jobs_service.update_job_table_status(job_table_id, "success")
    logger.info("JSM extraction: extracted %d organizations", total)
    return total


# ── Attachment file downloads ──────────────────────────────────────────

_ATTACHMENT_WORKERS = 5
_ATTACHMENT_MAX_RETRIES = 2


def extract_jsm_attachment_files(job_id: int, table_mapping: dict) -> None:
    """Download attachment binaries for JSM issues.

    Unlike ServiceNow (which has a separate sys_attachment metadata table),
    Jira embeds attachments inside each issue's ``fields.attachment`` array.
    We iterate the already-extracted issue CSVs, fetch attachment metadata
    per issue, then download each file.
    """
    from services.internal.mappings_service import cfg_source_tables
    import csv

    job_table_id = jobs_service.get_or_create_job_table(job_id, "jsm_attachment_files")
    source_tables = cfg_source_tables(table_mapping)
    if not source_tables:
        return

    driving_entity = source_tables[0]
    entity_dir = PATH_TO_DOWNLOADS / "jsm" / driving_entity

    if not entity_dir.exists():
        logger.warning("JSM attachment extraction: no extracted data at %s", entity_dir)
        return

    all_issue_keys: list[str] = []
    for csv_file in entity_dir.glob("*.csv"):
        with open(csv_file, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                key = row.get("key", "")
                if key:
                    all_issue_keys.append(key)

    logger.info("JSM attachment extraction: %d issue keys to process", len(all_issue_keys))

    attachment_meta: list[dict] = []
    for key in all_issue_keys:
        try:
            data = jira_client.fetch_request_attachments(key, start=0, limit=100)
            for att in data.get("values", []):
                filename = att.get("filename", "unknown")
                att_id = str(att.get("id", ""))
                attachment_meta.append({
                    "id": att_id,
                    "filename": filename,
                    "issue_key": key,
                    "mimeType": att.get("mimeType", ""),
                    "size": att.get("size", 0),
                    "created": (att.get("created") or {}).get("iso8601", ""),
                    "author_accountid": (att.get("author") or {}).get("accountId", ""),
                })
        except Exception:
            pass

    if attachment_meta:
        meta_path = PATH_TO_DOWNLOADS / "jsm" / "attachment_meta" / "100_0.csv"
        save_as_csv(meta_path, attachment_meta)

    def _download_one(att: dict) -> bool:
        att_id = att["id"]
        fname = att["filename"]
        dest = PATH_TO_DOWNLOADS / "jsm" / "attachment_files" / f"{att_id}_{fname}"
        if dest.exists():
            return True
        for attempt in range(_ATTACHMENT_MAX_RETRIES + 1):
            try:
                resp = jira_client.download_attachment(att_id)
                file_manager.save_stream_to_file(resp, dest)
                return True
            except Exception as e:
                if attempt < _ATTACHMENT_MAX_RETRIES:
                    logger.warning("JSM attachment download retry %d for %s: %s", attempt + 1, att_id, e)
                else:
                    logger.error("JSM attachment download failed for %s: %s", att_id, e)
        return False

    with ThreadPoolExecutor(max_workers=_ATTACHMENT_WORKERS) as executor:
        futs = {executor.submit(_download_one, a): a for a in attachment_meta}
        ok = 0
        for future in as_completed(futs):
            try:
                if future.result():
                    ok += 1
            except Exception:
                pass
        logger.info("JSM attachment extraction: %d/%d downloaded", ok, len(attachment_meta))

    jobs_service.update_job_table_status(job_table_id, "success")
