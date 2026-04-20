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
        from controllers import extraction_controller, transformation_controller, loading_controller
        from services.http_clients.salesforce_http_client import salesforce_client
        from services.http_clients.servicenow_http_client import servicenow_client
        from config.settings import (
            EXTRACTION_JOB_NUMBER,
            TRANSFORMATION_JOB_NUMBER,
            LOADING_JOB_NUMBER,
            ATTACHMENT_MAPPING_PRIORITY,
        )

        selected_objects: list[str] = config.get("selected_objects", [])
        options: dict = config.get("options", {})
        source_type: str = config.get("source_type", "servicenow")
        source_credentials: dict = config.get("source_credentials", {})
        destination_credentials: dict = config.get("destination_credentials", {})
        dry_run: bool = bool(options.get("disable_load", False))
        token = state.token

        # Configure destination client with credentials from this session.
        salesforce_client.configure(
            base_url=destination_credentials.get("base_url"),
            client_id=destination_credentials.get("client_id"),
            client_secret=destination_credentials.get("client_secret"),
            api_version=destination_credentials.get("api_version"),
        )

        # Configure the source client with credentials from this session.
        if source_type == "servicenow":
            servicenow_client.configure(
                base_url=source_credentials.get("base_url"),
                username=source_credentials.get("username"),
                password=source_credentials.get("password"),
            )

        # Build an ordered priority list containing only selected objects
        from config.settings import MAPPING_PRIORITY
        ordered: list[list[str]] = []
        for group in MAPPING_PRIORITY + ATTACHMENT_MAPPING_PRIORITY:
            filtered = [name for name in group if name in selected_objects]
            if filtered:
                ordered.append(filtered)

        total_groups = len(ordered)

        # Cumulative record counts
        total_fetched = 0
        total_processed = 0
        total_failed = 0

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

        # Perform actual extraction (all selected tables at once)
        total_fetched = extraction_controller.start_extraction(EXTRACTION_JOB_NUMBER, flat)

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

            load_result = loading_controller.start_loading(LOADING_JOB_NUMBER, group)
            total_processed += load_result.get("records_processed", 0)
            total_failed += load_result.get("records_failed", 0)

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
            ),
        )


# Module-level singleton
migration_manager = MigrationManager()
