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


def _build_transformation_error_message(exc: Exception) -> str:
    raw = str(exc)
    if "/services/oauth2/token" in raw or "refresh Salesforce access token" in raw:
        return (
            "Transformation failed while authenticating to Salesforce. "
            "Please verify Salesforce base URL, client ID, client secret, and connected app settings."
        )
    return f"Transformation failed: {raw}"



def start_trasformation(job_id: int, mappings_file_names: list[str] = []):
    # Debug: write to file since terminal isn't showing prints
    from pathlib import Path
    _dbg = Path("_transform_debug.log")
    _dbg.write_text(f"CALLED: mappings={mappings_file_names}\n", encoding="utf-8")

    job_id = jobs_service.get_or_create_job(job_id, "transformation")
    jobs_service.update_job_status(job_id, "in-progress")
    try:
        if not mappings_file_names:
            with open(_dbg, "a") as f: f.write("EMPTY mappings — returning early\n")
            return
        tables_mappings = get_mappings(mappings_file_names)
        with open(_dbg, "a") as f: f.write(f"get_mappings returned {len(tables_mappings)} configs\n")
        validate_mappings(tables_mappings)
        for table_mappings in tables_mappings:
            with open(_dbg, "a") as f:
                f.write(f"transform_table: dest={table_mappings.get('destination_object')}, source_type={table_mappings.get('source_type')}\n")
                f.write(f"  source_tables={table_mappings.get('source_tables')}\n")
                f.write(f"  fields_count={len(table_mappings.get('fields', []))}\n")
            transform_table(job_id, table_mappings)
            with open(_dbg, "a") as f: f.write("transform_table DONE\n")
    except Exception as e:
        with open(_dbg, "a") as f: f.write(f"EXCEPTION: {e}\n")
        logger.exception("Exception occured during transformation:", exc_info=e)
        jobs_service.update_job_status(job_id, "failed")
        raise RuntimeError(_build_transformation_error_message(e)) from e
    jobs_service.update_job_status(job_id, "success")
    with open(_dbg, "a") as f: f.write("SUCCESS\n")

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



