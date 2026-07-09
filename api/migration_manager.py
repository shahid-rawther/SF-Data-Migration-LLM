"""Migration session manager.

Manages the lifecycle of migration sessions: start, pause, resume, stop.
Each session runs in a background thread and publishes progress events that
SSE clients can subscribe to via asyncio queues.
"""
import asyncio
import json
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import dotenv
import requests

from api.cancellation_token import CancellationToken
from api import session_db
from common.logger import logger


# ── Transient-failure retry (EC-05) ─────────────────────────────────────────
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 2.0  # seconds; doubled each attempt
_TRANSIENT_HTTP_STATUS = {429, 500, 502, 503, 504}


def _is_transient_error(exc: Exception) -> bool:
    """Best-effort classification of retryable transient failures.

    Network blips and server-side 429/5xx responses are worth retrying; other
    errors (bad config, validation, auth misconfig) are fatal and re-raised.
    """
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code in _TRANSIENT_HTTP_STATUS
    return False


def _run_with_retry(fn, *args, what: str = "operation", **kwargs):
    """Run ``fn`` with bounded retry + exponential backoff on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= _RETRY_ATTEMPTS or not _is_transient_error(exc):
                raise
            delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "Transient failure during %s (attempt %d/%d): %s — retrying in %.1fs",
                what, attempt, _RETRY_ATTEMPTS, exc, delay,
            )
            time.sleep(delay)
    if last_exc:
        raise last_exc


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

        # Build execution order from Salesforce schema dependencies.
        # Falls back to wizard card order (selected_objects) when schema
        # resolution fails or detects a cycle.
        ordered = _build_schema_ordered_groups(
            selected_objects, destination_credentials,
        )
        logger.info(
            "Execution order resolved — %d group(s): %s",
            len(ordered),
            [g[0] for g in ordered],
        )

        total_groups = len(ordered)

        # Cumulative record counts
        total_fetched = 0
        total_processed = 0
        total_failed = 0
        records_by_object: dict[str, dict[str, int]] = {}

        # ── Phase 1: Extraction ──────────────────────────────────────
        session_db.update_session_status(session_id, "running", "extraction")
        flat = [name for group in ordered for name in group]

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

        # Perform actual extraction (all selected tables at once).
        # EC-26: pass the session's date range so extraction is actually scoped.
        extract_start_date = options.get("start_date", "") or None
        extract_end_date = options.get("end_date", "") or None
        total_fetched = extraction_controller.start_extraction(
            EXTRACTION_JOB_NUMBER,
            flat,
            source_plugin=source_plugin,
            start_date=extract_start_date,
            end_date=extract_end_date,
        )

        # Extract individual attachment files for any attachment objects.
        # Detection is data-driven: a mapping with is_attachment_table=true.
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

            _run_with_retry(
                transformation_controller.start_trasformation,
                TRANSFORMATION_JOB_NUMBER,
                group,
                what=f"transformation of {', '.join(group)}",
            )

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

            load_result = _run_with_retry(
                destination_plugin.start_loading,
                LOADING_JOB_NUMBER,
                group,
                what=f"loading of {', '.join(group)}",
            )
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

        had_errors = (not dry_run) and total_failed > 0
        final_msg = (
            f"Dry run completed — {total_fetched:,} fetched, {total_processed:,} transformed"
            if dry_run
            else f"Migration completed — {total_fetched:,} fetched, {total_processed:,} processed, {total_failed:,} failed"
        )
        if had_errors:
            final_msg += " (completed with errors)"
        final_phase = "dry_run" if dry_run else "loading"

        session_db.mark_session_completed(session_id, had_errors=had_errors)
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
                status="completed_with_errors" if had_errors else "completed",
                message=final_msg,
                records_by_object=records_by_object,
            ),
        )


# ── Schema-based ordering ──────────────────────────────────────────────────────

def _build_schema_ordered_groups(
    selected_objects: list[str],
    destination_credentials: dict,
) -> list[list[str]]:
    """Determine execution order from live Salesforce reference-field metadata.

    Uses ``resolve_dependency_order`` to topologically sort the selected
    mappings based on their destination object relationships.  Falls back
    to the wizard card order (``selected_objects``) when the schema
    resolution fails, detects a cycle, or returns an empty result.
    """
    try:
        from api.compat_engine import resolve_dependency_order

        dep_result = resolve_dependency_order(
            selected_objects, destination_credentials,
        )
        if not dep_result.get("has_cycle", False) and dep_result.get("order"):
            topo_order: list[str] = dep_result["order"]
            logger.info(
                "SF dependency order resolved: %s (edges: %d, warnings: %d)",
                topo_order,
                len(dep_result.get("edges", [])),
                len(dep_result.get("warnings", [])),
            )
            return [[name] for name in topo_order]

        if dep_result.get("has_cycle"):
            logger.warning(
                "Circular SF dependency detected — falling back to "
                "wizard card order"
            )
    except Exception as exc:
        logger.warning(
            "SF dependency resolution failed (non-blocking) — using "
            "wizard card order: %s",
            exc,
        )

    return [[name] for name in selected_objects]


# Module-level singleton
migration_manager = MigrationManager()
