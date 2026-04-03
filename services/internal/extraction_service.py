from datetime import datetime
from pathlib import Path

import tqdm
from common.logger import logger
from config.settings import (
    MAX_WORKERS,
    PATH_TO_DOWNLOADS,
    SALESFORCE_BATCH_SIZE,
    SERVICE_NOW_START_DATE,
    SERCICE_NOW_END_DATE,
    SERVICE_NOW_BATCH_SIZE,
    SERVICE_NOW_NO_OF_BATCH,
)
from services.internal import jobs_service
from services.http_clients.servicenow_http_client import servicenow_client
from services.http_clients.salesforce_http_client import salesforce_client
from tools import file_manager
from tools.csv_manager import save_as_csv
from services.external import duckdb_client
from concurrent.futures import ThreadPoolExecutor, as_completed
import math


def extract_servicenow_tables(job_id: int, service_now_tables: list):
    executer_queue = ThreadPoolExecutor(max_workers=1)
    futures = []
    for servicenow_table in service_now_tables:
        futures.append(
            executer_queue.submit(extract_servicenow_table, job_id, servicenow_table)
        )

    for future in as_completed(futures):
            future.result()
    

    executer_queue.shutdown(wait=True)

def extract_servicenow_table(job_id: int, servicenow_table: str):
    executer = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    job_table_id = jobs_service.get_or_create_job_table(job_id, servicenow_table)
    logger.info(f"Job: {job_id}, Job Table: {job_table_id}")
    logger.info(
        f"Servicenow table '{servicenow_table}' extraction start at {datetime.now()}"
    )
    try:

        start_date = SERVICE_NOW_START_DATE
        end_date = SERCICE_NOW_END_DATE

        row_count = servicenow_client.get_row_count(servicenow_table, start_date, end_date)
        logger.info(f"Total rows in {servicenow_table} is {row_count}")

        futures = []

        batch_no = 0
        while True:

            offset = batch_no * SERVICE_NOW_BATCH_SIZE

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
                    end_date
                )
            )

            batch_no += 1
            if SERVICE_NOW_NO_OF_BATCH and batch_no >= SERVICE_NOW_NO_OF_BATCH:
                break
        
        for future in tqdm.tqdm(as_completed(futures), total=len(futures), desc=f"Ext: {servicenow_table}", ncols=120):
            future.result()  
        
        logger.info(
            f"Servicenow table '{servicenow_table}' extraction ends at {datetime.now()}"
        )
    except Exception as e:
        logger.error(
            f"Exception occured during fetching table: Job Table Id: {job_table_id}, Error: {e}"
        )
        jobs_service.update_job_table_status(job_table_id, "failed")
    jobs_service.update_job_table_status(job_table_id, "success")

def extract_single_batch(
        job_table_id: int, 
        batch_no: int,
        servicenow_table: str,
        offset: int,
        start_date:str = "",
        end_date:str = ""

    ):
    job_table_batch_id = jobs_service.get_or_create_job_table_batch(
        job_table_id, SERVICE_NOW_BATCH_SIZE, batch_no, "extraction"
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
                / f"{SERVICE_NOW_BATCH_SIZE}_{batch_no}.csv"
            )

            batch = servicenow_client.fetch_table(
                servicenow_table,
                start_date,
                end_date,
                offset,
                SERVICE_NOW_BATCH_SIZE,
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
    records_found = 0
    attachment_meta_data = attachment_meta_data.to_dict(orient='records')
    for row in attachment_meta_data:
        sys_id = row["sys_id"]
        file_name = row["file_name"]
        try:
            attachment_response = servicenow_client.get_attachment_doc(sys_id)
        except Exception as e:
            logger.error(f"File not found for sys id: {sys_id}", exc_info=e)
            continue
        path = (
            PATH_TO_DOWNLOADS
            / "servicenow"
            / "sys_attachment_files"
            / f"{sys_id}_{file_name}"
        )
        file_manager.save_stream_to_file(attachment_response, path)
        records_found += 1
    logger.info(f"Total records extracted for batch number {batch_number} is {records_found}")
    jobs_service.update_job_table_batch_status(job_table_batch_id,"success")

def extract_salesforce_objects(job_id: int, sales_force_objects: list[dict]):
    jobs_service.get_or_create_job(job_id, "extraction-salesforce")
    for sales_force_object_details in sales_force_objects:
        extract_salesforce_table(job_id, sales_force_object_details)
    jobs_service.update_job_status(job_id, "success")

def extract_salesforce_table(job_id: int, sales_force_object_details: dict):
    job_table_id = jobs_service.get_or_create_job_table(
        job_id, sales_force_object_details["object_name"]
    )
    logger.info(f"Job: {job_id}, Job Table: {job_table_id}")
    sales_force_object = sales_force_object_details["object_name"]
    fields = sales_force_object_details["fields"]
    logger.info(
        f"Salesforce table '{sales_force_object}' extraction start at {datetime.now()}"
    )

    start_date = SERVICE_NOW_START_DATE
    end_date = SERCICE_NOW_END_DATE

    row_count = salesforce_client.get_row_count(
        sales_force_object, start_date, end_date
    )
    logger.info(f"Total rows in {sales_force_object} is {row_count}")

    batch_no = 0
    number_of_batches = math.ceil(row_count / SALESFORCE_BATCH_SIZE)
    next_records_url = ""
    batches = list(range(number_of_batches))

    for batch_no in tqdm.tqdm(
        batches,
        total=len(batches),
        desc=f"Extraction From Salesforce - {sales_force_object}",
        ncols=120,
    ):

        job_table_batch_id = jobs_service.get_or_create_job_table_batch(
            job_table_id, SALESFORCE_BATCH_SIZE, batch_no, "extraction"
        )

        logger.info(
            f"Start: { datetime.now()} Table: {sales_force_object}, Batch: {batch_no}, Job Batch Id: {job_table_batch_id}"
        )
        if not jobs_service.is_job_table_batch_success(job_table_batch_id):
            path_csv = (
                PATH_TO_DOWNLOADS
                / "salesforce"
                / f"{sales_force_object}"
                / f"{SALESFORCE_BATCH_SIZE}_{batch_no}.csv"
            )

            response = salesforce_client.fetch_table(
                sales_force_object, fields, next_records_url
            )
            next_records_url = (
                response["nextRecordsUrl"] if "nextRecordsUrl" in response else None
            )

            # response = JSON from Salesforce
            records = response["records"]

            # Remove attributes
            clean_records = [
                {k: v for k, v in rec.items() if k != "attributes"} for rec in records
            ]

            save_as_csv(path_csv, clean_records)
            jobs_service.update_job_table_batch_status(job_table_batch_id, "success")

            if response.get("done") is True:
                break

        logger.info(
            f"End: {datetime.now()} Table: {sales_force_object}, Batch: {batch_no}, Job Batch Id: {job_table_batch_id}"
        )
    jobs_service.update_job_table_status(job_table_id, "success")
    logger.info(
        f"Salesforce table '{sales_force_object}' extraction ends at {datetime.now()}"
    )
