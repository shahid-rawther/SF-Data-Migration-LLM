from config.settings import MAX_WORKERS
from services.internal.mappings_service import get_mappings, validate_mappings, cfg_source_tables
import services.internal.jobs_service as jobs_service
from concurrent.futures import ThreadPoolExecutor
from common.logger import logger

executer = ThreadPoolExecutor(max_workers=MAX_WORKERS)


def _resolve_source_plugin(source_plugin=None, source_type: str = "servicenow"):
    """Return the given source plugin, or look one up from the registry.

    Keeping a registry fallback lets CLI / utility callers that don't have a
    plugin instance handy keep working (they default to ServiceNow).
    """
    if source_plugin is not None:
        return source_plugin
    from api import sources as plugin_registry

    plugin = plugin_registry.get(source_type)
    if plugin is None:
        raise ValueError(f"No source integration registered for '{source_type}'")
    return plugin


def extract_full_tables(source_plugin=None):
    job_id = jobs_service.get_or_create_job(1, "extraction")
    plugin = _resolve_source_plugin(source_plugin)
    from integrations.servicenow.http_client import servicenow_client
    all_tables = servicenow_client.get_all_table_list()
    all_tables.remove("sys_attachment_doc")
    all_tables.remove("sys_attachment_doc_v2")
    logger.info(f"[Extraction] Full table extraction started | tables={len(all_tables)}")
    plugin.extract_tables(job_id, all_tables)
    logger.info(f"[Extraction] Full table extraction completed | job_id={job_id}")
    jobs_service.update_job_status(job_id, "success")


def start_extraction(
    job_id: int,
    mappings_file_names: list[str] = [],
    source_plugin=None,
    record_limit: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
):
    job_id = jobs_service.get_or_create_job(job_id, "extraction")
    jobs_service.update_job_status(job_id, "in-progress")
    total_extracted = 0
    plugin = _resolve_source_plugin(source_plugin)
    try:
        logger.info(f"[Extraction] Starting | job_id={job_id} | mappings={mappings_file_names} | record_limit={record_limit}")
        tables_mappings = get_mappings(mappings_file_names)
        validate_mappings(tables_mappings)

        all_tables = []

        for table_mapping in tables_mappings:
            all_tables.extend(cfg_source_tables(table_mapping))

        all_tables = list(dict.fromkeys(all_tables))
        logger.info(f"[Extraction] Resolved tables | plugin={type(plugin).__name__} | tables={all_tables}")
        total_extracted = plugin.extract_tables(
            job_id, all_tables, record_limit=record_limit,
            start_date=start_date, end_date=end_date,
        )
        logger.info(f"[Extraction] Completed | job_id={job_id} | total_extracted={total_extracted}")
    except Exception as e:
        logger.error(f"[Extraction] Failed | job_id={job_id} | error={e}")
        jobs_service.update_job_status(job_id, "failed")
        raise
    jobs_service.update_job_status(job_id, "success")
    return total_extracted


def start_attachment_table_extraction(job_id: int, mappings_file_names: list[str] = [], source_plugin=None):
    job_id = jobs_service.get_or_create_job(job_id, "extraction-attachment-table")
    plugin = _resolve_source_plugin(source_plugin)
    tables_mappings = get_mappings(mappings_file_names)
    validate_mappings(tables_mappings)

    all_tables = []

    for table_mapping in tables_mappings:
        all_tables.extend(cfg_source_tables(table_mapping))

    logger.info(f"[Extraction] Attachment table extraction started | job_id={job_id} | tables={all_tables}")
    plugin.extract_tables(job_id, all_tables)
    logger.info(f"[Extraction] Attachment table extraction completed | job_id={job_id}")
    jobs_service.update_job_status(job_id, "success")


def start_attachment_file_extraction(job_id: int, mappings_file_names: list[str] = [], source_plugin=None):
    job_id = jobs_service.get_or_create_job(job_id, "extraction-attachment-files")
    plugin = _resolve_source_plugin(source_plugin)
    tables_mappings = get_mappings(mappings_file_names)
    validate_mappings(tables_mappings)
    logger.info(f"[Extraction] Attachment file extraction started | job_id={job_id} | mappings={mappings_file_names}")
    try:
        for table_mapping in tables_mappings:
            plugin.extract_attachment_files(job_id, table_mapping)
    except Exception as e:
        logger.error(f"[Extraction] Attachment file extraction failed | job_id={job_id} | error={e}")
        jobs_service.update_job_status(job_id, "failed")
        raise
    logger.info(f"[Extraction] Attachment file extraction completed | job_id={job_id}")
    jobs_service.update_job_status(job_id, "success")
