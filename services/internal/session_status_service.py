"""Per-object migration status for one session.

Answers, for every object a session selected: what it reads, what it writes,
how many rows the source had, and how many of those are in the destination now.

The row counts come from the two places that already know:

* ``id_mappings`` holds one row per source record written to the destination, so
  its count is the number of records actually migrated — the same table the
  transform consults to skip rows that are already loaded, which is why this
  figure and the resume point can never disagree;
* the source's own total, recorded on the extraction job each time a table is
  pulled, is the denominator the progress bar needs. A run with a record limit
  stages only a slice of that total, so the staged extract on disk is used only
  when no total was recorded.

Neither depends on the session still running, so a finished or interrupted
session reports the same numbers as a live one. A checkpoint, when one exists,
only fills the gaps: per-object failure counts, and the source row counts for a
session whose staged extract has since been cleared from disk.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from common.logger import logger
from services.external.db_client import db_client
from services.internal.mappings_service import (
    cfg_destination_object,
    cfg_source_tables,
    get_id_mapping_table_name,
    get_mapping_config,
)

# What a session's objects can be doing, in the order the UI shows them.
STATE_COMPLETED = "completed"
STATE_IN_PROGRESS = "in_progress"
STATE_PENDING = "pending"
STATE_UNMAPPED = "unmapped"

# Where a source row count came from, so the UI can say when it is an estimate.
ORIGIN_SOURCE = "source"
ORIGIN_STAGE = "stage"
ORIGIN_CHECKPOINT = "checkpoint"
ORIGIN_NONE = "none"


@dataclass
class ObjectStatus:
    """One selected object's source, destination and row counts."""

    object: str
    source_table: str = ""
    source_tables: list[str] = field(default_factory=list)
    destination_object: str = ""
    id_mapping_table: str = ""
    source_rows: int = 0
    source_rows_origin: str = ORIGIN_NONE
    loaded: int = 0
    processed: int = 0
    failed: int = 0
    percent: float = 0.0
    state: str = STATE_PENDING
    current: bool = False
    errors: list[str] = field(default_factory=list)
    # How the loader names this object in its per-object counts. Kept off the
    # response because it is an internal key, not something to display.
    counts_key: str = ""

    def to_dict(self) -> dict:
        return {
            "object": self.object,
            "source_table": self.source_table,
            "source_tables": self.source_tables,
            "destination_object": self.destination_object,
            "id_mapping_table": self.id_mapping_table,
            "source_rows": self.source_rows,
            "source_rows_origin": self.source_rows_origin,
            "loaded": self.loaded,
            "processed": self.processed,
            "failed": self.failed,
            "percent": self.percent,
            "state": self.state,
            "current": self.current,
            "errors": self.errors,
        }


def _staged_rows(selected_objects: list[str], source_type: str) -> dict[str, int]:
    """Rows on disk per source table, or an empty map when nothing is staged."""
    from services.internal import staging_service

    try:
        manifest = staging_service.inspect(
            selected_objects, source_type=source_type, count_rows=True
        )
    except Exception as exc:  # noqa: BLE001 — a status view must not fail over this
        logger.warning("[status] could not inspect the local stage: %s", exc)
        return {}
    return {table.table: table.rows for table in manifest.tables}


def _checkpoint_rows(checkpoint_summary: dict | None) -> dict[str, int]:
    """Rows per source table as they were when the checkpoint was written."""
    if not checkpoint_summary:
        return {}
    stage = checkpoint_summary.get("stage") or {}
    return {
        entry.get("table", ""): int(entry.get("rows") or 0)
        for entry in stage.get("tables") or []
        if entry.get("table")
    }


def _loaded_counts(id_tables: list[str]) -> dict[str, int]:
    """Records written to the destination, per ``id_mappings.table_name``."""
    tables = [name for name in dict.fromkeys(id_tables) if name]
    if not tables:
        return {}
    placeholders = ",".join("?" for _ in tables)
    rows = db_client.fetch_all(
        "SELECT table_name, COUNT(DISTINCT servicenow_id) FROM id_mappings "
        f"WHERE table_name IN ({placeholders}) GROUP BY table_name",
        tuple(tables),
    )
    return {row[0]: int(row[1] or 0) for row in rows}


def _counts_for(
    object_name: str, counts_key: str, records_by_object: dict
) -> dict[str, int]:
    """The load result for one object.

    The loader keys its per-object counts by transformation name (falling back to
    the destination object), which is not always the mapping name the session
    selected, so both are tried.
    """
    entry = records_by_object.get(object_name) or records_by_object.get(counts_key)
    if not isinstance(entry, dict):
        return {"processed": 0, "failed": 0}
    return {
        "processed": int(entry.get("processed") or 0),
        "failed": int(entry.get("failed") or 0),
    }


def _current_objects(live: dict | None) -> set[str]:
    """The objects the running session is working on right now.

    Progress events name a whole priority group at once, comma-joined.
    """
    if not live:
        return set()
    raw = str(live.get("object") or "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def build(
    session: dict,
    *,
    live: dict | None = None,
    checkpoint_summary: dict | None = None,
    local_index: dict | None = None,
) -> dict:
    """Describe what a session has migrated, object by object.

    ``live`` is the running session's progress event, ``checkpoint_summary`` the
    saved resume state and ``local_index`` its local record. All three are
    optional: the row counts stand on their own, and these only add failure
    detail and a source total for a session whose stage has been cleared.
    """
    session_id = session.get("id", "")
    source_type = session.get("source_type") or "servicenow"
    selected: list[str] = session.get("selected_objects") or []
    records_by_object: dict = (
        (live or {}).get("records_by_object")
        or (checkpoint_summary or {}).get("records_by_object")
        or {}
    )
    load_errors: dict = (live or {}).get("load_errors") or {}
    completed_objects = set(
        (checkpoint_summary or {}).get("completed_objects")
        or (local_index or {}).get("completed_objects")
        or []
    )
    in_flight = _current_objects(live)

    from services.internal import jobs_service

    staged = _staged_rows(selected, source_type)
    from_checkpoint = _checkpoint_rows(checkpoint_summary)

    configs = {name: get_mapping_config(name) for name in selected}
    source_totals = jobs_service.latest_source_totals(
        [
            table
            for config in configs.values()
            if config
            for table in [t for t in cfg_source_tables(config) if t][:1]
        ]
    )

    entries: list[ObjectStatus] = []
    for name in selected:
        config = configs[name]
        if not config:
            entries.append(ObjectStatus(object=name, state=STATE_UNMAPPED))
            continue

        tables = [table for table in cfg_source_tables(config) if table]
        driving = tables[0] if tables else ""
        destination = cfg_destination_object(config)
        transformation_name = config.get("transformation_name") or ""
        id_table = (
            get_id_mapping_table_name(driving, destination, transformation_name)
            if driving and destination
            else ""
        )

        source_rows = source_totals.get(driving, 0)
        origin = ORIGIN_SOURCE if source_rows else ORIGIN_NONE
        if not source_rows:
            source_rows = staged.get(driving, 0)
            origin = ORIGIN_STAGE if source_rows else ORIGIN_NONE
        if not source_rows:
            source_rows = from_checkpoint.get(driving, 0)
            origin = ORIGIN_CHECKPOINT if source_rows else ORIGIN_NONE

        entries.append(
            ObjectStatus(
                object=name,
                source_table=driving,
                source_tables=tables,
                destination_object=destination,
                id_mapping_table=id_table,
                source_rows=source_rows,
                source_rows_origin=origin,
                current=name in in_flight or destination in in_flight,
                errors=list(
                    load_errors.get(name)
                    or load_errors.get(transformation_name or destination)
                    or []
                ),
                counts_key=transformation_name or destination,
            )
        )

    loaded = _loaded_counts([entry.id_mapping_table for entry in entries])
    for entry in entries:
        entry.loaded = loaded.get(entry.id_mapping_table, 0)
        counts = _counts_for(entry.object, entry.counts_key, records_by_object)
        entry.processed = counts["processed"]
        entry.failed = counts["failed"]
        # A dry run writes nothing to the destination, so the transformed count
        # is the only evidence the object was worked at all.
        reached = entry.loaded or entry.processed
        if entry.source_rows > 0:
            entry.percent = round(min(entry.loaded / entry.source_rows, 1.0) * 100, 1)
        elif entry.object in completed_objects:
            entry.percent = 100.0

        if entry.state == STATE_UNMAPPED:
            continue
        # A run marks an object complete when it finishes its slice, which under
        # a record limit is not the whole table. The source total, when known,
        # says whether rows are still waiting to be extracted.
        if entry.source_rows_origin == ORIGIN_SOURCE:
            done = entry.loaded >= entry.source_rows
        else:
            done = entry.object in completed_objects or (
                entry.source_rows > 0 and entry.loaded >= entry.source_rows
            )
        if done:
            entry.state = STATE_COMPLETED
        elif entry.current or reached or entry.failed:
            entry.state = STATE_IN_PROGRESS
        else:
            entry.state = STATE_PENDING

    total_source = sum(entry.source_rows for entry in entries)
    total_loaded = sum(entry.loaded for entry in entries)
    total_failed = sum(entry.failed for entry in entries)

    return {
        "session_id": session_id,
        "source_type": source_type,
        "destination_type": "salesforce",
        "status": (live or {}).get("status") or session.get("status") or "",
        "phase": (live or {}).get("phase") or session.get("phase") or "",
        "started_at": session.get("started_at") or "",
        "updated_at": session.get("updated_at") or "",
        "completed_at": session.get("completed_at") or "",
        "live": bool(live),
        "dry_run": bool(
            ((session.get("config") or {}).get("options") or {}).get("disable_load")
        ),
        "objects": [entry.to_dict() for entry in entries],
        "totals": {
            "objects": len(entries),
            "objects_completed": sum(
                1 for entry in entries if entry.state == STATE_COMPLETED
            ),
            "source_rows": total_source,
            "loaded": total_loaded,
            "failed": total_failed,
            "percent": (
                round(min(total_loaded / total_source, 1.0) * 100, 1)
                if total_source
                else 0.0
            ),
        },
        "checkpoint": (
            {
                "sequence": (checkpoint_summary or {}).get("sequence")
                or (local_index or {}).get("sequence")
                or 0,
                "saved_at": (checkpoint_summary or {}).get("saved_at")
                or (local_index or {}).get("saved_at")
                or "",
                "phase": (checkpoint_summary or {}).get("phase")
                or (local_index or {}).get("phase")
                or "",
                "completed_objects": sorted(completed_objects),
                "id_mapping_count": (checkpoint_summary or {}).get("id_mapping_count")
                or (local_index or {}).get("id_mapping_count")
                or 0,
                "stage_available": bool(
                    (checkpoint_summary or {}).get("stage_available")
                ),
                "credentials_required": bool(
                    (checkpoint_summary or {}).get("credentials_required")
                ),
                "last_error": (local_index or {}).get("last_error") or "",
            }
            if checkpoint_summary or local_index
            else None
        ),
    }
