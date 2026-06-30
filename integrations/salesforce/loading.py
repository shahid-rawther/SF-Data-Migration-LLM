
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv

import json
from datetime import datetime
from pathlib import Path
from typing import List
from config.settings import LOADING_BATCH_SIZE, MAX_WORKERS, PATH_TO_RESULTS, SALESFORCE_ID_COLUMN, SERVICE_NOW_ID_COLUMN
from integrations.salesforce.http_client import salesforce_client
from services.internal import jobs_service, mappings_service
from common.logger import logger
from tools.file_manager import  save_json_to_file

def load_insert_attachments(
        insert_path: Path,
        job_table_id: int,
        id_mapping_table_name: str,
        table_mapping:dict,
):
    """
    Reads a CSV containing:
    Title, PathOnClient, VersionData, FirstPublishLocationId

    Uploads ContentVersion records in batches of 25
    using Salesforce Composite API.

    Returns ``{"processed": int, "failed": int}``.
    """

    futures = []

    executer = ThreadPoolExecutor(max_workers=MAX_WORKERS)

    batch: List[dict] = []

    batch_size, batch_number = insert_path.parent.stem.split("_")

    job_table_batch_id = jobs_service.get_or_create_job_table_batch(job_table_id, batch_size,batch_number)

    total_processed = 0
    total_failed = 0

    try:
        with open(insert_path, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            sub_batch_number = 0
            for row in reader:
                batch.append(row)

                if len(batch) == LOADING_BATCH_SIZE:
                    logger.info(f"Attchment sub batch: {sub_batch_number} added to thread pool")
                    futures.append(executer.submit(
                        _post_attachment, 
                        insert_path,
                        job_table_batch_id,
                        batch.copy(), 
                        sub_batch_number,
                        id_mapping_table_name,
                        table_mapping
                        )
                    )

                    batch.clear()
                    sub_batch_number += 1

            # upload remaining
            if batch:
                logger.info(f"Attchment final sub batch: {sub_batch_number} added to thread pool")
                futures.append(executer.submit(
                    _post_attachment,
                    insert_path, 
                    job_table_batch_id,
                    batch.copy(), 
                    sub_batch_number, 
                    id_mapping_table_name,
                    table_mapping
                ))
        
        for future in as_completed(futures):
            result = future.result()
            if isinstance(result, dict):
                total_processed += result.get("processed", 0)
                total_failed += result.get("failed", 0)
    except Exception as e:
        logger.error(f"Batch failed: Batch number: {batch_number}")
        jobs_service.update_job_table_batch_status(job_table_batch_id, "failed")
        return {"processed": total_processed, "failed": total_failed}
    finally:
        executer.shutdown(wait=False)

    jobs_service.update_job_table_batch_status(job_table_batch_id, "success")
    return {"processed": total_processed, "failed": total_failed}


def create_id_mapping(response: list[dict], batch: list[dict]):
    """
    Creates ID mapping records:
    {
        "sys_id": <source system id>,
        "sf__id": <Salesforce ContentVersion Id>
    }
    """

    id_mappings = []

    for idx, result in enumerate(response):
        if not result.get("success"):
            # Skip or log failures if needed
            continue

        source_record = batch[idx]

        id_mappings.append(
            {
                SERVICE_NOW_ID_COLUMN: source_record["sys_id"],
                SALESFORCE_ID_COLUMN: result["id"],
            }
        )

    return id_mappings

def _post_attachment(
        insert_path:Path, 
        job_table_batch_id: int,
        batch: list[dict], 
        sub_batch_number: int, 
        id_mapping_table_name: str,
        table_mapping:dict
    ):
    original_count = len(batch)
    try:
        folder = (table_mapping.get("transformation_name") if table_mapping.get("transformation_name") else table_mapping.get("salesforce_object"))
        path_to_result = PATH_TO_RESULTS / f"{folder}/{insert_path.parent.stem}/{insert_path.stem}/{sub_batch_number}.json"
        job_table_sub_batch_id = jobs_service.get_or_create_job_table_sub_batch(
            job_table_batch_id, 
            25, 
            sub_batch_number
        )
        if not jobs_service.is_job_table_sub_batch_success(job_table_sub_batch_id):
            try:
                response = salesforce_client.post_attachments(batch)
            except Exception as e:
                response = []
                failed_records = []
                for record in batch:
                    try:
                        response.extend(salesforce_client.post_attachments([record]))
                    except Exception as e:
                        logger.error(f"Attachment failed. FirstPublishLocationId: {record['FirstPublishLocationId']}. SysId: {record['sys_id']}")
                        failed_records.append(record)
                batch[:] = [r for r in batch if r not in failed_records]
            save_json_to_file(response, path_to_result)
            id_mappings = create_id_mapping(response, batch)
            logger.info(f"Inserting id mappings for batch number: {insert_path.parent.stem}, sub batch: {sub_batch_number}")
            mappings_service.insert_id_mappings(id_mapping_table_name, id_mappings)
            jobs_service.update_job_table_sub_batch_status(job_table_sub_batch_id, "success")

            succeeded = sum(1 for r in response if r.get("success"))
            return {"processed": succeeded, "failed": original_count - succeeded}
        else:
            logger.info(f"batch number: {insert_path.parent.stem}, sub batch: {sub_batch_number} skipped")
            return {"processed": 0, "failed": 0}
        
    except Exception as e:
        logger.error(f"Exception occured during attachment upload.", exc_info=e)
        jobs_service.update_job_table_sub_batch_status(job_table_sub_batch_id, "failed")
        return {"processed": 0, "failed": original_count}

def extract_and_write_errors(
    response: list[dict],
    batch: list[dict],
    file_path: str,
):
    """
    Extract Salesforce errors and write them to a file.

    Each line contains one failed record with source + SF error details.
    """

    if len(response) != len(batch):
        raise ValueError(
            f"Response count ({len(response)}) does not match batch count ({len(batch)})"
        )

    error_rows = []

    for idx, result in enumerate(response):
        if result.get("success"):
            continue

        source = batch[idx]
        errors = result.get("errors", [])

        for err in errors:
            error_rows.append(
                {
                    "sys_id": source.get("sys_id"),
                    "referenceId": source.get("referenceId"),
                    "error_code": err.get("statusCode"),
                    "message": err.get("message"),
                    "fields": err.get("fields"),
                }
            )

    if not error_rows:
        return  # nothing to write

    payload = {
        "timestamp": datetime.utcnow().isoformat(),
        "error_count": len(error_rows),
        "errors": error_rows,
    }

    with open(file_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")