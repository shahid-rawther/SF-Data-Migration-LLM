"""Salesforce extraction.

Pulls Salesforce objects into CSV under ``data/downloads/salesforce`` so the
transformation step can use them as lookup sources. Moved out of the old
``services/internal/extraction_service.py``.
"""
import math
from datetime import datetime

import tqdm

from common.logger import logger
from config.settings import (
    PATH_TO_DOWNLOADS,
    SALESFORCE_BATCH_SIZE,
    SERVICE_NOW_START_DATE,
    SERCICE_NOW_END_DATE,
)
from services.internal import jobs_service
from integrations.salesforce.http_client import salesforce_client
from tools.csv_manager import save_as_csv


def extract_salesforce_objects(job_id: int, sales_force_objects: list[dict]):
    jobs_service.get_or_create_job(job_id, "extraction-salesforce")
    for sales_force_object_details in sales_force_objects:
        obj_name = sales_force_object_details.get("object_name", "")
        try:
            extract_salesforce_table(job_id, sales_force_object_details)
        except Exception as exc:
            logger.warning(
                "Salesforce extraction skipped for '%s' (object may not exist in org): %s",
                obj_name, exc,
            )
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
