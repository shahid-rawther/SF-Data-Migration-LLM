"""Migration session manager.

Manages the lifecycle of migration sessions: start, pause, resume, stop.
Each session runs in a background thread and publishes progress events that
SSE clients can subscribe to via asyncio queues.
"""
import asyncio
import json
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import dotenv

from api.cancellation_token import CancellationToken
from api import session_db
from common.logger import logger


@dataclass
class ProgressEvent:
    session_id: str
    phase: str
    object_name: str
    batch_current: int
    batch_total: int
    records_fetched: int
    records_processed: int
    records_failed: int
    overall_percent: float
    status: str
    message: str = ""
    records_by_object: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "phase": self.phase,
            "object": self.object_name,
            "batch_current": self.batch_current,
            "batch_total": self.batch_total,
            "records_fetched": self.records_fetched,
            "records_processed": self.records_processed,
            "records_failed": self.records_failed,
            "overall_percent": self.overall_percent,
            "status": self.status,
            "message": self.message,
            "records_by_object": self.records_by_object,
        }


@dataclass
class SessionState:
    token: CancellationToken = field(default_factory=CancellationToken)
    thread: threading.Thread | None = None
    progress: ProgressEvent | None = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    loop: asyncio.AbstractEventLoop | None = None


class MigrationManager:
    """Singleton that tracks all active migration sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public control API
    # ------------------------------------------------------------------

    def start_migration(self, session_id: str, config: dict) -> None:
        """Start a migration session in a background thread."""
        with self._lock:
            if session_id in self._sessions:
                existing = self._sessions[session_id]
                if existing.thread and existing.thread.is_alive():
                    raise RuntimeError(f"Session {session_id} is already running")

            state = SessionState()
            try:
                state.loop = asyncio.get_event_loop()
            except RuntimeError:
                state.loop = None
            self._sessions[session_id] = state

        t = threading.Thread(
            target=self._run_migration,
            args=(session_id, config, state),
            daemon=True,
            name=f"migration-{session_id[:8]}",
        )
        state.thread = t
        session_db.mark_session_started(session_id)
        t.start()

    def pause_migration(self, session_id: str) -> None:
        state = self._get_state(session_id)
        state.token.pause()
        session_db.update_session_status(session_id, "paused")
        self._emit_status_change(session_id, "paused")

    def resume_migration(self, session_id: str) -> None:
        state = self._get_state(session_id)
        state.token.resume()
        session_db.update_session_status(session_id, "running")
        self._emit_status_change(session_id, "running")

    def stop_migration(self, session_id: str) -> None:
        state = self._get_state(session_id)
        state.token.cancel()
        session_db.update_session_status(session_id, "stopped")
        self._emit_status_change(session_id, "stopped")

    def get_status(self, session_id: str) -> dict | None:
        with self._lock:
            state = self._sessions.get(session_id)
        if not state:
            return None
        if state.progress:
            return state.progress.to_dict()
        db_session = session_db.get_session(session_id)
        if not db_session:
            return None
        return {
            "session_id": session_id,
            "phase": db_session.get("phase", ""),
            "object": "",
            "batch_current": 0,
            "batch_total": 0,
            "records_fetched": 0,
            "records_processed": 0,
            "records_failed": 0,
            "overall_percent": 0.0,
            "status": db_session.get("status", "pending"),
            "message": "",
            "records_by_object": {},
        }

    def is_active(self, session_id: str) -> bool:
        with self._lock:
            state = self._sessions.get(session_id)
        return bool(state and state.thread and state.thread.is_alive())

    def subscribe(self, session_id: str) -> asyncio.Queue:
        """Return a queue that will receive ProgressEvent dicts."""
        with self._lock:
            state = self._sessions.get(session_id)
            if not state:
                state = SessionState()
                self._sessions[session_id] = state
            q: asyncio.Queue = asyncio.Queue()
            state.subscribers.append(q)
            return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        with self._lock:
            state = self._sessions.get(session_id)
            if state and q in state.subscribers:
                state.subscribers.remove(q)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_state(self, session_id: str) -> SessionState:
        with self._lock:
            state = self._sessions.get(session_id)
        if not state:
            raise KeyError(f"Unknown session: {session_id}")
        return state

    def _emit(self, session_id: str, event: ProgressEvent) -> None:
        with self._lock:
            state = self._sessions.get(session_id)
        if not state:
            return
        state.progress = event
        payload = event.to_dict()
        for q in list(state.subscribers):
            if state.loop:
                try:
                    state.loop.call_soon_threadsafe(q.put_nowait, payload)
                except Exception:
                    pass
            else:
                try:
                    q.put_nowait(payload)
                except Exception:
                    pass

    def _emit_status_change(self, session_id: str, status: str) -> None:
        with self._lock:
            state = self._sessions.get(session_id)
        if not state or not state.progress:
            return
        updated = ProgressEvent(**{**state.progress.__dict__, "status": status})
        self._emit(session_id, updated)

    def _count_extracted_records(self, object_names: list[str], source_type: str) -> int:
        """Count records in already-extracted CSV files for a retry run."""
        import csv
        from config.settings import PATH_TO_DOWNLOADS
        from services.internal.mappings_service import get_mapping_config, cfg_source_tables

        downloads_path = PATH_TO_DOWNLOADS / source_type
        total = 0
        for name in object_names:
            mcfg = get_mapping_config(name)
            if not mcfg:
                continue
            tables = cfg_source_tables(mcfg)
            if not tables:
                continue
            table_dir = downloads_path / tables[0]
            if not table_dir.exists():
                continue
            for csv_file in table_dir.glob("*.csv"):
                with open(csv_file, newline="", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    next(reader, None)  # skip header
                    total += sum(1 for _ in reader)
        return total

    def _run_migration(self, session_id: str, config: dict, state: SessionState) -> None:
        """Main ETL runner executed in a background thread."""
        dotenv.load_dotenv()
        try:
            self._do_run(session_id, config, state)
        except Exception as exc:
            logger.exception("Migration session %s failed: %s", session_id, exc)
            session_db.mark_session_failed(session_id)
            err_event = ProgressEvent(
                session_id=session_id,
                phase=state.progress.phase if state.progress else "",
                object_name="",
                batch_current=0,
                batch_total=0,
                records_fetched=0,
                records_processed=0,
                records_failed=0,
                overall_percent=0.0,
                status="failed",
                message=str(exc),
            )
            self._emit(session_id, err_event)

    def _do_run(self, session_id: str, config: dict, state: SessionState) -> None:
        from controllers import extraction_controller, transformation_controller
        from api import sources as plugin_registry
        from config.settings import (
            EXTRACTION_JOB_NUMBER,
            TRANSFORMATION_JOB_NUMBER,
            LOADING_JOB_NUMBER,
        )
        from tools.cache import execution_cache

        execution_cache.clear()

        selected_objects: list[str] = config.get("selected_objects", [])
        options: dict = config.get("options", {})
        source_type: str = config.get("source_type", "servicenow")
        destination_type: str = config.get("destination_type", "salesforce")
        source_credentials: dict = config.get("source_credentials", {})
        destination_credentials: dict = config.get("destination_credentials", {})
        dry_run: bool = bool(options.get("disable_load", False))
        token = state.token

        # Resolve the source and destination plugins from the integration registry.
        source_plugin = plugin_registry.get(source_type)
        if source_plugin is None:
            raise ValueError(f"No source integration registered for '{source_type}'")
        destination_plugin = plugin_registry.get_destination(destination_type)
        if destination_plugin is None:
            raise ValueError(
                f"No destination integration registered for '{destination_type}'"
            )

        # Configure each plugin's client with credentials from this session.
        destination_plugin.configure_client(destination_credentials)
        source_plugin.configure_client(source_credentials)

        # ── Pre-flight: sync Product2 ↔ sc_cat_item ID mappings ─────────────────
        # ServiceNow-specific; only runs when source is ServiceNow.
        _PRODUCT_SYNC_OBJECTS = {"InsertCase", "ServiceRequest", "UpdateServiceRequestFinal"}
        if source_type == "servicenow" and any(obj in selected_objects for obj in _PRODUCT_SYNC_OBJECTS):
            try:
                from services.internal import mappings_service as _ms
                _result = _ms.sync_product_id_mappings(
                    sn_base=source_credentials.get("base_url", ""),
                    sn_user=source_credentials.get("username", ""),
                    sn_pass=source_credentials.get("password", ""),
                    sf_credentials=destination_credentials,
                )
                logger.info(
                    "Product mapping pre-flight sync complete: %d matched, "
                    "%d unmatched ServiceNow items",
                    _result["matched"], len(_result["unmatched_sn"]),
                )
            except Exception as _sync_err:
                logger.warning(
                    "Product mapping pre-flight sync failed (non-blocking) — "
                    "ProductId/Item__c may be blank: %s", _sync_err,
                )

        # Build an ordered priority list containing only selected objects.
        # Priority groups are derived from the mapping_configs DB table so that
        # user-created mappings (added via the Mapping Editor) participate in
        # ordering; falls back to the hardcoded settings groups when empty.
        from services.internal.mappings_service import get_priority_groups

        priority_groups = get_priority_groups(source_type)

        # Auto-include dependency objects required by the selected objects
        selected_objects = _resolve_dependencies(selected_objects, priority_groups, source_type)
        logger.info("Resolved dependencies — final object list: %s", selected_objects)

        ordered: list[list[str]] = []
        for group in priority_groups:
            filtered = [name for name in group if name in selected_objects]
            if filtered:
                ordered.append(filtered)

        total_groups = len(ordered)

        # Cumulative record counts
        total_fetched = 0
        total_processed = 0
        total_failed = 0
        records_by_object: dict[str, dict[str, int]] = {}

        skip_extraction = config.get("skip_extraction", False)
        flat = [name for group in ordered for name in group]

        # ── Phase 1: Extraction ──────────────────────────────────────
        if skip_extraction:
            logger.info("Retry mode — skipping extraction, counting existing files")
            total_fetched = self._count_extracted_records(flat, source_type)
            self._emit(
                session_id,
                ProgressEvent(
                    session_id=session_id,
                    phase="extraction",
                    object_name=", ".join(flat),
                    batch_current=len(flat),
                    batch_total=len(flat),
                    records_fetched=total_fetched,
                    records_processed=total_processed,
                    records_failed=total_failed,
                    overall_percent=round(100 / 3, 1),
                    status="running",
                    message=f"Extraction skipped (retry) — {total_fetched:,} records from previous run",
                ),
            )
        else:
            session_db.update_session_status(session_id, "running", "extraction")

            for idx, name in enumerate(flat):
                if not token.wait_if_paused():
                    return
                self._emit(
                    session_id,
                    ProgressEvent(
                        session_id=session_id,
                        phase="extraction",
                        object_name=name,
                        batch_current=idx,
                        batch_total=len(flat),
                        records_fetched=total_fetched,
                        records_processed=total_processed,
                        records_failed=total_failed,
                        overall_percent=round(idx / max(len(flat) * 3, 1) * 100, 1),
                        status="running",
                        message=f"Extracting {name}",
                    ),
                )

            # Perform actual extraction (all selected tables at once)
            total_fetched = extraction_controller.start_extraction(
                EXTRACTION_JOB_NUMBER, flat, source_plugin=source_plugin
            )

            # Extract individual attachment files for any attachment objects.
            from services.internal.mappings_service import get_mapping_config
            attachment_names = set()
            for n in flat:
                mcfg = get_mapping_config(n)
                if mcfg and mcfg.get("is_attachment_table"):
                    attachment_names.add(n)
            selected_attachment_names = [n for n in flat if n in attachment_names]
            if selected_attachment_names:
                logger.info("Extracting attachment files for: %s", selected_attachment_names)
                extraction_controller.start_attachment_file_extraction(
                    EXTRACTION_JOB_NUMBER, selected_attachment_names, source_plugin=source_plugin
                )

            self._emit(
                session_id,
                ProgressEvent(
                    session_id=session_id,
                    phase="extraction",
                    object_name=", ".join(flat),
                    batch_current=len(flat),
                    batch_total=len(flat),
                    records_fetched=total_fetched,
                    records_processed=total_processed,
                    records_failed=total_failed,
                    overall_percent=round(100 / 3, 1),
                    status="running",
                    message=f"Extraction complete — {total_fetched:,} records fetched",
                ),
            )

        if token.is_cancelled():
            return

        # ── Phase 2 & 3: Transform + Load (per priority group) ───────
        for group_idx, group in enumerate(ordered):
            if not token.wait_if_paused():
                return

            overall = round((1 + group_idx) / max(total_groups, 1) * 100, 1)

            session_db.update_session_status(session_id, "running", "transformation")
            self._emit(
                session_id,
                ProgressEvent(
                    session_id=session_id,
                    phase="transformation",
                    object_name=", ".join(group),
                    batch_current=group_idx,
                    batch_total=total_groups,
                    records_fetched=total_fetched,
                    records_processed=total_processed,
                    records_failed=total_failed,
                    overall_percent=overall,
                    status="running",
                    message=f"Transforming {', '.join(group)}",
                ),
            )

            transformation_controller.start_trasformation(TRANSFORMATION_JOB_NUMBER, group)

            if token.is_cancelled():
                return
            if not token.wait_if_paused():
                return

            if dry_run:
                # Dry-run: skip Salesforce upload, count transformed records only
                self._emit(
                    session_id,
                    ProgressEvent(
                        session_id=session_id,
                        phase="dry_run",
                        object_name=", ".join(group),
                        batch_current=group_idx + 1,
                        batch_total=total_groups,
                        records_fetched=total_fetched,
                        records_processed=total_processed,
                        records_failed=total_failed,
                        overall_percent=overall,
                        status="running",
                        message=f"[Dry Run] Skipping load for {', '.join(group)}",
                    ),
                )
                continue

            session_db.update_session_status(session_id, "running", "loading")
            self._emit(
                session_id,
                ProgressEvent(
                    session_id=session_id,
                    phase="loading",
                    object_name=", ".join(group),
                    batch_current=group_idx,
                    batch_total=total_groups,
                    records_fetched=total_fetched,
                    records_processed=total_processed,
                    records_failed=total_failed,
                    overall_percent=overall,
                    status="running",
                    message=f"Loading {', '.join(group)}",
                ),
            )

            load_result = destination_plugin.start_loading(LOADING_JOB_NUMBER, group)
            total_processed += load_result.get("records_processed", 0)
            total_failed += load_result.get("records_failed", 0)
            for obj_name, counts in load_result.get("by_object", {}).items():
                records_by_object[obj_name] = counts

            self._emit(
                session_id,
                ProgressEvent(
                    session_id=session_id,
                    phase="loading",
                    object_name=", ".join(group),
                    batch_current=group_idx + 1,
                    batch_total=total_groups,
                    records_fetched=total_fetched,
                    records_processed=total_processed,
                    records_failed=total_failed,
                    overall_percent=overall,
                    status="running",
                    message=f"Loaded {', '.join(group)} — {total_processed:,} processed, {total_failed:,} failed",
                    records_by_object=records_by_object,
                ),
            )

            if token.is_cancelled():
                return

        final_msg = (
            f"Dry run completed — {total_fetched:,} fetched, {total_processed:,} transformed"
            if dry_run
            else f"Migration completed — {total_fetched:,} fetched, {total_processed:,} processed, {total_failed:,} failed"
        )
        final_phase = "dry_run" if dry_run else "loading"

        session_db.mark_session_completed(session_id)
        self._emit(
            session_id,
            ProgressEvent(
                session_id=session_id,
                phase=final_phase,
                object_name="",
                batch_current=total_groups,
                batch_total=total_groups,
                records_fetched=total_fetched,
                records_processed=total_processed,
                records_failed=total_failed,
                overall_percent=100.0,
                status="completed",
                message=final_msg,
                records_by_object=records_by_object,
            ),
        )


# ── Dependency resolution ──────────────────────────────────────────────────────

# Per-source-type dependency maps.  The key is the downloaded folder name
# expected by transformation helpers; the value is the migration object whose
# extraction produces that folder.

_TABLE_DEPENDENCY_MAPS: dict[str, dict[str, str]] = {
    "servicenow": {
        "sys_user": "Account",
        "sys_user_group": "Group",
        "cmn_location": "ParentLocation",
        "cmn_department": "Department",
        "core_company": "Company",
    },
    "jsm": {
        "users": "JSMUser",
        "organizations": "JSMOrganization",
    },
}

# Which transformation_function names imply a dependency on which table
_FUNCTION_DEPENDENCY_MAPS: dict[str, dict[str, str]] = {
    "servicenow": {
        "look_up_user_sys_id": "sys_user",
        "look_up_user_user_name": "sys_user",
        "look_up_assignement_group": "sys_user_group",
        "look_up_location_sys_id": "cmn_location",
        "look_up_company_sys_id": "core_company",
    },
    "jsm": {
        "look_up_jsm_user": "users",
        "look_up_jsm_organization": "organizations",
    },
}

# Legacy flat aliases so any external code referencing the old names still works
_TABLE_DEPENDENCY_MAP = _TABLE_DEPENDENCY_MAPS["servicenow"]
_FUNCTION_DEPENDENCY_MAP = _FUNCTION_DEPENDENCY_MAPS["servicenow"]


def _resolve_dependencies(
    selected_objects: list[str],
    all_priority_groups: list[list[str]],
    source_type: str = "servicenow",
) -> list[str]:
    """Ensure all dependency objects are included and ordered correctly.

    Scans each selected object's mapping config (from DB first, JSON fallback)
    for transformation functions that require lookup tables from other objects.
    If those prerequisite objects are not already selected, they are
    automatically added.
    """
    from services.internal.mappings_service import get_mapping_config

    table_dep_map = _TABLE_DEPENDENCY_MAPS.get(source_type, {})
    func_dep_map = _FUNCTION_DEPENDENCY_MAPS.get(source_type, {})

    needed: set[str] = set(selected_objects)

    for obj_name in list(selected_objects):
        mapping = get_mapping_config(obj_name)
        if not mapping:
            continue
        for field_def in mapping.get("fields", []):
            func = field_def.get("transformation_function", "")
            if func in func_dep_map:
                table = func_dep_map[func]
                dep_object = table_dep_map.get(table)
                if dep_object and dep_object not in needed:
                    needed.add(dep_object)
                    logger.info(
                        "Auto-including '%s' as dependency for '%s' (requires %s)",
                        dep_object, obj_name, table,
                    )

    # Return in priority order
    all_flat = [name for group in all_priority_groups for name in group]
    ordered = [name for name in all_flat if name in needed]
    for name in needed:
        if name not in ordered:
            ordered.append(name)
    return ordered


# Module-level singleton
migration_manager = MigrationManager()
