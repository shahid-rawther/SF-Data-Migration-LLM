
from datetime import datetime
import tqdm
from config.settings import MAX_WORKERS
from services.internal.extraction_service import extract_servicenow_tables, extract_servicenow_files
from services.internal.mappings_service import get_mappings, validate_mappings
import services.internal.jobs_service as jobs_service
from concurrent.futures import ThreadPoolExecutor, as_completed

executer = ThreadPoolExecutor(max_workers=MAX_WORKERS)


def extract_full_tables():
    job_id = jobs_service.get_or_create_job(1, "extraction")
    from services.http_clients.servicenow_http_client import servicenow_client
    all_tables = servicenow_client.get_all_table_list()
    all_tables.remove("sys_attachment_doc")
    all_tables.remove("sys_attachment_doc_v2")
    extract_servicenow_tables(job_id, all_tables)
    jobs_service.update_job_status(job_id, "success")

def start_extraction(job_id: int, mappings_file_names: list[str] = []):
    job_id = jobs_service.get_or_create_job(job_id, "extraction")
    tables_mappings = get_mappings(mappings_file_names)
    validate_mappings(tables_mappings)
 
    all_tables = []

    for table_mapping in tables_mappings:
        all_tables.extend(table_mapping["servicenow_tables"])
    
    all_tables = list(dict.fromkeys(all_tables))
    extract_servicenow_tables(job_id, all_tables)
    jobs_service.update_job_status(job_id, "success")

def start_attachment_table_extraction(job_id: int, mappings_file_names: list[str] = []):
    job_id = jobs_service.get_or_create_job(job_id, "extraction-attachment-table")
    tables_mappings = get_mappings(mappings_file_names)
    validate_mappings(tables_mappings)

    all_tables = []

    for table_mapping in tables_mappings:
        all_tables.extend(table_mapping["servicenow_tables"])

    extract_servicenow_tables(job_id, all_tables)
    jobs_service.update_job_status(job_id, "success")



def start_attachment_file_extraction(job_id: int, mappings_file_names: list[str] = []):
    job_id = jobs_service.get_or_create_job(job_id, "extraction-attachment-files")
    tables_mappings = get_mappings(mappings_file_names)
    validate_mappings(tables_mappings)
    try:

        for table_mapping in tables_mappings:
            extract_servicenow_files(job_id, table_mapping)
    except Exception as e:
         jobs_service.update_job_status(job_id, "failed")
         raise
    jobs_service.update_job_status(job_id, "success")



