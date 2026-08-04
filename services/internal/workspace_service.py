"""Working-directory cleanup for fresh (non-resumable) migration runs.

The extraction/transformation/loading stages persist intermediate files on
disk keyed by table/object name (not by run). For always-fresh wizard runs we
clear these directories up front so a new run never picks up stale batches from
a previous run.

This is only wired into the wizard runtime (``migration_manager._do_run``); the
CLI path intentionally keeps its resumable behavior.
"""
import shutil

from common.logger import logger
from config.settings import PATH_TO_DOWNLOADS, PATH_TO_TRANSFORMATION
from services.internal.mappings_service import (
    cfg_destination_object,
    cfg_source_tables,
    get_mappings,
)


def clean_working_state(
    selected_objects: list[str],
    source_type: str = "servicenow",
) -> None:
    """Remove on-disk working directories for the given mappings.

    For each selected mapping this clears:
      - the extracted source data (``downloads/<source_type>/<source_table>``)
      - the transformed CSVs (``transformation/csv/<name>``)
      - the transformation hashes (``transformation/hash/<name>``)

    Failures are logged but never raised — a stale-cleanup issue must not abort
    the migration.
    """
    if not selected_objects:
        return

    mappings = get_mappings(selected_objects)
    for config in mappings:
        for source_table in cfg_source_tables(config):
            _safe_rmtree(PATH_TO_DOWNLOADS / source_type / source_table)

        transformation_name = config.get("transformation_name")
        folder = transformation_name or cfg_destination_object(config)
        if folder:
            _safe_rmtree(PATH_TO_TRANSFORMATION / "csv" / folder)
            _safe_rmtree(PATH_TO_TRANSFORMATION / "hash" / folder)


def clean_transformed_state(selected_objects: list[str]) -> None:
    """Remove only the transformed output for the given mappings.

    Used when an object is re-transformed after a repair: the staged extract must
    survive (that is the point of staging) but the previous transform output has
    to go, so the retry loads the patched result rather than a stale CSV.
    """
    if not selected_objects:
        return

    for config in get_mappings(selected_objects):
        folder = config.get("transformation_name") or cfg_destination_object(config)
        if folder:
            _safe_rmtree(PATH_TO_TRANSFORMATION / "csv" / folder)
            _safe_rmtree(PATH_TO_TRANSFORMATION / "hash" / folder)


def _safe_rmtree(path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
        logger.info("Cleared working directory for fresh run: %s", path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not clear working directory %s: %s", path, exc)
