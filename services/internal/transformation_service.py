from datetime import datetime, timezone
import importlib
import pandas as pd
import os
import tqdm
from config.settings import (
    FILTER_BY_DATE,
    MAX_WORKERS,
    PATH_TO_DOWNLOADS,
    SALESFORCE_EXTRACTION_JOB_NUMBER,
    SERVICE_NOW_BATCH_SIZE,
    SERVICE_NOW_ID_COLUMN,
    PATH_TO_TRANSFORMATION,
    HASH_KEY_NAME,
)
from services.internal import jobs_service
from pathlib import Path
from common.logger import logger
from services.internal import mappings_service
from services.internal.mappings_service import (
    get_id_mapping_table_name,
    get_sales_force_id,
    cfg_source_tables,
    cfg_destination_object,
)
from tools.csv_manager import read_csv_as_dictionary, save_as_csv, write_csv_split_by_size
from services.transforms.generic import GENERIC_TRANSFORMS
import services.external.duckdb_client as duckdb_client
from helpers.hash_helper import create_hash_for_row
from services.internal import extraction_service
from concurrent.futures import ThreadPoolExecutor, as_completed
from tools.reconciliation import reconciliation_tracker

_TRANSFORM_MODULES: dict[str, object] = {}


def _servicenow_transform_module():
    """The ServiceNow transforms, which also serve as the shared fallback."""
    return importlib.import_module("helpers.transformation_helper")


def _get_transform_module(source_type: str):
    """Resolve the transformation module for the given source type.

    ServiceNow functions live in ``helpers.transformation_helper`` (a shim to
    ``integrations.servicenow.transformations``).  JSM functions live in
    ``integrations.jira.transformations``.  Results are cached per source type.
    """
    if source_type in _TRANSFORM_MODULES:
        return _TRANSFORM_MODULES[source_type]

    if source_type == "jsm":
        mod = importlib.import_module("integrations.jira.transformations")
    else:
        mod = _servicenow_transform_module()

    _TRANSFORM_MODULES[source_type] = mod
    return mod


def _resolve_transform_func(func_name: str, source_type: str):
    """Look up a transformation function, checking source-specific module first,
    then falling back to the ServiceNow module for shared helpers."""
    mod = _get_transform_module(source_type)
    if hasattr(mod, func_name):
        return getattr(mod, func_name)
    servicenow = _servicenow_transform_module()
    if mod is not servicenow and hasattr(servicenow, func_name):
        return getattr(servicenow, func_name)
    return None


def transform_exists(func_name: str, source_type: str) -> bool:
    """Whether a named transform can be resolved for a source system.

    A mapping naming one that cannot fails the whole batch part way through the
    transform, after the extract has already run, so callers use this to find
    out before a migration starts rather than during it.
    """
    if not func_name:
        return True
    if func_name in GENERIC_TRANSFORMS:
        return True
    try:
        return _resolve_transform_func(func_name, source_type or "servicenow") is not None
    except Exception:  # noqa: BLE001 — an unimportable module means unresolvable
        return False


def transform_table(job_id: int, table_mappings: dict) -> dict:
    """Transform every batch of one table. Returns what was discarded overall."""
    executer = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    source_type = table_mappings.get("source_type", "servicenow")
    tables_path = PATH_TO_DOWNLOADS / source_type
    salesforce_objects = table_mappings.get("salesforce_objects")
    if salesforce_objects:
        extraction_service.extract_salesforce_objects(
            job_id=SALESFORCE_EXTRACTION_JOB_NUMBER,
            sales_force_objects=salesforce_objects,
        )
    source_tables = cfg_source_tables(table_mappings)
    base_table = tables_path / source_tables[0]
    if not base_table.exists():
        raise FileNotFoundError(
            f"Extracted data directory not found: {base_table}  "
            f"(source_type={source_type!r}). Was extraction completed successfully?"
        )
    base_path_files = list(base_table.iterdir())
    table_name = source_tables[0]  # Driving table
    dest_object = cfg_destination_object(table_mappings)
    operation = table_mappings.get("operation", "insert")
    job_table = get_id_mapping_table_name(
        table_name,
        dest_object,
        table_mappings.get("transformation_name"),
    )
    logger.info(f"Transformation: Table: {job_table}...")
    job_table_id = jobs_service.create_job_table(job_id, job_table)
    jobs_service.update_job_table_status(job_table_id, "in-progress")
    try:
        futures = []
        for batch_path in base_path_files:
            futures.append(
                executer.submit(
                    transform_and_save_batch, batch_path, job_table_id, table_mappings
                )
            )

        rows_dropped = 0
        by_reason: dict[str, int] = {}
        for future in tqdm.tqdm(
            as_completed(futures),
            total=len(futures),
            desc=f"Transform: {base_table.stem}",
            ncols=120,
        ):
            try:
                outcome = future.result() or {}
                rows_dropped += int(outcome.get("rows_dropped") or 0)
                for reason, count in (outcome.get("by_reason") or {}).items():
                    by_reason[reason] = by_reason.get(reason, 0) + count
            except Exception as e:
                logger.exception("Transformation batch failed", exc_info=e)
                jobs_service.update_job_table_status(job_table_id, "failed")
                raise
        executer.shutdown(wait=False)
    except Exception as e:
        logger.exception("Transformation batch failed", exc_info=e)
        jobs_service.update_job_table_status(job_table_id, "failed")
        raise
    jobs_service.update_job_table_status(job_table_id, "success")

    report_path = reconciliation_tracker.flush(object_name=job_table)
    if report_path:
        logger.info(f"Reconciliation report written to {report_path}")

    logger.info(f"Transformation: Table: {job_table}...Done")
    return {
        "object": dest_object,
        "rows_dropped": rows_dropped,
        "by_reason": by_reason,
    }



def transform_and_save_batch(
    batch_path: Path, job_table_id: int, table_mappings: dict
) -> dict:
    """Transform one batch and persist it. Returns what the batch discarded.

    The count is reported rather than only written to the error CSV because a
    dropped row is invisible to the load, and therefore to the progress counters
    that are derived from the load — a run that discarded every record would
    otherwise finish looking clean.
    """
    no_rows_dropped: dict = {"rows_dropped": 0, "by_reason": {}}

    transformations_path = PATH_TO_TRANSFORMATION / "csv"
    transformations_hash_path = PATH_TO_TRANSFORMATION / "hash"
    salesforce_object_name = cfg_destination_object(table_mappings)
    transformation_name = table_mappings.get("transformation_name")
    batch_numbers = table_mappings.get("load_batches")
    batch_size, batch_number = map(int, batch_path.stem.split("_"))

    if batch_numbers and batch_number not in batch_numbers:
        logger.info(
            f"Transformation: {job_table_id}: Batch Number: {batch_number} skipped as per configuration"
        )
        return no_rows_dropped

    job_table_batch_id = jobs_service.get_or_create_job_table_batch(
        job_table_id, batch_size, batch_number, "transformation"
    )

    logger.info(f"Transformation: Batch Number: {batch_number} Status: Pending")
    if jobs_service.is_job_table_batch_success(job_table_batch_id):
        logger.info(f"Transformation: Batch Number: {batch_number} Status: Skipped")
        return no_rows_dropped

    logger.info(f"Transformation: Batch Number: {batch_number} Status: In Progress")
    jobs_service.update_job_table_batch_status(job_table_batch_id, "in-progress")

    try:
        salesforce_object_transformation_path = transformations_path / (
            transformation_name if transformation_name else salesforce_object_name
        )

        salesforce_object_transformation_hash_path = transformations_hash_path / (
            transformation_name if transformation_name else salesforce_object_name
        )

        transformations, transformation_hashes, skipped_rows, deferred_rows = transform_batch(
            batch_path, table_mappings
        )

        if skipped_rows:
            error_batch_path = (
                salesforce_object_transformation_path
                / batch_path.stem
                / f"{len(skipped_rows)}_errors.csv"
            )
            save_as_csv(error_batch_path, skipped_rows)

        # The record of what the backfill pass has to attach. Written under a
        # fixed name so a re-transform of the batch replaces it rather than
        # accumulating a second copy alongside it.
        if deferred_rows:
            save_as_csv(
                salesforce_object_transformation_path
                / batch_path.stem
                / "deferred.csv",
                deferred_rows,
            )

        # ================== INSERT ================================================
        if transformations["insert"]:

            salesforce_object_transformation_path_insert_batch_path = (
                salesforce_object_transformation_path
                / batch_path.stem
                / f"{len(transformations['insert'])}_insert.csv"
            )
            print(f"[TRANSFORM_SAVE] Writing insert CSV to: {salesforce_object_transformation_path_insert_batch_path}", flush=True)

            save_as_csv(
                salesforce_object_transformation_path_insert_batch_path,
                transformations["insert"],
            )
        else:
            print(f"[TRANSFORM_SAVE] NO insert records — skipping CSV write", flush=True)

        # ================== UPDATE ================================================
        if transformations["update"]:
            salesforce_object_transformation_path_update_batch_path = (
                salesforce_object_transformation_path
                / batch_path.stem
                / f"{len(transformations['update'])}_update.csv"
            )

            save_as_csv(
                salesforce_object_transformation_path_update_batch_path,
                transformations["update"],
            )

        if transformation_hashes:
            salesforce_object_transformation_path_insert_hash_batch_path = (
                salesforce_object_transformation_hash_path
                / batch_path.stem
                / f"{len(transformation_hashes)}_insert_hash.csv"
            )
            save_as_csv(
                salesforce_object_transformation_path_insert_hash_batch_path,
                transformation_hashes,
            )
    except Exception as e:
        logger.exception("Exception occured during batch transformtion", exc_info=e)
        jobs_service.update_job_table_batch_status(job_table_batch_id, "failed")
        raise

    logger.info(f"Transformation: Batch Number: {batch_number} Status: Success")
    jobs_service.update_job_table_batch_status(job_table_batch_id, "success")

    # One skipped_rows entry is one field, so the distinct source ids are what
    # says how many records were actually lost.
    by_reason: dict[str, int] = {}
    for entry in skipped_rows:
        reason = str(entry.get("reason") or "unknown")
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return {
        "rows_dropped": len({entry[SERVICE_NOW_ID_COLUMN] for entry in skipped_rows}),
        "by_reason": by_reason,
    }


def extract_unique_source_fields(fields: list[dict]) -> list[str]:
    seen = set()
    result = []

    for f in fields:
        value = f.get("source_field")
        if not value:
            continue

        items = value if isinstance(value, list) else value.split(",")

        for s in items:
            if isinstance(s, str):
                s = s.strip()
                if s and s not in seen:
                    seen.add(s)
                    result.append(s)

    return result

def remove_audit_fields(salesforce_object: dict, extra_fields: list[dict] = []):
    audit_fields = ["LastModifiedDate", "CreatedById", "CreatedDate", "LastModifiedById", "ClosedDate"]
    audit_fields.extend(extra_fields)
    return {k: v for k, v in salesforce_object.items() if k not in audit_fields}

def transform_batch(batch_path: Path, table_mappings: dict):
    source_type = table_mappings.get("source_type", "servicenow")
    source_tables_path = PATH_TO_DOWNLOADS / source_type
    source_tables = cfg_source_tables(table_mappings)
    salesforce_object_name = cfg_destination_object(table_mappings)
    driving_table = source_tables[0]
    _default_id_col = "accountId" if source_type == "jsm" else SERVICE_NOW_ID_COLUMN
    source_id_column = table_mappings.get("source_id_column", _default_id_col)
    fields = table_mappings.get("fields", [])
    joins = table_mappings.get("joins", [])
    filters = table_mappings.get("filters", [])
    operation = table_mappings.get("operation", "insert")
    look_up_table_names = table_mappings.get(
        "look_up_table_names", None
    )
    transformation_name = table_mappings.get("transformation_name")
    id_mapping_table_names = look_up_table_names if look_up_table_names else [
        get_id_mapping_table_name(
            driving_table, salesforce_object_name, transformation_name
        )
    ]
    transformations = {"insert": [], "update": []}
    transformation_hashes = []
    # Rows dropped during transformation are collected here (EC-12/EC-14) so the
    # caller can persist them to an error CSV instead of losing them to a log line.
    skipped_rows: list[dict] = []
    # Rows that loaded with a deferred lookup left empty. These are what the
    # backfill pass has to come back and attach.
    deferred_rows: list[dict] = []
    removable_extra_fields = [field['destination_field'] for field in fields if field.get("ignore_on_update")]

    join_tables = []
    for join_table in source_tables[1:]:
        join_tables.append(source_tables_path / join_table)

    source_fields_list = extract_unique_source_fields(fields)
    source_fields_list.append(f"{driving_table}.{source_id_column}")
    source_data = duckdb_client.fetch_all(
        source_fields_list, batch_path, join_tables, joins, filters
    )

    logger.info(
        f"[Transformation] {salesforce_object_name} | batch={batch_path.stem} | "
        f"rows_after_filter={len(source_data)} | operation={operation}"
    )

    skipped_exists = 0
    skipped_date = 0
    skipped_error = 0

    for _, row in source_data.iterrows():
        salesforce_object = {}
        skip_row = False
       
        if FILTER_BY_DATE:
            sys_created_by = row["sys_created_on"]
            filter_date = datetime.fromisoformat(FILTER_BY_DATE.replace("Z", "+00:00"))
            sys_created_by = datetime.fromisoformat(sys_created_by.replace("Z", "+00:00"))
            if filter_date > sys_created_by:
                skip_row = True
        if skip_row:
            skipped_date += 1
            continue
        
        if operation == "insert" and mappings_service.is_exists(id_mapping_table_names, row[source_id_column]):
            skipped_exists += 1
            continue

        for field in fields:
            field_values = []
            destination_field = field.get("destination_field")

            # Source fields will support str or list
            source_fields = field.get("source_field")
            if source_fields:
                if isinstance(source_fields, str):
                    source_fields = [s.strip() for s in source_fields.split(",")]

                for source_field in source_fields:
                    field_values.append(row.get(source_field.split(".")[-1], ""))

            transformation_function = field.get("transformation_function")
            transformation_params = field.get("transformation_params")
            constant = field.get("use_constant")
            constant_value = field.get("constant_value")
            skip_if_not_found = field.get("skip_if_not_found")
            # A lookup in a reference cycle cannot resolve on the pass that
            # creates the records, because the object it points at has no
            # destination ids yet. Such a field is attempted anyway — it may
            # resolve from an earlier batch — but it must never cost the row,
            # and where it comes back empty the row is noted for the follow-up
            # update rather than silently left unattached.
            deferred = bool(field.get("defer_to_backfill")) and operation == "insert"
            if deferred:
                skip_if_not_found = False
            if constant_value is not None:
                # Per-migration literal chosen in the wizard (UI-specific, no env).
                salesforce_object[destination_field] = constant_value
            elif constant:
                # Legacy: constant resolved from a global environment variable.
                salesforce_object[destination_field] = os.getenv(constant)
            elif transformation_function:
                func = GENERIC_TRANSFORMS.get(transformation_function)
                if func is None:
                    func = _resolve_transform_func(transformation_function, source_type)
                if func is None:
                    raise ValueError(
                        f"Function {transformation_function} not found in module"
                    )
                try:
                    if transformation_params is not None:
                        salesforce_object.update(
                            func(destination_field, *field_values, params=transformation_params)
                        )
                    else:
                        salesforce_object.update(func(destination_field, *field_values))
                    if deferred and not salesforce_object.get(destination_field):
                        # Only a row whose source actually held a reference has
                        # anything to come back for; one that was empty at the
                        # source is correctly empty at the destination.
                        source_value = next(
                            (
                                str(v)
                                for v in field_values
                                if v is not None and not pd.isna(v) and str(v).strip()
                            ),
                            "",
                        )
                        if source_value:
                            # Recorded, not skipped: the record still loads, and
                            # the reference is attached by the backfill pass.
                            deferred_rows.append({
                                SERVICE_NOW_ID_COLUMN: row[source_id_column],
                                "destination_field": destination_field,
                                "source_value": source_value,
                            })
                    if skip_if_not_found:
                        if not salesforce_object[destination_field]:
                            skip_row = True
                            skipped_rows.append({
                                SERVICE_NOW_ID_COLUMN: row[source_id_column],
                                "destination_field": destination_field,
                                "reason": "missing_reference",
                                "error": (
                                    f"'{destination_field}' resolved empty via "
                                    f"'{transformation_function}'; skipped (skip_if_not_found)"
                                ),
                            })
                            continue
                except Exception as e:
                    logger.error("Exception occured during transformation of row. Row Skipped", exc_info=e)
                    skip_row = True
                    skipped_rows.append({
                        SERVICE_NOW_ID_COLUMN: row[source_id_column],
                        "destination_field": destination_field,
                        "reason": "transform_error",
                        "error": str(e),
                    })
                    continue
            else:
                if not field_values:
                    logger.debug(
                        "Skipping unmapped field with no source value for row %s, destination=%s",
                        row[source_id_column],
                        destination_field,
                    )
                    continue
                if len(field_values) == 1:
                    try:
                        # EC-13: only treat null/NaN as empty. Using bool() here
                        # wrongly blanked legitimate falsy values like 0 / False.
                        raw_value = field_values[0]
                        field_value = "" if (raw_value is None or pd.isna(raw_value)) else raw_value
                        salesforce_object[destination_field] = field_value
                    except Exception as e:
                        logger.error(
                            f"Error processing field {destination_field} with value {field_values[0]}: {e}"
                        )
                        skip_row = True
                        skipped_error += 1
                        continue
                else:
                    logger.error(
                        "Multiple source values found for non-transform field on row %s, destination=%s, source_field=%s",
                        row[source_id_column],
                        destination_field,
                        field.get("source_field"),
                    )
                    skip_row = True
                    skipped_error += 1
                    continue
        if skip_row:
            continue

        if operation == "update":
            sales_force_id = get_sales_force_id(
                row[source_id_column], id_mapping_table_names
            )
            if sales_force_id:
                salesforce_object["Id"] = sales_force_id
                salesforce_object = remove_audit_fields(salesforce_object, removable_extra_fields)
                transformations["update"].append(salesforce_object)
        elif operation == "insert":
            source_hash = {}
            source_hash[HASH_KEY_NAME] = create_hash_for_row(salesforce_object)
            source_hash[SERVICE_NOW_ID_COLUMN] = row[source_id_column]
            transformations["insert"].append(salesforce_object)
            transformation_hashes.append(source_hash)
        elif operation == "upsert":
            sales_force_id = get_sales_force_id(
                row[source_id_column], id_mapping_table_names
            )
            if sales_force_id:
                salesforce_object["Id"] = sales_force_id
                salesforce_object = remove_audit_fields(salesforce_object, removable_extra_fields)
                transformations["update"].append(salesforce_object)
            else:
                source_hash = {}
                source_hash[HASH_KEY_NAME] = create_hash_for_row(salesforce_object)
                source_hash[SERVICE_NOW_ID_COLUMN] = row[source_id_column]
                transformations["insert"].append(salesforce_object)
                transformation_hashes.append(source_hash)

        logger.debug(f"Transform record {row[source_id_column]} ends at {datetime.now()}")

    logger.info(
        f"[Transformation] {salesforce_object_name} | batch={batch_path.stem} | "
        f"insert={len(transformations['insert'])}, update={len(transformations['update'])} | "
        f"skipped: exists={skipped_exists}, date_filter={skipped_date}, error={skipped_error}"
    )
    if skipped_rows:
        error_count = sum(1 for r in skipped_rows if r["reason"] == "transform_error")
        missing_ref_count = sum(1 for r in skipped_rows if r["reason"] == "missing_reference")
        logger.warning(
            "Transformation skipped %d row(s) for %s: %d transform error(s), "
            "%d missing-reference row(s)",
            len(skipped_rows), salesforce_object_name, error_count, missing_ref_count,
        )
    if deferred_rows:
        by_field: dict[str, int] = {}
        for entry in deferred_rows:
            by_field[entry["destination_field"]] = by_field.get(entry["destination_field"], 0) + 1
        logger.info(
            "[Transformation] %s | batch=%s | deferred for backfill: %s",
            salesforce_object_name,
            batch_path.stem,
            ", ".join(f"{field}={count}" for field, count in sorted(by_field.items())),
        )
    return transformations, transformation_hashes, skipped_rows, deferred_rows
