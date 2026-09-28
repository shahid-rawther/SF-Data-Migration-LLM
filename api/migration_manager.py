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
    load_errors: dict = field(default_factory=dict)
    # References loaded empty because no order could resolve them, each awaiting
    # a decision before the follow-up update runs.
    pending_backfill: list = field(default_factory=list)
    # Deferrals recorded by *earlier* sessions that this run has just made
    # fillable, by loading the object they were waiting on.
    unblocked_backfill: list = field(default_factory=list)

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
            "load_errors": self.load_errors,
            "pending_backfill": self.pending_backfill,
            "unblocked_backfill": self.unblocked_backfill,
        }


@dataclass
class SessionState:
    token: CancellationToken = field(default_factory=CancellationToken)
    thread: threading.Thread | None = None
    progress: ProgressEvent | None = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    loop: asyncio.AbstractEventLoop | None = None
    # Arguments for the most recent checkpoint, so pause and stop can persist
    # resume state without reaching into the runner's local variables.
    checkpoint_args: dict | None = None


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
        # A pause is the most likely point for the process to be shut down, so
        # the resume state is written before control returns to the caller.
        self._checkpoint_from_state(session_id, state)

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
        self._checkpoint_from_state(session_id, state)

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
            "load_errors": {},
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

    # ------------------------------------------------------------------
    # Checkpointing
    # ------------------------------------------------------------------

    def _checkpoint(self, session_id: str, state: SessionState, **kwargs) -> None:
        """Persist enough state to continue this session from where it is.

        Called wherever the amount of completed work has changed, so a session
        interrupted between two calls resumes from the earlier one instead of
        from the beginning. Storage failures are logged, never raised: losing a
        checkpoint slows a resume down, but failing the migration would lose the
        run outright.
        """
        from services.internal import checkpoint_service

        state.checkpoint_args = kwargs
        checkpoint_service.save_quietly(session_id, **kwargs)

    def _checkpoint_from_state(self, session_id: str, state: SessionState) -> None:
        """Re-save using the last known arguments, for pause and stop."""
        if not state.checkpoint_args:
            return
        from services.internal import checkpoint_service

        checkpoint_service.save_quietly(session_id, **state.checkpoint_args)

    def _prepare_relationships(
        self,
        session_id: str,
        options: dict,
        source_type: str,
        selected_objects: list[str],
        source_credentials: dict,
        destination_credentials: dict,
    ) -> dict:
        """Resolve outstanding relationships, then gate the launch.

        The agent pass is optional and best-effort: if the model is unreachable
        the migration still proceeds to the gate, which is deterministic. The
        gate itself is not optional — :class:`~agents.preflight.PreflightBlocked`
        propagates and fails the session with the list of unresolved problems.
        """
        from agents import preflight
        from agents.config import load_settings
        from agents.context import MigrationContext

        settings = load_settings()
        summary: dict = {"order": [], "agent": None, "preflight": None}

        # One context for both passes: the describes it caches are the expensive
        # part, and after the agent applies its changes it holds the same configs
        # the gate needs to check.
        context = MigrationContext.load(
            source_type=source_type,
            mapping_names=selected_objects,
            source_credentials=source_credentials,
            destination_credentials=destination_credentials,
        )

        autowire = options.get("agent_autowire")
        if autowire is None:
            autowire = settings.autowire_enabled

        if autowire and settings.has_credentials:
            self._emit(
                session_id,
                ProgressEvent(
                    session_id=session_id,
                    phase="preparation",
                    object_name="",
                    batch_current=0,
                    batch_total=len(selected_objects),
                    records_fetched=0,
                    records_processed=0,
                    records_failed=0,
                    overall_percent=0.0,
                    status="running",
                    message="Resolving relationships across the selected objects",
                ),
            )
            try:
                from agents import relationship_agent

                report = relationship_agent.analyze(
                    source_type=source_type,
                    mapping_names=selected_objects,
                    source_credentials=source_credentials,
                    destination_credentials=destination_credentials,
                    session_id=session_id,
                    settings=settings,
                    context=context,
                )
                summary["agent"] = report.to_dict()
                logger.info(
                    "Relationship agent run %s — %d change(s), %d blocker(s) left%s",
                    report.run_id,
                    len(report.changes),
                    len(report.remaining_blockers),
                    f", error: {report.error}" if report.error else "",
                )
            except Exception as exc:  # noqa: BLE001 — the gate below is the real check
                logger.warning(
                    "Relationship agent pass failed (non-blocking) — continuing to "
                    "the launch gate: %s",
                    exc,
                )

        gate = preflight.enforce(
            source_type=source_type,
            mapping_names=selected_objects,
            source_credentials=source_credentials,
            destination_credentials=destination_credentials,
            settings=settings,
            context=context,
        )
        summary["preflight"] = gate.to_dict()
        if gate.deferred:
            logger.warning("[migration] %s", gate.deferred_notice())
        if not gate.has_cycle:
            summary["order"] = gate.order
        return summary

    def _repair_failed_objects(
        self,
        session_id: str,
        options: dict,
        source_type: str,
        group: list[str],
        all_objects: list[str],
        source_credentials: dict,
        destination_credentials: dict,
        destination_plugin,
        load_result: dict,
    ) -> dict:
        """Diagnose each failed object, patch its mapping, and retry it.

        Retrying re-transforms from the staged extract, so the source is never
        touched again. Rows that already succeeded carry an id mapping and are
        skipped by the transform, which is what makes a retry cost only the rows
        that failed.

        Returns the load result with the retried objects' counts folded in.
        """
        from services.internal import jobs_service, workspace_service
        from services.internal.mappings_service import (
            cfg_destination_object,
            get_mapping_config,
        )
        from tools.cache import execution_cache
        from controllers import transformation_controller

        from agents.config import load_settings

        settings = load_settings()
        enabled = options.get("agent_repair")
        if enabled is None:
            enabled = settings.repair_enabled
        if not enabled or not settings.has_credentials:
            return load_result

        by_object = dict(load_result.get("by_object") or {})
        errors_by_object = dict(load_result.get("errors_by_object") or {})

        # start_loading reports per destination object (or transformation name);
        # the agent works in mapping names, so map one to the other.
        mapping_for_result_key: dict[str, str] = {}
        for name in group:
            config = get_mapping_config(name)
            if not config:
                continue
            key = config.get("transformation_name") or cfg_destination_object(config)
            if key:
                mapping_for_result_key[key] = name

        attempts_log: list[dict] = []

        for result_key, counts in list(by_object.items()):
            mapping_name = mapping_for_result_key.get(result_key)
            if not mapping_name or counts.get("failed", 0) <= 0:
                continue

            for attempt in range(1, settings.repair_max_attempts + 1):
                self._emit(
                    session_id,
                    ProgressEvent(
                        session_id=session_id,
                        phase="repair",
                        object_name=mapping_name,
                        batch_current=attempt,
                        batch_total=settings.repair_max_attempts,
                        records_fetched=0,
                        records_processed=counts.get("processed", 0),
                        records_failed=counts.get("failed", 0),
                        overall_percent=0.0,
                        status="running",
                        message=(
                            f"Diagnosing {counts.get('failed', 0):,} rejected "
                            f"{mapping_name} row(s) (attempt {attempt})"
                        ),
                        records_by_object=by_object,
                        load_errors=errors_by_object,
                    ),
                )
                try:
                    from agents import repair_agent

                    report = repair_agent.repair(
                        source_type=source_type,
                        mapping_name=mapping_name,
                        error_messages=errors_by_object.get(result_key, []),
                        source_credentials=source_credentials,
                        destination_credentials=destination_credentials,
                        session_id=session_id,
                        related_mappings=all_objects,
                        settings=settings,
                    )
                except Exception as exc:  # noqa: BLE001 — a failed repair is not a failed migration
                    logger.warning(
                        "Repair agent failed for %s (non-blocking): %s", mapping_name, exc
                    )
                    break

                attempts_log.append(
                    {"mapping": mapping_name, "attempt": attempt, **report.to_dict()}
                )
                logger.info(
                    "Repair attempt %d for %s — %d change(s), retry=%s: %s",
                    attempt,
                    mapping_name,
                    len(report.changes),
                    report.should_retry,
                    report.summary or report.error,
                )
                if not report.should_retry:
                    break

                try:
                    workspace_service.clean_transformed_state([mapping_name])
                    execution_cache.clear()
                    retransform = transformation_controller.start_trasformation(
                        jobs_service.create_new_job("transformation"), [mapping_name]
                    )
                    retry_result = destination_plugin.start_loading(
                        jobs_service.create_new_job("loading"), [mapping_name]
                    )
                except Exception as exc:  # noqa: BLE001 — keep the original failure visible
                    logger.warning(
                        "Retry of %s after repair failed (non-blocking): %s",
                        mapping_name,
                        exc,
                    )
                    break

                # A repair can fix the load error and still leave the row behind,
                # by making a transform that now discards it. That has to stay in
                # the failed count, or the retry looks like a clean success.
                retransform_dropped = sum(
                    int(entry.get("rows_dropped") or 0)
                    for entry in (
                        (retransform or {}).get("discarded_by_object") or {}
                    ).values()
                )
                retry_counts = (retry_result.get("by_object") or {}).get(result_key, {})
                counts = {
                    "processed": counts.get("processed", 0)
                    + retry_counts.get("processed", 0),
                    "failed": retry_counts.get("failed", 0) + retransform_dropped,
                }
                by_object[result_key] = counts
                retry_errors = (retry_result.get("errors_by_object") or {}).get(
                    result_key, []
                )
                if retry_errors:
                    errors_by_object[result_key] = retry_errors
                else:
                    errors_by_object.pop(result_key, None)
                execution_cache.clear()

                logger.info(
                    "Retry of %s after repair — %d processed, %d still failing",
                    mapping_name,
                    counts["processed"],
                    counts["failed"],
                )
                if counts["failed"] == 0:
                    break

            if counts.get("failed", 0) > 0 and options.get("agent_diagnosis", True):
                self._diagnose_remaining(
                    session_id=session_id,
                    mapping_name=mapping_name,
                    all_objects=all_objects,
                    source_type=source_type,
                    source_credentials=source_credentials,
                    destination_credentials=destination_credentials,
                    error_messages=errors_by_object.get(result_key, []),
                    counts=counts,
                    by_object=by_object,
                    errors_by_object=errors_by_object,
                    settings=settings,
                )

        return {
            **load_result,
            "records_processed": sum(c.get("processed", 0) for c in by_object.values()),
            "records_failed": sum(c.get("failed", 0) for c in by_object.values()),
            "by_object": by_object,
            "errors_by_object": errors_by_object,
            "repair_attempts": attempts_log,
        }

    def _diagnose_remaining(
        self,
        *,
        session_id: str,
        mapping_name: str,
        all_objects: list[str],
        source_type: str,
        source_credentials: dict,
        destination_credentials: dict,
        error_messages: list[str],
        counts: dict,
        by_object: dict,
        errors_by_object: dict,
        settings,
    ) -> None:
        """Explain rows the repairs could not fix, for the person to act on.

        Never blocks or fails the run: a diagnosis is advice, and the assistant
        can still be asked about the failure if this does not produce one.
        """
        self._emit(
            session_id,
            ProgressEvent(
                session_id=session_id,
                phase="repair",
                object_name=mapping_name,
                batch_current=0,
                batch_total=0,
                records_fetched=0,
                records_processed=counts.get("processed", 0),
                records_failed=counts.get("failed", 0),
                overall_percent=0.0,
                status="running",
                message=(
                    f"Automatic repair could not fix {counts.get('failed', 0):,} "
                    f"{mapping_name} row(s); asking the agent to diagnose why"
                ),
                records_by_object=by_object,
                load_errors=errors_by_object,
            ),
        )
        try:
            from agents import diagnosis_agent, repair_agent
            from agents.context import MigrationContext

            context = MigrationContext.load(
                source_type=source_type,
                mapping_names=[mapping_name]
                + [name for name in all_objects if name != mapping_name],
                source_credentials=source_credentials,
                destination_credentials=destination_credentials,
            )
            digest = repair_agent.build_digest(context, mapping_name, error_messages)
            if digest.is_empty:
                return
            diagnosis = diagnosis_agent.diagnose(
                context, mapping_name, digest, session_id=session_id, settings=settings
            )
        except Exception as exc:  # noqa: BLE001 — a failed diagnosis is not a failed migration
            logger.warning("Diagnosis failed for %s (non-blocking): %s", mapping_name, exc)
            return

        self._emit(
            session_id,
            ProgressEvent(
                session_id=session_id,
                phase="repair",
                object_name=mapping_name,
                batch_current=0,
                batch_total=0,
                records_fetched=0,
                records_processed=counts.get("processed", 0),
                records_failed=counts.get("failed", 0),
                overall_percent=0.0,
                status="running",
                message=(
                    f"Diagnosis ready for {mapping_name}: "
                    f"{len(diagnosis.suggestions)} suggested fix(es). See the "
                    "assistant panel to review or apply them."
                ),
                records_by_object=by_object,
                load_errors=errors_by_object,
            ),
        )

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
        from pathlib import Path
        _dbg = Path("_migration_debug.log")
        _dbg.write_text(f"[{datetime.now(timezone.utc).isoformat()}] Migration started: {session_id}\n", encoding="utf-8")
        try:
            self._do_run(session_id, config, state)
            with open(_dbg, "a") as f: f.write(f"[{datetime.now(timezone.utc).isoformat()}] Migration completed successfully\n")
        except Exception as exc:
            with open(_dbg, "a") as f: f.write(f"[{datetime.now(timezone.utc).isoformat()}] Migration FAILED: {exc}\n")
            logger.exception("Migration session %s failed: %s", session_id, exc)
            session_db.mark_session_failed(session_id)
            # Whatever loaded before the failure is still in the destination, so
            # the checkpoint is refreshed here to keep the retry incremental.
            self._checkpoint_from_state(session_id, state)
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
        from services.internal import jobs_service
        from tools.cache import execution_cache

        execution_cache.clear()

        extraction_job_number = jobs_service.create_new_job("extraction")
        transformation_job_number = jobs_service.create_new_job("transformation")
        loading_job_number = jobs_service.create_new_job("loading")
        job_ids = [
            extraction_job_number,
            transformation_job_number,
            loading_job_number,
        ]

        selected_objects: list[str] = config.get("selected_objects", [])
        options: dict = config.get("options", {})
        source_type: str = config.get("source_type", "servicenow")
        destination_type: str = config.get("destination_type", "salesforce")
        source_credentials: dict = config.get("source_credentials", {})
        destination_credentials: dict = config.get("destination_credentials", {})
        dry_run: bool = bool(options.get("disable_load", False))
        # Caps rows pulled **per source table**, not per migration: the selected
        # mappings' source tables are extracted independently, so N tables at a
        # limit of 100 stages 100 rows each. Extraction also resumes past what is
        # already in id_mappings, so a second run takes the next 100 rather than
        # the same ones.
        record_limit: int | None = options.get("service_now_batch_size") or None
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

        # ── Agent layer: resolve relationships, then gate the launch ────────────
        # Runs before extraction so the mapping configs the rest of this method
        # obeys are the resolved ones, and so a run that cannot succeed is
        # stopped before it spends hours pulling data.
        agent_preparation = self._prepare_relationships(
            session_id=session_id,
            options=options,
            source_type=source_type,
            selected_objects=selected_objects,
            source_credentials=source_credentials,
            destination_credentials=destination_credentials,
        )

        ordered = _build_schema_ordered_groups(
            selected_objects,
            destination_credentials,
            precomputed_order=agent_preparation.get("order") or None,
        )
        logger.info(
            "Execution order resolved — %d group(s): %s",
            len(ordered),
            [g[0] for g in ordered],
        )

        # Objects a previous run of this session finished. Their rows are already
        # in the destination and their ids are already in id_mappings, so a
        # resume skips their transform and load entirely while children can
        # still resolve lookups against them.
        already_completed: list[str] = list(config.get("completed_objects") or [])
        if already_completed:
            ordered = [
                [name for name in group if name not in already_completed]
                for group in ordered
            ]
            ordered = [group for group in ordered if group]
            logger.info(
                "Resuming session %s — skipping %d object(s) completed earlier: %s",
                session_id,
                len(already_completed),
                already_completed,
            )

        resolved_order = [name for group in ordered for name in group]
        total_groups = len(ordered)

        # Cumulative record counts
        total_fetched = 0
        total_processed = 0
        total_failed = 0
        records_by_object: dict[str, dict[str, int]] = {}
        load_errors_by_object: dict[str, list[str]] = {}
        completed_objects: list[str] = list(already_completed)

        skip_extraction = config.get("skip_extraction", False)
        flat = [name for group in ordered for name in group]

        def checkpoint(phase: str) -> None:
            """Write resume state for wherever the run has got to."""
            self._checkpoint(
                session_id,
                state,
                progress={
                    "phase": phase,
                    "records_fetched": total_fetched,
                    "records_processed": total_processed,
                    "records_failed": total_failed,
                },
                order=resolved_order,
                completed_objects=completed_objects,
                records_by_object=records_by_object,
                load_errors=load_errors_by_object,
                job_ids=job_ids,
                destination_type=destination_type,
            )

        # Clear stale on-disk working state so a new run never picks up files from
        # a previous one. When extraction is being skipped the staged extract is
        # the whole point of the retry, so only the derived output is cleared.
        from services.internal import staging_service, workspace_service
        if skip_extraction:
            workspace_service.clean_transformed_state(flat)
        else:
            workspace_service.clean_working_state(flat, source_type=source_type)

        # ── Phase 1: Extraction into the local stage ─────────────────
        # Every selected object is pulled and written to disk here. Transform and
        # load below read only from that staged copy — never from the source — so
        # a retry or a repair never re-queries the source system.
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

        if skip_extraction:
            total_fetched = self._count_extracted_records(flat, source_type)
            logger.info(
                "Skipping extraction — reusing the staged extract (%d record(s))",
                total_fetched,
            )
        else:
            extract_start_date = options.get("start_date", "") or None
            extract_end_date = options.get("end_date", "") or None
            total_fetched = extraction_controller.start_extraction(
                extraction_job_number,
                flat,
                source_plugin=source_plugin,
                record_limit=record_limit,
                start_date=extract_start_date,
                end_date=extract_end_date,
            )

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
                    extraction_job_number, selected_attachment_names, source_plugin=source_plugin
                )

        # The staging boundary: refuse to transform what was never staged.
        manifest = staging_service.verify(flat, source_type=source_type)
        staging_service.write_manifest(manifest, extraction_job_number)
        if not total_fetched:
            total_fetched = manifest.total_rows

        # The first checkpoint worth having: from here the staged extract exists
        # on disk, so a resume can skip straight to transform and load.
        checkpoint("extraction")

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
                message=(
                    f"Staged {manifest.total_rows:,} record(s) across "
                    f"{len(manifest.tables)} table(s)"
                ),
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

            transform_result = _run_with_retry(
                transformation_controller.start_trasformation,
                transformation_job_number,
                group,
                what=f"transformation of {', '.join(group)}",
            )
            logger.info(f"[Migration] Transformation complete for group: {group}")

            # A row dropped here never reaches the destination, so the load
            # counters cannot see it. Counting it as failed is what stops a run
            # that discarded every record from reporting a clean finish.
            discarded = (transform_result or {}).get("discarded_by_object") or {}
            for object_name, entry in discarded.items():
                dropped = int(entry.get("rows_dropped") or 0)
                if not dropped:
                    continue
                total_failed += dropped
                counts = records_by_object.setdefault(object_name, {})
                counts["discarded"] = counts.get("discarded", 0) + dropped
                counts["failed"] = counts.get("failed", 0) + dropped
                reasons = ", ".join(
                    f"{count} {reason.replace('_', ' ')}"
                    for reason, count in sorted(entry.get("by_reason", {}).items())
                )
                detail = (
                    f"{dropped:,} {object_name} row(s) were discarded during "
                    f"transformation and will not be loaded"
                    + (f" — {reasons}" if reasons else "")
                    + ". See the *_errors.csv for this batch."
                )
                logger.warning("[Migration] %s", detail)
                load_errors_by_object.setdefault(object_name, []).append(detail)
                self._emit(
                    session_id,
                    ProgressEvent(
                        session_id=session_id,
                        phase="transformation",
                        object_name=object_name,
                        batch_current=group_idx,
                        batch_total=total_groups,
                        records_fetched=total_fetched,
                        records_processed=total_processed,
                        records_failed=total_failed,
                        overall_percent=overall,
                        status="running",
                        message=detail,
                        records_by_object=records_by_object,
                        load_errors=load_errors_by_object,
                    ),
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
                loading_job_number,
                group,
                what=f"loading of {', '.join(group)}",
            )

            if load_result.get("records_failed", 0) > 0:
                load_result = self._repair_failed_objects(
                    session_id=session_id,
                    options=options,
                    source_type=source_type,
                    group=group,
                    all_objects=flat,
                    source_credentials=source_credentials,
                    destination_credentials=destination_credentials,
                    destination_plugin=destination_plugin,
                    load_result=load_result,
                )

            total_processed += load_result.get("records_processed", 0)
            total_failed += load_result.get("records_failed", 0)
            for obj_name, counts in load_result.get("by_object", {}).items():
                records_by_object[obj_name] = counts
            for obj_name, errs in load_result.get("errors_by_object", {}).items():
                load_errors_by_object[obj_name] = errs

            # Invalidate cached id_mappings so subsequent priority groups
            # see the records that were just loaded into Salesforce.
            execution_cache.clear()

            # Only an object with nothing left failing is treated as done; one
            # that still has rejected rows is replayed on resume so the repair
            # agent gets another attempt at them.
            if load_result.get("records_failed", 0) == 0:
                completed_objects.extend(
                    name for name in group if name not in completed_objects
                )
            checkpoint("loading")

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
                    load_errors=load_errors_by_object,
                ),
            )

            if token.is_cancelled():
                return

        # ── Deferred references: recorded, never filled automatically ────
        # A lookup caught in a reference cycle was loaded empty on purpose,
        # because no order would have resolved it. Now that every object is in,
        # those rows can be attached — but that is an update against records
        # already in the destination, so it is offered rather than done.
        pending_backfill: list[dict] = []
        unblocked_backfill: list[dict] = []
        if not dry_run:
            try:
                from services.internal import backfill_service

                pending_backfill = backfill_service.scan(session_id, flat)
                # Loading an object is what ends someone else's wait, and that
                # someone is usually an earlier session that cannot notice on
                # its own. Checked after the scan so this run's own units are
                # already current and only older ones can be reported.
                unblocked_backfill = backfill_service.newly_fillable(
                    exclude_session=session_id
                )
            except Exception as exc:  # noqa: BLE001 — never fail a finished run
                logger.warning(
                    "Could not work out what was deferred for backfill: %s", exc
                )

        had_errors = (not dry_run) and total_failed > 0
        final_msg = (
            f"Dry run completed — {total_fetched:,} fetched, {total_processed:,} transformed"
            if dry_run
            else f"Migration completed — {total_fetched:,} fetched, {total_processed:,} processed, {total_failed:,} failed"
        )
        if had_errors:
            final_msg += " (completed with errors)"
        outstanding = [u for u in pending_backfill if u["status"] == "pending"]
        if outstanding:
            fields = ", ".join(
                f"{u['destination_object']}.{u['field']}" for u in outstanding[:3]
            )
            total_rows = sum(u["rows_deferred"] for u in outstanding)
            final_msg += (
                f" · {len(outstanding)} reference{'' if len(outstanding) == 1 else 's'} "
                f"left to attach on {total_rows:,} record(s) ({fields}"
                f"{' and others' if len(outstanding) > 3 else ''}) — "
                "review and approve the backfill to fill them"
            )
        if unblocked_backfill:
            where = ", ".join(
                f"{u['destination_object']}.{u['field']}" for u in unblocked_backfill[:3]
            )
            ready = sum(u["rows_resolvable"] for u in unblocked_backfill)
            final_msg += (
                f" · this run also unblocked {len(unblocked_backfill)} earlier "
                f"deferral{'' if len(unblocked_backfill) == 1 else 's'} "
                f"({where}{' and others' if len(unblocked_backfill) > 3 else ''}) — "
                f"{ready:,} record(s) can now be attached"
            )
        final_phase = "dry_run" if dry_run else "loading"

        session_db.mark_session_completed(session_id, had_errors=had_errors)
        # Saved after the status is written, so the checkpoint records the run as
        # finished and a later resume has nothing left to do.
        checkpoint(final_phase)
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
                load_errors=load_errors_by_object,
                pending_backfill=outstanding,
                unblocked_backfill=unblocked_backfill,
            ),
        )


# ── Schema-based ordering ──────────────────────────────────────────────────────

def _build_schema_ordered_groups(
    selected_objects: list[str],
    destination_credentials: dict,
    precomputed_order: list[str] | None = None,
) -> list[list[str]]:
    """Determine execution order from live Salesforce reference-field metadata.

    Uses ``resolve_dependency_order`` to topologically sort the selected
    mappings based on their destination object relationships.  Falls back
    to the wizard card order (``selected_objects``) when the schema
    resolution fails, detects a cycle, or returns an empty result.

    ``precomputed_order`` is the order the launch gate already derived from the
    same sort; passing it through avoids describing every destination object
    twice for one run.
    """
    if precomputed_order:
        logger.info("Using load order from the launch gate: %s", precomputed_order)
        return [[name] for name in precomputed_order]

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
