import tqdm
from common.logger import logger

from services.internal.mappings_service import (
    get_mappings,
    get_sales_force_id,
    validate_mappings,
    get_id_mapping_table_name,
)

import services.internal.jobs_service as jobs_service
from services.internal.transformation_service import transform_table



def start_trasformation(job_id: int, mappings_file_names: list[str] = []):
    job_id = jobs_service.get_or_create_job(job_id, "transformation")
    jobs_service.update_job_status(job_id, "in-progress")
    try:
        if not mappings_file_names:
            return
        tables_mappings = get_mappings(mappings_file_names)
        validate_mappings(tables_mappings)
        for table_mappings in tables_mappings:
            transform_table(job_id, table_mappings)
    except Exception as e:
        logger.exception("Exception occured during transformation:", exc_info=e)
        jobs_service.update_job_status(job_id, "failed")
        return
    jobs_service.update_job_status(job_id, "success")

def start_attachment_trasformation(job_id: int, mappings_file_names: list[str] = []):
    job_id = jobs_service.get_or_create_job(job_id, "transformation")
    if not mappings_file_names:
        return
    tables_mappings = get_mappings(mappings_file_names)
    validate_mappings(tables_mappings)

    for table_mappings in tables_mappings:
        table_name = table_mappings["servicenow_tables"][0]  # Driving table
        job_table = get_id_mapping_table_name(
            table_name, table_mappings["salesforce_object"], table_mappings.get("transformation_name")
        )
        logger.info(f"Transformation: Table: {job_table}...")
        operation = table_mappings["operation"]
        
        if operation == "update":
            job_table_id = jobs_service.create_job_table(job_id, job_table)
        else:
            job_table_id = jobs_service.get_or_create_job_table(job_id, job_table)

        job_table_id = jobs_service.get_or_create_job_table(job_id, job_table)
        transform_table(job_table_id, table_mappings)
        jobs_service.update_job_table_status(job_table_id, "success")
        logger.info(f"Transformation: Table: {job_table}...Done")

    jobs_service.update_job_status(job_id, "success")



