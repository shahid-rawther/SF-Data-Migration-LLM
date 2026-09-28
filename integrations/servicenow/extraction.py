from datetime import datetime
from pathlib import Path

import tqdm
from common.logger import logger
from config.settings import (
    MAX_WORKERS,
    PATH_TO_DOWNLOADS,
    SERVICE_NOW_START_DATE,
    SERCICE_NOW_END_DATE,
    SERVICE_NOW_BATCH_SIZE,
    SERVICE_NOW_NO_OF_BATCH,
)
from services.internal import jobs_service
from integrations.servicenow.http_client import servicenow_client
from tools import file_manager
from tools.csv_manager import save_as_csv
from services.external import duckdb_client
from concurrent.futures import ThreadPoolExecutor, as_completed


def extract_servicenow_tables(
    job_id: int,
    service_now_tables: list,
    record_limit: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
):
    executer_queue = ThreadPoolExecutor(max_workers=5)
    futures = []
    for servicenow_table in service_now_tables:
        futures.append(
            executer_queue.submit(
                extract_servicenow_table, job_id, servicenow_table,
                record_limit=record_limit, start_date=start_date, end_date=end_date,
            )
        )

    total_rows = 0
    for future in as_completed(futures):
            total_rows += future.result() or 0

    executer_queue.shutdown(wait=True)
    return total_rows

def extract_servicenow_table(
    job_id: int,
    servicenow_table: str,
    record_limit: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
):
    executer = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    job_table_id = jobs_service.get_or_create_job_table(job_id, servicenow_table)
    logger.info(f"Job: {job_id}, Job Table: {job_table_id}")
    logger.info(
        f"Servicenow table '{servicenow_table}' extraction start at {datetime.now()}"
    )
    row_count = 0
    had_error = False
    try:

        # EC-26: prefer the per-session date window passed from the wizard;
        # fall back to the module-level settings (CLI/default path unchanged).
        start_date = start_date if start_date is not None else SERVICE_NOW_START_DATE
        end_date = end_date if end_date is not None else SERCICE_NOW_END_DATE

        row_count = servicenow_client.get_row_count(servicenow_table, start_date, end_date)
        logger.info(f"Total rows in {servicenow_table} is {row_count}")
        jobs_service.update_job_table_source_total(job_table_id, row_count)

        batch_size = SERVICE_NOW_BATCH_SIZE
        max_batches = SERVICE_NOW_NO_OF_BATCH
        already_migrated_offset = 0

        if record_limit and record_limit > 0:
            from services.internal.mappings_service import count_migrated_for_source_table
            already_migrated_offset = count_migrated_for_source_table(servicenow_table)
            effective_total = min(row_count - already_migrated_offset, record_limit)
            if effective_total <= 0:
                logger.info(f"All records from {servicenow_table} already migrated ({already_migrated_offset} in id_mappings)")
                jobs_service.update_job_table_status(job_table_id, "success")
                return 0
            row_count = already_migrated_offset + effective_total
            batch_size = record_limit
            max_batches = 1
            logger.info(
                f"Record limit active: extracting {effective_total} rows from {servicenow_table} "
                f"(offset={already_migrated_offset}, already_migrated={already_migrated_offset})"
            )

        futures = []

        batch_no = 0
        while True:

            offset = already_migrated_offset + (batch_no * batch_size)

            if offset >= row_count:
                break

            futures.append(
                executer.submit(
                    extract_single_batch,
                    job_table_id,
                    batch_no,
                    servicenow_table,
                    offset,
                    start_date,
                    end_date,
                    batch_size,
                )
            )

            batch_no += 1
            if max_batches and batch_no >= max_batches:
                break
        
        for future in tqdm.tqdm(as_completed(futures), total=len(futures), desc=f"Ext: {servicenow_table}", ncols=120):
            future.result()  
        
        logger.info(
            f"Servicenow table '{servicenow_table}' extraction ends at {datetime.now()}"
        )
    except Exception as e:
        had_error = True
        logger.error(
            f"Exception occured during fetching table: Job Table Id: {job_table_id}, Error: {e}"
        )
        jobs_service.update_job_table_status(job_table_id, "failed")
    # EC-19: only report success when extraction actually completed cleanly.
    if not had_error:
        jobs_service.update_job_table_status(job_table_id, "success")
    return row_count

def extract_single_batch(
        job_table_id: int, 
        batch_no: int,
        servicenow_table: str,
        offset: int,
        start_date:str = "",
        end_date:str = "",
        batch_size: int = SERVICE_NOW_BATCH_SIZE,

    ):
    job_table_batch_id = jobs_service.get_or_create_job_table_batch(
        job_table_id, batch_size, batch_no, "extraction"
    )

    try:
        logger.info(
            f"Start: { datetime.now()} Table: {servicenow_table}, Batch: {batch_no}, Job Batch Id: {job_table_batch_id}"
        )
        today = datetime.now().strftime("%Y-%m-%d")
        if not jobs_service.is_job_table_batch_success(job_table_batch_id):
            batch = []
            path_csv = (
                PATH_TO_DOWNLOADS
                / "servicenow"
                / f"{servicenow_table}"
                / f"{batch_size}_{batch_no}.csv"
            )

            batch = servicenow_client.fetch_table(
                servicenow_table,
                start_date,
                end_date,
                offset,
                batch_size,
            )
            if not batch:
                return

            save_as_csv(path_csv, batch)
            jobs_service.update_job_table_batch_status(
                job_table_batch_id, "success"
            )
        logger.info(
            f"End: {datetime.now()} Table: {servicenow_table}, Batch: {batch_no}, Job Batch Id: {job_table_batch_id}"
        )
    except Exception as e:
        logger.error(
            f"Exception occured during fetching table: {servicenow_table} and  batch {batch_no}, Error: {e}"
        )
        jobs_service.update_job_table_batch_status(job_table_batch_id, "failed")


def extract_servicenow_files(job_id: int, table_mapping: dict):
    job_table_id = jobs_service.get_or_create_job_table(job_id, "sys_attachment_files")
    load_batches = table_mapping.get("load_batches")
    executer = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    futures = []
    service_now_attachment_meta_table_path = (
        PATH_TO_DOWNLOADS / "servicenow" / "sys_attachment"
    )
    for batch_path in service_now_attachment_meta_table_path.iterdir():
        _, batch_number = batch_path.stem.split("_")
        if load_batches and not int(batch_number) in load_batches:
            continue
        futures.append(executer.submit(download_attachment_batch, job_table_id,batch_path, table_mapping))

    for future in tqdm.tqdm(as_completed(futures), total=len(futures), desc=f"Extraction Tables", ncols=120):
        try:
            future.result()  
        except Exception as e:
            logger.exception("Extraction batch failed", exc_info=e)
            jobs_service.update_job_table_status(job_table_id, "failed")
            raise
    jobs_service.update_job_table_status(job_table_id, "success")
    executer.shutdown(wait=False)

_ATTACHMENT_FILE_WORKERS = 5  # Concurrent file downloads per batch — keep below ServiceNow rate limit
_ATTACHMENT_MAX_RETRIES = 2   # Retry on transient connection errors


def _download_single_attachment(row: dict) -> bool:
    """Download one attachment file and save it. Returns True on success."""
    sys_id = row["sys_id"]
    file_name = row["file_name"]
    path = (
        PATH_TO_DOWNLOADS
        / "servicenow"
        / "sys_attachment_files"
        / f"{sys_id}_{file_name}"
    )
    if path.exists():
        return True
    for attempt in range(_ATTACHMENT_MAX_RETRIES + 1):
        try:
            attachment_response = servicenow_client.get_attachment_doc(sys_id)
            file_manager.save_stream_to_file(attachment_response, path)
            return True
        except Exception as e:
            if attempt < _ATTACHMENT_MAX_RETRIES:
                logger.warning(
                    f"Attachment download attempt {attempt + 1} failed for sys_id={sys_id}, retrying: {e}"
                )
            else:
                logger.error(f"File not found for sys id: {sys_id}", exc_info=e)
    return False


def download_attachment_batch(job_table_id:int, batch_path: Path, table_mapping: dict):
    batch, batch_number = batch_path.stem.split("_")
    job_table_batch_id = jobs_service.get_or_create_job_table_batch(job_table_id,batch, batch_number, "extraction-file")
    if jobs_service.is_job_table_batch_success(job_table_batch_id):
        return
    fields = [
        "sys_attachment.sys_id",
        "sys_attachment.file_name",
        "sys_attachment.size_bytes",
        "sys_attachment.table_name",
        "sys_attachment.table_sys_id",
    ]
    filters = table_mapping.get("filters", [])
    attachment_meta_data = duckdb_client.fetch_all(
        base_table=batch_path, fields=fields, tables=[], joins=[], filters=filters
    )
    rows = attachment_meta_data.to_dict(orient='records')

    records_found = 0
    with ThreadPoolExecutor(max_workers=_ATTACHMENT_FILE_WORKERS) as file_executor:
        file_futures = {
            file_executor.submit(_download_single_attachment, row): row
            for row in rows
        }
        for future in as_completed(file_futures):
            try:
                if future.result():
                    records_found += 1
            except Exception as e:
                logger.exception("Unexpected error downloading attachment", exc_info=e)

    logger.info(f"Total records extracted for batch number {batch_number} is {records_found}")
    jobs_service.update_job_table_batch_status(job_table_batch_id,"success")
