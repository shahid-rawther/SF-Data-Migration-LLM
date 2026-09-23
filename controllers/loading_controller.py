import csv
import io
import random
import time
from pathlib import Path


import tqdm
from common.logger import logger
from services.internal.mappings_service import (
    get_mappings,
    insert_id_mappings,
    map_servicenow_to_salesforce,
    get_id_mapping_table_name,
    cfg_source_tables,
    cfg_destination_object,
    cfg_external_id_field,
)
import services.internal.jobs_service as jobs_service
from integrations.salesforce.http_client import salesforce_client
from config.settings import (
    DISABLE_LOAD,
    HASH_KEY_NAME,
    MAX_WORKERS,
    PATH_TO_TRANSFORMATION,
    PATH_TO_RESULTS,
    POLL_TIME,
    SERVICE_NOW_ID_COLUMN,
    SALESFORCE_ID_COLUMN,
    SALESFORCE_CREATED_AT_COLUMN,
)
from tools.file_manager import save_text_to_file, read_text_from_file
from tools.csv_manager import convert_to_salesforce_hash, convert_to_servicenow_hash, read_csv_as_dictionary, save_as_csv
from helpers.hash_helper import create_hash_for_row
from concurrent.futures import ThreadPoolExecutor, as_completed
from integrations.salesforce import loading as loading_service
from typing import List, Dict


def _summarize_failed_results(failed_csv: str, max_items: int = 8) -> list[str]:
    """Extract unique Salesforce error messages from failedResults CSV text."""
    if not failed_csv or not failed_csv.strip():
        return []

    messages: list[str] = []
    seen: set[str] = set()
    try:
        reader = csv.DictReader(io.StringIO(failed_csv))
        for row in reader:
            msg = (row.get("sf__Error") or "").strip()
            if not msg:
                continue
            if msg in seen:
                continue
            seen.add(msg)
            messages.append(msg)
            if len(messages) >= max_items:
                break
    except Exception:
        return []
    return messages


def start_loading(job_id: int, mappings_file_names: list[str] = []):
    job_id = jobs_service.get_or_create_job(job_id, "loading")
    jobs_service.update_job_status(job_id, "in-progress")
    total_processed = 0
    total_failed = 0
    by_object: dict[str, dict[str, int]] = {}
    by_object_errors: dict[str, list[str]] = {}
    
    try:
        tables_mappings = get_mappings(mappings_file_names)
        for table_mapping in tables_mappings:
            processed, failed, error_messages = load_table(job_id, table_mapping)
            total_processed += processed
            total_failed += failed
            obj_name = table_mapping.get("transformation_name") or cfg_destination_object(table_mapping)
            by_object[obj_name] = {"processed": processed, "failed": failed}
            if error_messages:
                by_object_errors[obj_name] = error_messages
    except Exception as e:
        jobs_service.update_job_status(job_id, "failed")
        raise
    jobs_service.update_job_status(job_id, "success")
    return {
        "records_processed": total_processed,
        "records_failed": total_failed,
        "by_object": by_object,
        "errors_by_object": by_object_errors,
    }


def load_csv_batch(
        executer:ThreadPoolExecutor,
        batch_path: Path,
        batch_numbers: list[int],
        job_table: str,
        operation: str,
        job_table_id: int,
        table_mapping: dict
    ):
    futures = []
    insert_path = max(
        batch_path.glob("*_insert.csv"),
        key=lambda p: p.stat().st_mtime,
        default=None
    )

    update_path = max(
        batch_path.glob("*_update.csv"),
        key=lambda p: p.stat().st_mtime,
        default=None
    )
    batch_size, batch_number = map(int, batch_path.stem.split("_"))

    if batch_numbers and batch_number not in batch_numbers:
        logger.info(
            f"Loading: {job_table}: Batch Number: {batch_number} skipped as per configuration"
        )
        return futures


    if operation == "insert" and insert_path:
        if insert_path.exists():
            table_batch_insert_id = jobs_service.get_or_create_job_table_batch(
                job_table_id, batch_size, batch_number, "insert"
            )
            futures.append(executer.submit(
                load_insert_batch,
                job_table_id,
                insert_path,
                batch_number,
                table_batch_insert_id,
                table_mapping,
            ))
    if operation == "update" and update_path:
        if update_path.exists():
            table_batch_update_id = jobs_service.get_or_create_job_table_batch(
                job_table_id, batch_size, batch_number, "update"
            )
            futures.append(executer.submit(
                load_update_batch,
                job_table_id,
                update_path,
                batch_number,
                table_batch_update_id,
                table_mapping,
            ))
    if operation == "upsert":
        if insert_path is not None and insert_path.exists():
            table_batch_insert_id = jobs_service.get_or_create_job_table_batch(
                job_table_id, batch_size, batch_number, "insert"
            )
            if insert_path:
                futures.append(
                    executer.submit(
                        load_insert_batch,
                        job_table_id,
                        insert_path,
                        batch_number,
                        table_batch_insert_id,
                        table_mapping,
                    )
                )
        if update_path is not None and update_path.exists():
            table_batch_update_id = jobs_service.get_or_create_job_table_batch(
                job_table_id, batch_size, batch_number, "update"
            )
            if update_path:
                futures.append(
                    executer.submit(
                        load_update_batch,
                        job_table_id,
                        update_path,
                        batch_number,
                        table_batch_update_id,
                        table_mapping,
                    )
                )
    return futures


def load_attachment_batch(
        batch_path: Path,
        batch_numbers: list[int],
        job_table: str,
        operation: str,
        job_table_id: int,
        table_mapping: dict
    ):
    insert_path = max(
        batch_path.glob("*_insert.csv"),
        key=lambda p: p.stat().st_mtime,
        default=None
    )

    _, batch_number = map(int, batch_path.stem.split("_"))

    if batch_numbers and batch_number not in batch_numbers:
        logger.info(
            f"Loading: {job_table}: Batch Number: {batch_number} skipped as per configuration"
        )
        return {"processed": 0, "failed": 0}

    if operation == "insert" and insert_path:
        return loading_service.load_insert_attachments(
            insert_path,
            job_table_id,
            job_table,
            table_mapping
        ) or {"processed": 0, "failed": 0}
    return {"processed": 0, "failed": 0}



def load_table(job_id: int, table_mapping: dict):
    executer_parallel = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    executer_queue = ThreadPoolExecutor(max_workers=1)
    table_name = cfg_source_tables(table_mapping)[0]  # Driving table
    salesforce_object = cfg_destination_object(table_mapping)
    transformation_name = table_mapping.get("transformation_name")
    use_queue = table_mapping.get("use_queue")
    batch_numbers = table_mapping.get("load_batches")
    is_attachment_table = table_mapping.get("is_attachment_table")
    futures = []
    total_processed = 0
    total_failed = 0
    collected_errors: list[str] = []
    transformation_path = (
        PATH_TO_TRANSFORMATION
        / "csv"
        / (transformation_name if transformation_name else salesforce_object)
    )
    operation = table_mapping["operation"]
    job_table = get_id_mapping_table_name(
        table_name,
        salesforce_object,
        table_mapping.get("transformation_name"),
    )
    logger.info(f"Loading: {job_table}...")
    
    job_table_id = jobs_service.create_job_table(job_id, job_table)

    executer = executer_queue if use_queue else executer_parallel
    if Path(transformation_path).exists():
        batch_dirs = list(transformation_path.iterdir())
        logger.info(f"Loading: {job_table} | transformation_path={transformation_path} | batches={len(batch_dirs)}")
        for batch_path in batch_dirs:
            if not is_attachment_table:
                futures.extend(
                    load_csv_batch(
                        executer,
                        batch_path,
                        batch_numbers,
                        job_table,
                        operation,
                        job_table_id,
                        table_mapping
                    )
                )
            else:
                # For attachment the executer has to be a queue executer
                futures.append(
                    executer_queue.submit(
                            load_attachment_batch,
                            batch_path,
                            batch_numbers,
                            job_table,
                            operation,
                            job_table_id,
                            table_mapping
                    )
                )
        
    else:
        logger.warning(f"Loading: {job_table} | transformation_path NOT FOUND: {transformation_path}")

    for future in tqdm.tqdm(
        as_completed(futures),
        total=len(futures),
        desc=f"Loading: {job_table}",
        ncols=120,
    ):
        try:
            result = future.result()
            if isinstance(result, dict):
                total_processed += result.get("processed", 0)
                total_failed += result.get("failed", 0)
                for err in result.get("errors", []):
                    if err not in collected_errors:
                        collected_errors.append(err)
        except Exception as e:
            logger.exception("Loading batch failed", exc_info=e)
            jobs_service.update_job_table_status(job_table_id, "failed")
            raise
    jobs_service.update_job_table_status(job_table_id, "success")
    executer_parallel.shutdown(wait=False)
    executer_queue.shutdown(wait=False)
    return total_processed, total_failed, collected_errors


def load_insert_batch(
    job_table_id:int,
    batch_path: Path,
    batch_number: int,
    table_batch_id: int,
    table_mapping: dict
):
    batch_size_str = ""

    batch_size_str, _ = batch_path.parent.stem.split("_")
    batch_insert_size = int(batch_size_str)
    logger.info(
        f"Loading: Insert: Batch Number: {batch_number}, Insert Batch Size: {batch_insert_size}, Status: Pending"
    )
    if jobs_service.is_job_table_batch_success(table_batch_id):
        logger.info(
            f"Loading: Insert: Batch Number: {batch_number} Insert Batch Size: {batch_insert_size}, Status: Skipped"
        )
        return {"processed": 0, "failed": 0}

    try:
        jobs_service.update_job_table_batch_status(table_batch_id, "in-progress")
        result = load_batch(job_table_id, table_batch_id, batch_path, "insert", table_mapping)
        fully_migrated = isinstance(result, dict) and result.get("failed", 0) == 0 and result.get("processed", 0) > 0
        if fully_migrated:
            jobs_service.update_job_table_batch_status(table_batch_id, "success")
            logger.info(
                f"Loading: Insert: Batch Number: {batch_number}, Insert Batch Size: {batch_insert_size}, Status: Success"
            )
        else:
            jobs_service.update_job_table_batch_status(table_batch_id, "partial-success")
            logger.info(
                f"Loading: Insert: Batch Number: {batch_number}, Insert Batch Size: {batch_insert_size}, "
                f"Failed Records: {result.get('failed', 0) if isinstance(result, dict) else 'unknown'}, Status: Partial Success"
            )
        return result or {"processed": 0, "failed": 0}

    except Exception as e:
        jobs_service.update_job_table_batch_status(table_batch_id, "failed")
        logger.error(
            f"Loading: Insert: Batch: {batch_number}, Insert Batch Size: {batch_insert_size}, Status: Failed"
        )
        logger.exception(str(e))
        raise


def load_update_batch(
    job_table_id:int,
    batch_path: Path,
    batch_number: int,
    table_batch_id: int,
    table_mapping: dict,
):

    batch_size_str, _ = batch_path.parent.stem.split("_")
    batch_update_size = int(batch_size_str)
    logger.info(
        f"Loading: Update: Batch: {batch_number}, Update Batch Size: {batch_update_size}, Status: Pending"
    )
    if jobs_service.is_job_table_batch_success(table_batch_id):
        logger.info(
            f"Loading: Update: Batch: {batch_number}, Update Batch Size: {batch_update_size},  Status: Skipped"
        )
        return {"processed": 0, "failed": 0}

    try:
        jobs_service.update_job_table_batch_status(table_batch_id, "in-progress")
        result = load_batch(job_table_id, table_batch_id, batch_path, "update", table_mapping)
        full_migrated = isinstance(result, dict) and result.get("failed", 0) == 0 and result.get("processed", 0) > 0
        if full_migrated:
            jobs_service.update_job_table_batch_status(table_batch_id, "success")
            logger.info(
                f"Loading: Update: Batch Number: {batch_number}, Insert Batch Size: {batch_update_size}, Status: Success"
            )

        else:
            jobs_service.update_job_table_batch_status(table_batch_id, "partial-success")
            logger.info(
                f"Loading: Update: Batch Number: {batch_number}, Insert Batch Size: {batch_update_size}, Status: Partial Success"
            )
        return result or {"processed": 0, "failed": 0}
        
    except Exception as e:
        jobs_service.update_job_table_batch_status(table_batch_id, "failed")
        logger.exception(f"Loading: Update: Batch: {batch_number}, Update Batch Size: {batch_update_size}, Status: Failed", exc_info=e)
        raise


def _match_id_mappings_by_recomputed_hash(
    transformation_csv_path: Path,
    servicenow_hash_path: Path,
    salesforce_hash: List[Dict[str, str]],
    success_data: List[Dict],
    picklist_values: dict | None,
    force_values: dict | None,
) -> List[Dict[str, str]]:
    """Fallback ID-mapping strategy when the pre-computed hash CSV doesn't match.

    The mismatch happens because the transformation hash is computed from ALL
    fields, but ``upload_csv`` removes non-existent Salesforce fields (e.g.
    audit fields) before sending.  Here we recompute ServiceNow hashes using
    only the fields that appear in the Salesforce success response, applying
    the same picklist-blanking and force-value overrides so the hashes align.

    The transformation CSV contains only Salesforce fields (no sys_id).
    The hash CSV contains sys_id + hash_key, in the same row order.
    We merge them by position to recover sys_id for each record.
    """
    if not success_data or not salesforce_hash:
        return []

    sf_fields = set(success_data[0].keys())

    sf_hash_lookup: Dict[str, str] = {
        entry[HASH_KEY_NAME]: entry[SALESFORCE_ID_COLUMN]
        for entry in salesforce_hash
        if entry.get(HASH_KEY_NAME) and entry.get(SALESFORCE_ID_COLUMN)
    }

    hash_rows: List[Dict[str, str]] = []
    if servicenow_hash_path.exists():
        with open(servicenow_hash_path, newline="", encoding="utf-8") as hf:
            hash_rows = list(csv.DictReader(hf))

    with open(transformation_csv_path, newline="", encoding="utf-8") as f:
        transformation_rows = list(csv.DictReader(f))

    # The fallback recovers sys_id for each transformation row by POSITION in the
    # hash CSV. That is only safe when both files describe exactly the same rows
    # in the same order. If the counts differ, the files are misaligned and any
    # positional pairing would silently link a sys_id to the wrong Salesforce Id
    # (corrupting child lookups). Abort loudly instead of guessing.
    if len(hash_rows) != len(transformation_rows):
        logger.error(
            "ID-mapping fallback aborted: transformation rows (%d) and hash rows (%d) "
            "are misaligned; refusing positional sys_id pairing.",
            len(transformation_rows), len(hash_rows),
        )
        return []

    mappings: List[Dict[str, str]] = []
    for idx, row in enumerate(transformation_rows):
        sys_id = hash_rows[idx].get(SERVICE_NOW_ID_COLUMN, "")
        if not sys_id:
            continue

        filtered = {k: row[k] for k in sf_fields if k in row}

        if picklist_values:
            for field_name, allowed in picklist_values.items():
                if field_name in filtered and filtered[field_name] and filtered[field_name] not in allowed:
                    filtered[field_name] = ""
        if force_values:
            for field_name, val in force_values.items():
                if field_name in filtered:
                    filtered[field_name] = val

        h = create_hash_for_row(filtered)
        sf_id = sf_hash_lookup.get(h)
        if sf_id:
            mappings.append({
                SERVICE_NOW_ID_COLUMN: sys_id,
                SALESFORCE_ID_COLUMN: sf_id,
                HASH_KEY_NAME: h,
            })
    return mappings


def load_batch(job_table_id:int, table_batch_id: int, batch_path: Path, operation: str, table_mappings: dict):
    if DISABLE_LOAD:
        return {"processed": 0, "failed": 0}
    salesforce_object = cfg_destination_object(table_mappings)
    transformation_name = table_mappings.get("transformation_name")
    service_now_driving_table = cfg_source_tables(table_mappings)[0]
    id_mapping_table_name = get_id_mapping_table_name(
        service_now_driving_table, salesforce_object, transformation_name
    )
    batch_size = int(batch_path.stem.split("_")[0])

    # Determine if we need to force field values on insert (e.g. Status="New" for Incident)
    insert_force_values = None
    if operation == "insert":
        insert_force_values = _get_insert_force_values(salesforce_object)

    # EC-03: If the mapping declares an external-id field, perform inserts as an
    # upsert keyed on that field so re-runs (even after a local DB reset) update
    # the existing Salesforce record instead of creating a duplicate. The logical
    # `operation` is preserved for the id-mapping/hash bookkeeping below.
    external_id_field = cfg_external_id_field(table_mappings)
    sf_operation = operation
    if operation == "insert" and external_id_field:
        sf_operation = "upsert"
        logger.info(
            f"[{salesforce_object}] Using external-id upsert on '{external_id_field}' for idempotent insert"
        )

    # Create a Job
    job = salesforce_client.create_job(
        salesforce_object,
        sf_operation,
        external_id_field=external_id_field if sf_operation == "upsert" else None,
    )
    job_id = job["id"]
    jobs_service.update_job_table_batch_salesforce_job_id(table_batch_id, job_id)

    # Fetch valid Salesforce fields to filter out non-existent columns
    try:
        valid_fields = salesforce_client.describe_object(salesforce_object)
        picklist_values = salesforce_client.get_picklist_values(salesforce_object)
    except Exception as e:
        logger.warning(f"Could not describe '{salesforce_object}', skipping field validation: {e}")
        valid_fields = None
        picklist_values = None

    salesforce_client.upload_csv(job_id, batch_path, valid_fields=valid_fields, picklist_values=picklist_values, force_values=insert_force_values)
    salesforce_client.mark_upload_complete(job_id)

    terminal_states = {"JobComplete", "Failed", "Aborted"}
    job_satus = None
    while True:
        job_satus = salesforce_client.get_job(job_id)
        state = job_satus.get("state")
        if state == "Failed":
            logger.info(job_satus)
        logger.info(
            f"[{salesforce_object}] State: {state}, Processed Records: {job_satus.get('numberRecordsProcessed')}, Failed Records:{job_satus.get('numberRecordsFailed')}"
        )
        if state in terminal_states:
            break
        time.sleep(POLL_TIME)  # backoff as needed

    batch_result = {"processed": 0, "failed": 0, "errors": []}
    if job_satus:
        processed_records = int(job_satus.get("numberRecordsProcessed") or 0)
        failed_records = int(job_satus.get("numberRecordsFailed") or 0)
        succeeded_records = max(processed_records - failed_records, 0)
        unprocessed_records = batch_size - processed_records
        results = {
            "processed_records": processed_records,
            "failed_records": failed_records,
            "unprocessed_records": unprocessed_records
        }
        jobs_service.update_job_table_batch_results(table_batch_id, results)
        batch_result = {"processed": succeeded_records, "failed": failed_records, "errors": []}

        
    success_path = (
        PATH_TO_RESULTS
        / (transformation_name if transformation_name else salesforce_object)
        / str(job_table_id)
        / batch_path.parent.stem
        / batch_path.stem
        / "success.csv"
    )
    failed_path = (
        PATH_TO_RESULTS
        / (transformation_name if transformation_name else salesforce_object)
        / str(job_table_id)
        / batch_path.parent.stem
        / batch_path.stem
        / "failed.csv"
    )
    unprocessed_path = (
        PATH_TO_RESULTS
        / (transformation_name if transformation_name else salesforce_object)
        / str(job_table_id)
        / batch_path.parent.stem
        / batch_path.stem
        / "unprocessed.csv"
    )
        
    

    success = salesforce_client.get_results(job_id, "successfulResults")
    save_text_to_file(success, success_path)

    success_data = read_csv_as_dictionary(success)

    if operation == "insert":
        salesforce_hash = convert_to_salesforce_hash(success_data)
        servicenow_hash_path = (
            PATH_TO_TRANSFORMATION
            / "hash"
            / (transformation_name if transformation_name else salesforce_object)
            / batch_path.parent.stem
            / f"{batch_path.stem}_hash.csv"
        )

        if not salesforce_hash:
            logger.warning(
                "No successful Salesforce inserts for %s batch %s; skipping ID mapping generation",
                salesforce_object,
                batch_path.stem,
            )
            id_mappings = []
        else:
            id_mappings = map_servicenow_to_salesforce(
                servicenow_hash_path, salesforce_hash
            )

        # Every record the destination accepted should end up with an id mapping.
        # Where fewer matched, the rest are rows the loader altered before upload
        # — a blanked restricted picklist value, a forced insert value — so the
        # hash computed at transform time no longer describes what was sent. The
        # fallback recomputes those hashes the same way. It runs whenever any row
        # is unmatched rather than only when all of them are: a partial match
        # used to leave the remainder unmapped, which silently breaks every child
        # lookup and backfill that needs those ids.
        if salesforce_hash and len(id_mappings) < len(success_data):
            recovered = _match_id_mappings_by_recomputed_hash(
                batch_path, servicenow_hash_path, salesforce_hash, success_data,
                picklist_values, insert_force_values,
            )
            # The primary pass matched on an untouched hash, so where the two
            # disagree it is the one to trust; the fallback only fills gaps.
            seen_sys_ids = {entry[SERVICE_NOW_ID_COLUMN] for entry in id_mappings}
            claimed_sf_ids = {entry[SALESFORCE_ID_COLUMN] for entry in id_mappings}
            added = 0
            for entry in recovered:
                if entry[SERVICE_NOW_ID_COLUMN] in seen_sys_ids:
                    continue
                # One destination record cannot stand for two source records.
                if entry[SALESFORCE_ID_COLUMN] in claimed_sf_ids:
                    continue
                id_mappings.append(entry)
                seen_sys_ids.add(entry[SERVICE_NOW_ID_COLUMN])
                claimed_sf_ids.add(entry[SALESFORCE_ID_COLUMN])
                added += 1
            if added:
                logger.info(
                    "Recovered %d additional ID mapping(s) via recomputed-hash "
                    "fallback for %s (%d of %d loaded record(s) now mapped)",
                    added, id_mapping_table_name, len(id_mappings), len(success_data),
                )

        if len(id_mappings) < len(success_data):
            logger.error(
                "%s: %d of %d loaded record(s) could not be mapped back to a "
                "source id. Children referencing them cannot be linked.",
                id_mapping_table_name, len(success_data) - len(id_mappings),
                len(success_data),
            )

        insert_id_mappings(id_mapping_table_name, id_mappings)

        # Post-insert update: restore real values for fields that were forced during insert
        if insert_force_values and success_data:
            _post_insert_update(
                salesforce_object, batch_path, success_data, insert_force_values, valid_fields,
                # Pairs the restore on the source's own key rather than on the
                # order the results came back in.
                match_field=external_id_field,
            )

    failed = salesforce_client.get_results(job_id, "failedResults")
    save_text_to_file(failed, failed_path)
    batch_result["errors"] = _summarize_failed_results(failed)

    unprocessed = salesforce_client.get_results(job_id, "unprocessedRecords")
    save_text_to_file(unprocessed, unprocessed_path)

    return batch_result


# ---------------------------------------------------------------------------
# Two-pass insert: force safe values on insert, then update with real values
# ---------------------------------------------------------------------------

# Map of Salesforce object → fields to force during insert and their forced values
_INSERT_FORCE_CONFIG = {
    "Incident": {"Status": "New"},
}


def _get_insert_force_values(salesforce_object: str) -> dict | None:
    """Return the force_values dict for a given SF object, or None if not configured."""
    return _INSERT_FORCE_CONFIG.get(salesforce_object)


def build_post_insert_update_csv(
    original_csv_path: Path,
    success_data: list[dict],
    force_values: dict,
    match_field: str = "",
) -> tuple[str, int]:
    """The CSV restoring real values to records inserted with forced ones.

    Returns ``(csv_content, row_count)``; a count of zero means every record
    genuinely wanted the forced value and there is nothing to send.

    Rows are paired on ``match_field`` — the mapping's external id — because
    Salesforce does not promise that the bulk result echoes rows in the order
    they were uploaded. Pairing them by position, which is what this did before,
    is silently wrong the first time that order differs: each record is then
    assigned another record's value, and nothing downstream can detect it.
    Position is still the fallback where no external id is declared, since a
    misordered restore is no worse than none, but it is warned about.

    Split out from the job it feeds so the decision can be inspected without
    sending anything to the destination.
    """
    forced_fields = list(force_values.keys())
    with open(original_csv_path, newline="", encoding="utf-8") as f:
        source_rows = list(csv.DictReader(f))

    paired: list[tuple[str, dict]] = []
    if match_field and all(row.get(match_field) for row in source_rows):
        by_key = {
            record[match_field]: record
            for record in success_data
            if record.get(match_field) and record.get("sf__Id")
        }
        for row in source_rows:
            record = by_key.get(row.get(match_field, ""))
            if record:
                paired.append((record["sf__Id"], row))
    else:
        if match_field:
            logger.warning(
                "Cannot pair post-insert updates on '%s' — not every row carries it; "
                "falling back to upload order",
                match_field,
            )
        for idx, record in enumerate(success_data):
            if record.get("sf__Id") and idx < len(source_rows):
                paired.append((record["sf__Id"], source_rows[idx]))

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["Id"] + forced_fields)
    writer.writeheader()
    written = 0
    for sf_id, source_row in paired:
        values = {field: source_row.get(field, "") for field in forced_fields}
        # A record whose real value is the forced one needs no update.
        if all(values[field] == force_values[field] for field in forced_fields):
            continue
        writer.writerow({"Id": sf_id, **values})
        written += 1

    return output.getvalue(), written


def _post_insert_update(
    salesforce_object: str,
    original_csv_path: Path,
    success_data: list[dict],
    force_values: dict,
    valid_fields: set | None,
    match_field: str = "",
):
    """Restore the real values of fields that were forced during insert."""
    if not success_data:
        return

    forced_fields = list(force_values.keys())
    logger.info(
        f"[{salesforce_object}] Post-insert update: restoring {forced_fields} for {len(success_data)} records"
    )

    csv_content, row_count = build_post_insert_update_csv(
        original_csv_path, success_data, force_values, match_field
    )
    if not row_count:
        logger.info(
            f"[{salesforce_object}] No {'/'.join(forced_fields)} updates needed — "
            f"every record's real value is the forced one"
        )
        return
    logger.info(
        f"[{salesforce_object}] Restoring {'/'.join(forced_fields)} on {row_count} "
        f"of {len(success_data)} record(s)"
    )

    # Create and run update job
    try:
        update_job = salesforce_client.create_job(salesforce_object, "update")
        update_job_id = update_job["id"]
        salesforce_client.upload_csv_data(update_job_id, csv_content)
        salesforce_client.mark_upload_complete(update_job_id)

        # Poll for completion
        terminal_states = {"JobComplete", "Failed", "Aborted"}
        while True:
            job_status = salesforce_client.get_job(update_job_id)
            state = job_status.get("state")
            if state in terminal_states:
                break
            time.sleep(POLL_TIME)

        processed = job_status.get("numberRecordsProcessed", 0)
        failed = job_status.get("numberRecordsFailed", 0)
        logger.info(
            f"[{salesforce_object}] Post-insert status update complete: {processed} processed, {failed} failed"
        )
    except Exception as e:
        logger.error(f"[{salesforce_object}] Post-insert status update failed: {e}")


def save_id_mappings():
    from tools.file_manager import read_text_from_file

    success = read_text_from_file(PATH_TO_RESULTS / "ParentIncident/5000_0/1300_insert/success.csv")

    success_data = read_csv_as_dictionary(success)

    salesforce_hash = convert_to_salesforce_hash(success_data)

    servicenow_hash_path = (
        PATH_TO_TRANSFORMATION / "hash" / "ParentIncident/5000_0/1300_insert_hash.csv"
    )
    id_mappings = map_servicenow_to_salesforce(servicenow_hash_path, salesforce_hash)
    id_mapping_table_name = "cmdb_ci_service_Service__c_"
    insert_id_mappings(id_mapping_table_name, id_mappings)


def insert_missing_id_mappings():
    id_mapping_table_name = "incident_Incident_ParentIncident"
    transformation_name = "ParentIncident"
    from helpers.transformation_helper import look_up_incident_sys_id
    results = PATH_TO_RESULTS / transformation_name
    transformation_hash = PATH_TO_TRANSFORMATION / f"hash/{transformation_name}"
    batch = "5000"
    total_records = 0
    missing_mappings = []
    for i in range(32):
        batch_name = batch + "_" + str(i)
        batch_path = results / batch_name
        insert_path = next(batch_path.glob("*_insert"), None)
        success = insert_path / "success.csv"
        total_records = total_records + int(insert_path.stem.split("_")[0])
        
        success_data = read_csv_as_dictionary(success)

        salesforce_hash = convert_to_salesforce_hash(success_data)

        servicenow_hash_path = (
            transformation_hash / f"{batch_name}/{insert_path.stem}_hash.csv"
        )
        mappings = map_servicenow_to_salesforce(servicenow_hash_path, salesforce_hash)
        
        for mapping in mappings:
            salesforce_id = look_up_incident_sys_id("test", mapping["sys_id"])
            if not salesforce_id["test"]:
                missing_mappings.append(mapping)
                logger.info("Batch: %s Missing record for %s. Found mapping: %s", batch_name,mapping["sys_id"], mapping["sf__Id"])

    
        insert_id_mappings(id_mapping_table_name, missing_mappings)


    
def start_attachment_loading(job_id: int, mappings_file_names: list[str] = []):
    job_id = jobs_service.get_or_create_job(job_id, "loading")
    jobs_service.update_job_status(job_id, "in-progress")
    
    try:
        tables_mappings = get_mappings(mappings_file_names)
        for table_mapping in tables_mappings:
            load_table(job_id, table_mapping)
    except Exception as e:
        jobs_service.update_job_status(job_id, "failed")
        raise
    jobs_service.update_job_status(job_id, "success")
