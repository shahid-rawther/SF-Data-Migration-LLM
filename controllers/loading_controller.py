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
)
import services.internal.jobs_service as jobs_service
from services.http_clients.salesforce_http_client import salesforce_client
from config.settings import (
    DISABLE_LOAD,
    MAX_WORKERS,
    PATH_TO_TRANSFORMATION,
    PATH_TO_RESULTS,
    POLL_TIME,
)
from tools.file_manager import save_text_to_file, read_text_from_file
from tools.csv_manager import convert_to_salesforce_hash, convert_to_servicenow_hash, read_csv_as_dictionary, save_as_csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from services.internal import loading_service


def start_loading(job_id: int, mappings_file_names: list[str] = []):
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
        if update_path.exists():
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
        return []

    if operation == "insert" and insert_path:
        loading_service.load_insert_attachments(
            insert_path,
            job_table_id,
            job_table,
            table_mapping
        )



def load_table(job_id: int, table_mapping: dict):
    executer_parallel = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    executer_queue = ThreadPoolExecutor(max_workers=1)
    table_name = table_mapping["servicenow_tables"][0]  # Driving table
    salesforce_object = table_mapping["salesforce_object"]
    transformation_name = table_mapping.get("transformation_name")
    use_queue = table_mapping.get("use_queue")
    batch_numbers = table_mapping.get("load_batches")
    is_attachment_table = table_mapping.get("is_attachment_table")
    futures = []
    transformation_path = (
        PATH_TO_TRANSFORMATION
        / "csv"
        / (transformation_name if transformation_name else salesforce_object)
    )
    operation = table_mapping["operation"]
    job_table = get_id_mapping_table_name(
        table_name,
        table_mapping["salesforce_object"],
        table_mapping.get("transformation_name"),
    )
    logger.info(f"Loading: {job_table}...")
    
    job_table_id = jobs_service.create_job_table(job_id, job_table)

    executer = executer_queue if use_queue else executer_parallel
    if Path(transformation_path).exists():
        for batch_path in transformation_path.iterdir():
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
        
    for future in tqdm.tqdm(
        as_completed(futures),
        total=len(futures),
        desc=f"Loading: {job_table}",
        ncols=120,
    ):
        try:
            future.result()  
        except Exception as e:
            logger.exception("Loading batch failed", exc_info=e)
            jobs_service.update_job_table_status(job_table_id, "failed")
            raise
    jobs_service.update_job_table_status(job_table_id, "success")
    executer_parallel.shutdown(wait=False)
    executer_queue.shutdown(wait=False)


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
        return

    try:
        jobs_service.update_job_table_batch_status(table_batch_id, "in-progress")
        load_batch(job_table_id, table_batch_id, batch_path, "insert", table_mapping)
        jobs_service.update_job_table_batch_status(table_batch_id, "success")
        logger.info(
            f"Loading: Insert: Batch Number: {batch_number}, Insert Batch Size: {batch_insert_size}, Status: Success"
        )

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
        return

    try:
        jobs_service.update_job_table_batch_status(table_batch_id, "in-progress")
        full_migrated = load_batch(job_table_id, table_batch_id, batch_path, "update", table_mapping)
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
        
    except Exception as e:
        jobs_service.update_job_table_batch_status(table_batch_id, "failed")
        logger.exception(f"Loading: Update: Batch: {batch_number}, Update Batch Size: {batch_update_size}, Status: Failed", exc_info=e)
        raise

def load_batch(job_table_id:int, table_batch_id: int, batch_path: Path, operation: str, table_mappings: dict):
    if DISABLE_LOAD:
        return
    full_migrated = False;
    salesforce_object = table_mappings["salesforce_object"]
    transformation_name = table_mappings.get("transformation_name")
    service_now_driving_table = table_mappings["servicenow_tables"][0]
    id_mapping_table_name = get_id_mapping_table_name(
        service_now_driving_table, salesforce_object, transformation_name
    )
    batch_size = int(batch_path.stem.split("_")[0])

    # Create a Job
    job = salesforce_client.create_job(salesforce_object, operation)
    job_id = job["id"]
    jobs_service.update_job_table_batch_salesforce_job_id(table_batch_id, job_id)

    salesforce_client.upload_csv(job_id, batch_path)
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

    if job_satus:
        processed_records = job_satus.get("numberRecordsProcessed")
        failed_records = job_satus.get("numberRecordsFailed")
        unprocessed_records = batch_size - processed_records
        results = {
            "processed_records": processed_records,
            "failed_records": failed_records,
            "unprocessed_records": unprocessed_records
        }
        jobs_service.update_job_table_batch_results(table_batch_id, results)
        full_migrated = failed_records == 0 and processed_records > 0 and unprocessed_records == 0

        
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
        servicenow_hash_path = ""
        servicenow_hash_path = (
            PATH_TO_TRANSFORMATION
            / "hash"
            / (transformation_name if transformation_name else salesforce_object)
            / batch_path.parent.stem
            / f"{batch_path.stem}_hash.csv"
        )
        
        id_mappings = map_servicenow_to_salesforce(
            servicenow_hash_path, salesforce_hash
        )

        insert_id_mappings(id_mapping_table_name, id_mappings)

    failed = salesforce_client.get_results(job_id, "failedResults")
    save_text_to_file(failed, failed_path)

    unprocessed = salesforce_client.get_results(job_id, "unprocessedRecords")
    save_text_to_file(unprocessed, unprocessed_path)

    return full_migrated


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
