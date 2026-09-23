"""Durable, resumable migration sessions backed by S3.

What has to survive to continue a migration is not the data — it is the record
of what has already been written to the destination. The pipeline is already
built so that a re-run is idempotent given that record:

* ``transformation_service`` skips any source row that already has an entry in
  ``id_mappings``, so an object that loaded 400 of 1,000 rows re-transforms only
  the remaining 600;
* ``job_table_batches`` marks batches that completed, so a resumed run does not
  redo finished work;
* the mapping configs decide how every field is written, including the lookups
  the relationship agent rewired — resuming against different configs would
  produce rows inconsistent with the ones already loaded.

So a checkpoint is those tables plus the progress counters, and nothing else.
The staged source extract under ``data/downloads`` is deliberately excluded: it
is large, it is reproducible from the source, and only the machine running the
transform needs it. A checkpoint records a fingerprint of that stage instead, so
a resume can tell whether the local copy still matches what the checkpoint was
taken against, and re-extract only when it does not.

Credentials are stripped before anything leaves the process. A checkpoint
restored onto a new machine therefore needs its connections re-selected, which
is reported as ``credentials_required``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from common.logger import logger
from services.external.db_client import db_client
from services.external.s3_client import S3Client, S3Error, S3Unavailable, load_settings

CHECKPOINT_VERSION = 1

# Values in the session config that must never reach the bucket.
_SECRET_KEYS = {
    "password",
    "client_secret",
    "secret",
    "access_token",
    "refresh_token",
    "api_key",
    "api_token",
    "private_key",
    "security_token",
}
_REDACTED = "***"

_ID_MAPPING_COLUMNS = ("table_name", "servicenow_id", "salesforce_id", "hash_key")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CheckpointError(RuntimeError):
    """A checkpoint could not be built, saved or restored."""


# ── keys ───────────────────────────────────────────────────────────────────────


def session_prefix(client: S3Client, session_id: str) -> str:
    return client.key("sessions", session_id)


def checkpoint_key(client: S3Client, session_id: str) -> str:
    return f"{session_prefix(client, session_id)}/checkpoint.json"


def id_mappings_key(client: S3Client, session_id: str) -> str:
    return f"{session_prefix(client, session_id)}/id_mappings.jsonl.gz"


# ── redaction ──────────────────────────────────────────────────────────────────


def _redact(value: Any) -> Any:
    """Copy a structure with anything secret-looking replaced."""
    if isinstance(value, dict):
        return {
            k: (_REDACTED if k.lower() in _SECRET_KEYS and v else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _has_redaction(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (k.lower() in _SECRET_KEYS and v == _REDACTED) or _has_redaction(v)
            for k, v in value.items()
        )
    if isinstance(value, list):
        return any(_has_redaction(item) for item in value)
    return False


# ── the state a checkpoint covers ──────────────────────────────────────────────


def _session_row(session_id: str) -> dict:
    row = db_client.fetch_one(
        "SELECT id, source_type, selected_objects, config, status, phase, "
        "started_at, updated_at, completed_at FROM migration_sessions WHERE id = ?",
        (session_id,),
    )
    if not row:
        raise CheckpointError(f"No migration session with id {session_id}")
    keys = (
        "id",
        "source_type",
        "selected_objects",
        "config",
        "status",
        "phase",
        "started_at",
        "updated_at",
        "completed_at",
    )
    entry = dict(zip(keys, row))
    entry["selected_objects"] = json.loads(entry["selected_objects"] or "[]")
    entry["config"] = json.loads(entry["config"] or "{}")
    return entry


def id_mapping_tables(selected_objects: list[str]) -> list[str]:
    """The ``id_mappings.table_name`` values these mappings write or read.

    Scoping the checkpoint this way keeps one session's resume state independent
    of every other session's, so restoring an old session cannot resurrect ids
    that belong to a different run.
    """
    from services.internal.mappings_service import (
        cfg_destination_object,
        cfg_source_tables,
        get_id_mapping_table_name,
        get_mapping_config,
    )

    names: list[str] = []
    for name in selected_objects:
        config = get_mapping_config(name)
        if not config:
            continue
        tables = cfg_source_tables(config)
        if not tables:
            continue
        own = get_id_mapping_table_name(
            tables[0],
            cfg_destination_object(config),
            config.get("transformation_name"),
        )
        if own and own not in names:
            names.append(own)
        # Lookups resolve parents through other mappings' id tables; a resume
        # that lost those would rewrite children with empty references.
        for lookup in config.get("look_up_table_names") or []:
            if lookup and lookup not in names:
                names.append(lookup)
        for entry in config.get("fields") or []:
            params = entry.get("transformation_params") or {}
            for lookup in params.get("lookup_tables") or []:
                if lookup and lookup not in names:
                    names.append(lookup)
    return names


def _fetch_id_mappings(tables: list[str]) -> list[dict]:
    if not tables:
        return []
    placeholders = ",".join("?" for _ in tables)
    rows = db_client.fetch_all(
        f"SELECT {', '.join(_ID_MAPPING_COLUMNS)} FROM id_mappings "
        f"WHERE table_name IN ({placeholders})",
        tuple(tables),
    )
    return [dict(zip(_ID_MAPPING_COLUMNS, row)) for row in rows]


def _fetch_mapping_configs(selected_objects: list[str]) -> list[dict]:
    """The resolved configs, exactly as the run obeyed them."""
    if not selected_objects:
        return []
    placeholders = ",".join("?" for _ in selected_objects)
    rows = db_client.fetch_all(
        f"SELECT name, source_type, config, priority, is_system FROM mapping_configs "
        f"WHERE name IN ({placeholders})",
        tuple(selected_objects),
    )
    keys = ("name", "source_type", "config", "priority", "is_system")
    entries: list[dict] = []
    for row in rows:
        entry = dict(zip(keys, row))
        try:
            entry["config"] = json.loads(entry["config"] or "{}")
        except json.JSONDecodeError:
            entry["config"] = {}
        entries.append(entry)
    return entries


def _fetch_jobs(job_ids: list[int]) -> dict:
    """Batch-level progress for the jobs belonging to this session."""
    if not job_ids:
        return {"jobs": [], "job_tables": [], "batches": [], "sub_batches": []}
    placeholders = ",".join("?" for _ in job_ids)
    params = tuple(job_ids)

    jobs = [
        dict(zip(("id", "job_type", "status", "created_at"), row))
        for row in db_client.fetch_all(
            f"SELECT id, job_type, status, created_at FROM jobs WHERE id IN ({placeholders})",
            params,
        )
    ]
    job_tables = [
        dict(zip(("id", "job_id", "table_name", "status", "created_at"), row))
        for row in db_client.fetch_all(
            "SELECT id, job_id, table_name, status, created_at FROM job_tables "
            f"WHERE job_id IN ({placeholders})",
            params,
        )
    ]
    table_ids = [t["id"] for t in job_tables]
    batches: list[dict] = []
    sub_batches: list[dict] = []
    if table_ids:
        tph = ",".join("?" for _ in table_ids)
        batches = [
            dict(
                zip(
                    (
                        "id",
                        "job_table_id",
                        "batch_size",
                        "batch_number",
                        "status",
                        "created_at",
                    ),
                    row,
                )
            )
            for row in db_client.fetch_all(
                "SELECT id, job_table_id, batch_size, batch_number, status, created_at "
                f"FROM job_table_batches WHERE job_table_id IN ({tph})",
                tuple(table_ids),
            )
        ]
        batch_ids = [b["id"] for b in batches]
        if batch_ids:
            bph = ",".join("?" for _ in batch_ids)
            sub_batches = [
                dict(
                    zip(
                        (
                            "id",
                            "job_table_batch_id",
                            "batch_size",
                            "batch_number",
                            "status",
                            "created_at",
                        ),
                        row,
                    )
                )
                for row in db_client.fetch_all(
                    "SELECT id, job_table_batch_id, batch_size, batch_number, status, "
                    f"created_at FROM job_table_sub_batches WHERE job_table_batch_id IN ({bph})",
                    tuple(batch_ids),
                )
            ]
    return {
        "jobs": jobs,
        "job_tables": job_tables,
        "batches": batches,
        "sub_batches": sub_batches,
    }


def _stage_fingerprint(selected_objects: list[str], source_type: str) -> dict:
    """Describe the local staged extract without carrying it.

    Row and byte counts per table are enough to tell, on resume, whether the
    local stage is the same one the checkpoint was taken against.
    """
    from services.internal import staging_service

    try:
        manifest = staging_service.inspect(
            selected_objects, source_type=source_type, count_rows=True
        )
    except Exception as exc:  # noqa: BLE001 — a checkpoint must not fail over this
        logger.warning("[checkpoint] could not inspect the local stage: %s", exc)
        return {"available": False, "error": str(exc), "tables": []}
    return {
        "available": True,
        "total_rows": manifest.total_rows,
        "tables": [
            {
                "table": t.table,
                "files": t.files,
                "rows": t.rows,
                "bytes": t.bytes,
                "is_driving": t.is_driving,
            }
            for t in manifest.tables
        ],
    }


# ── the checkpoint document ────────────────────────────────────────────────────


@dataclass
class Checkpoint:
    session_id: str
    sequence: int = 0
    saved_at: str = ""
    status: str = ""
    phase: str = ""
    source_type: str = ""
    destination_type: str = "salesforce"
    selected_objects: list[str] = field(default_factory=list)
    options: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    # Load order actually used, so a resume replays the same sequence rather
    # than re-deriving one from a schema that may have changed since.
    order: list[str] = field(default_factory=list)
    completed_objects: list[str] = field(default_factory=list)
    progress: dict = field(default_factory=dict)
    records_by_object: dict = field(default_factory=dict)
    load_errors: dict = field(default_factory=dict)
    stage: dict = field(default_factory=dict)
    mapping_configs: list[dict] = field(default_factory=list)
    agent_changes: list[dict] = field(default_factory=list)
    jobs: dict = field(default_factory=dict)
    job_ids: list[int] = field(default_factory=list)
    id_mapping_tables: list[str] = field(default_factory=list)
    id_mapping_count: int = 0
    id_mappings_key: str = ""

    def to_dict(self) -> dict:
        return {
            "version": CHECKPOINT_VERSION,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "saved_at": self.saved_at,
            "status": self.status,
            "phase": self.phase,
            "source_type": self.source_type,
            "destination_type": self.destination_type,
            "selected_objects": self.selected_objects,
            "options": self.options,
            "config": self.config,
            "order": self.order,
            "completed_objects": self.completed_objects,
            "progress": self.progress,
            "records_by_object": self.records_by_object,
            "load_errors": self.load_errors,
            "stage": self.stage,
            "mapping_configs": self.mapping_configs,
            "agent_changes": self.agent_changes,
            "jobs": self.jobs,
            "job_ids": self.job_ids,
            "id_mapping_tables": self.id_mapping_tables,
            "id_mapping_count": self.id_mapping_count,
            "id_mappings_key": self.id_mappings_key,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Checkpoint":
        return cls(
            session_id=payload.get("session_id", ""),
            sequence=int(payload.get("sequence") or 0),
            saved_at=payload.get("saved_at", ""),
            status=payload.get("status", ""),
            phase=payload.get("phase", ""),
            source_type=payload.get("source_type", ""),
            destination_type=payload.get("destination_type", "salesforce"),
            selected_objects=payload.get("selected_objects") or [],
            options=payload.get("options") or {},
            config=payload.get("config") or {},
            order=payload.get("order") or [],
            completed_objects=payload.get("completed_objects") or [],
            progress=payload.get("progress") or {},
            records_by_object=payload.get("records_by_object") or {},
            load_errors=payload.get("load_errors") or {},
            stage=payload.get("stage") or {},
            mapping_configs=payload.get("mapping_configs") or [],
            agent_changes=payload.get("agent_changes") or [],
            jobs=payload.get("jobs") or {},
            job_ids=payload.get("job_ids") or [],
            id_mapping_tables=payload.get("id_mapping_tables") or [],
            id_mapping_count=int(payload.get("id_mapping_count") or 0),
            id_mappings_key=payload.get("id_mappings_key", ""),
        )

    @property
    def credentials_required(self) -> bool:
        return _has_redaction(self.config)

    def summary(self) -> dict:
        """The short form the UI needs to describe resume state."""
        return {
            "session_id": self.session_id,
            "sequence": self.sequence,
            "saved_at": self.saved_at,
            "status": self.status,
            "phase": self.phase,
            "source_type": self.source_type,
            "selected_objects": self.selected_objects,
            "completed_objects": self.completed_objects,
            "remaining_objects": [
                name
                for name in (self.order or self.selected_objects)
                if name not in self.completed_objects
            ],
            "progress": self.progress,
            "records_by_object": self.records_by_object,
            "id_mapping_count": self.id_mapping_count,
            "stage_available": bool(self.stage.get("available")),
            "stage_rows": self.stage.get("total_rows", 0),
            "credentials_required": self.credentials_required,
        }


# ── local index ────────────────────────────────────────────────────────────────

_INDEX_COLUMNS = (
    "session_id",
    "sequence",
    "phase",
    "status",
    "bucket",
    "checkpoint_key",
    "id_mappings_key",
    "version_id",
    "records_fetched",
    "records_processed",
    "records_failed",
    "id_mapping_count",
    "completed_objects",
    "last_error",
    "saved_at",
    "updated_at",
)


def _record_index(row: dict) -> None:
    db_client.bulk_upsert(
        "session_checkpoints",
        [row],
        ["session_id"],
        ignore_cols=set(),
    )


def get_index(session_id: str) -> dict | None:
    row = db_client.fetch_one(
        f"SELECT {', '.join(_INDEX_COLUMNS)} FROM session_checkpoints WHERE session_id = ?",
        (session_id,),
    )
    if not row:
        return None
    entry = dict(zip(_INDEX_COLUMNS, row))
    try:
        entry["completed_objects"] = json.loads(entry["completed_objects"] or "[]")
    except json.JSONDecodeError:
        entry["completed_objects"] = []
    return entry


def last_sequence(session_id: str) -> int:
    index = get_index(session_id)
    return int(index["sequence"]) if index else 0


# ── build / save ───────────────────────────────────────────────────────────────


def build(
    session_id: str,
    *,
    progress: dict | None = None,
    order: list[str] | None = None,
    completed_objects: list[str] | None = None,
    records_by_object: dict | None = None,
    load_errors: dict | None = None,
    job_ids: list[int] | None = None,
    destination_type: str = "salesforce",
) -> tuple[Checkpoint, list[dict]]:
    """Collect everything needed to continue this session.

    The id map is returned alongside the document rather than inside it, because
    it is stored as a separate object.
    """
    session = _session_row(session_id)
    config = session["config"] or {}
    selected = session["selected_objects"] or config.get("selected_objects") or []
    source_type = session["source_type"] or config.get("source_type") or "servicenow"

    tables = id_mapping_tables(selected)
    mappings = _fetch_id_mappings(tables)

    from agents import audit

    try:
        agent_changes = audit.list_changes(session_id=session_id)
    except Exception as exc:  # noqa: BLE001 — audit is not worth failing a save
        logger.warning("[checkpoint] could not read the agent journal: %s", exc)
        agent_changes = []

    checkpoint = Checkpoint(
        session_id=session_id,
        sequence=last_sequence(session_id) + 1,
        saved_at=_now(),
        status=session["status"] or "",
        phase=session["phase"] or "",
        source_type=source_type,
        destination_type=destination_type,
        selected_objects=selected,
        options=config.get("options") or {},
        config=_redact(config),
        order=order or [],
        completed_objects=completed_objects or [],
        progress=progress or {},
        records_by_object=records_by_object or {},
        load_errors=load_errors or {},
        stage=_stage_fingerprint(selected, source_type),
        mapping_configs=_fetch_mapping_configs(selected),
        agent_changes=agent_changes,
        jobs=_fetch_jobs(job_ids or []),
        job_ids=job_ids or [],
        id_mapping_tables=tables,
        id_mapping_count=len(mappings),
    )
    return checkpoint, mappings


def save(
    session_id: str,
    *,
    progress: dict | None = None,
    order: list[str] | None = None,
    completed_objects: list[str] | None = None,
    records_by_object: dict | None = None,
    load_errors: dict | None = None,
    job_ids: list[int] | None = None,
    destination_type: str = "salesforce",
    client: S3Client | None = None,
) -> dict:
    """Write a checkpoint for this session to S3 and index it locally.

    The id map goes to its own gzipped object because it grows with the number
    of records migrated; the checkpoint document itself stays small enough to
    read and diff by hand.
    """
    checkpoint, mappings = build(
        session_id,
        progress=progress,
        order=order,
        completed_objects=completed_objects,
        records_by_object=records_by_object,
        load_errors=load_errors,
        job_ids=job_ids,
        destination_type=destination_type,
    )
    s3 = client or S3Client()
    mappings_key = id_mappings_key(s3, session_id)
    checkpoint.id_mappings_key = mappings_key

    # The id map first: a checkpoint that references rows it cannot fetch would
    # be worse than one written a moment later.
    s3.put_jsonl_gzip(mappings_key, mappings)
    written = s3.put_json(checkpoint_key(s3, session_id), checkpoint.to_dict())

    counters = checkpoint.progress or {}
    _record_index(
        {
            "session_id": session_id,
            "sequence": checkpoint.sequence,
            "phase": checkpoint.phase,
            "status": checkpoint.status,
            "bucket": s3.settings.bucket,
            "checkpoint_key": written["key"],
            "id_mappings_key": mappings_key,
            "version_id": written.get("version_id", ""),
            "records_fetched": int(counters.get("records_fetched") or 0),
            "records_processed": int(counters.get("records_processed") or 0),
            "records_failed": int(counters.get("records_failed") or 0),
            "id_mapping_count": checkpoint.id_mapping_count,
            "completed_objects": json.dumps(checkpoint.completed_objects),
            "last_error": "",
            "saved_at": checkpoint.saved_at,
            "updated_at": _now(),
        }
    )
    logger.info(
        "[checkpoint] saved session %s seq=%d phase=%s — %d id mapping(s), %d completed object(s)",
        session_id,
        checkpoint.sequence,
        checkpoint.phase,
        checkpoint.id_mapping_count,
        len(checkpoint.completed_objects),
    )
    return {
        **checkpoint.summary(),
        "bucket": s3.settings.bucket,
        "checkpoint_key": written["key"],
        "id_mappings_key": mappings_key,
        "version_id": written.get("version_id", ""),
    }


def save_quietly(session_id: str, **kwargs) -> dict | None:
    """Save without ever failing the caller.

    A migration is not worth aborting because the bucket was briefly
    unreachable, but a failed save must still be visible, so the reason is
    recorded in the local index.
    """
    try:
        return save(session_id, **kwargs)
    except S3Unavailable as exc:
        logger.debug("[checkpoint] not configured, skipping save: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 — storage must never fail a migration
        logger.warning("[checkpoint] could not save session %s: %s", session_id, exc)
        try:
            existing = get_index(session_id) or {}
            _record_index(
                {
                    "session_id": session_id,
                    "sequence": int(existing.get("sequence") or 0),
                    "phase": str(existing.get("phase") or ""),
                    "status": str(existing.get("status") or ""),
                    "bucket": str(existing.get("bucket") or ""),
                    "checkpoint_key": str(existing.get("checkpoint_key") or ""),
                    "id_mappings_key": str(existing.get("id_mappings_key") or ""),
                    "version_id": str(existing.get("version_id") or ""),
                    "records_fetched": int(existing.get("records_fetched") or 0),
                    "records_processed": int(existing.get("records_processed") or 0),
                    "records_failed": int(existing.get("records_failed") or 0),
                    "id_mapping_count": int(existing.get("id_mapping_count") or 0),
                    "completed_objects": json.dumps(
                        existing.get("completed_objects") or []
                    ),
                    "last_error": str(exc)[:500],
                    "saved_at": str(existing.get("saved_at") or ""),
                    "updated_at": _now(),
                }
            )
        except Exception:  # noqa: BLE001 — indexing a failure must not raise
            pass
        return None


# ── load / restore ─────────────────────────────────────────────────────────────


def load(session_id: str, client: S3Client | None = None) -> Checkpoint | None:
    """Read the latest checkpoint for a session, or None when there is none."""
    s3 = client or S3Client()
    payload = s3.get_json(checkpoint_key(s3, session_id))
    if not payload:
        return None
    version = int(payload.get("version") or 0)
    if version > CHECKPOINT_VERSION:
        raise CheckpointError(
            f"Checkpoint for {session_id} is version {version}, but this build "
            f"understands up to {CHECKPOINT_VERSION}. Upgrade before resuming."
        )
    return Checkpoint.from_dict(payload)


def list_saved(client: S3Client | None = None) -> list[dict]:
    """Summarise every session held in the bucket.

    This is what makes a checkpoint useful after the local database is gone: the
    bucket, not the instance, is the record of what has been migrated.
    """
    s3 = client or S3Client()
    summaries: list[dict] = []
    for session_id in s3.list_common_prefixes(s3.key("sessions")):
        try:
            checkpoint = load(session_id, client=s3)
        except (S3Error, CheckpointError) as exc:
            summaries.append({"session_id": session_id, "error": str(exc)})
            continue
        if checkpoint:
            summaries.append(checkpoint.summary())
    summaries.sort(key=lambda entry: entry.get("saved_at") or "", reverse=True)
    return summaries


def restore(
    session_id: str,
    *,
    client: S3Client | None = None,
    restore_mapping_configs: bool = True,
) -> dict:
    """Write a checkpoint's state back into the local database.

    Safe to run against a database that already holds the session: the id map
    and the job rows are upserted, so restoring is idempotent, and locally held
    credentials are never overwritten with the redacted copy from the bucket.
    """
    s3 = client or S3Client()
    checkpoint = load(session_id, client=s3)
    if not checkpoint:
        raise CheckpointError(f"No checkpoint in the bucket for session {session_id}")

    mappings = s3.get_jsonl_gzip(
        checkpoint.id_mappings_key or id_mappings_key(s3, session_id)
    )
    restored_ids = 0
    if mappings:
        rows = [
            {
                "table_name": row.get("table_name", ""),
                "servicenow_id": row.get("servicenow_id", ""),
                "salesforce_id": row.get("salesforce_id", ""),
                "hash_key": row.get("hash_key", "") or "",
            }
            for row in mappings
            if row.get("table_name") and row.get("servicenow_id")
        ]
        if rows:
            db_client.bulk_upsert(
                "id_mappings", rows, ["table_name", "servicenow_id"]
            )
            restored_ids = len(rows)

    session_created = _restore_session_row(checkpoint)
    configs_restored = (
        _restore_mapping_configs(checkpoint) if restore_mapping_configs else 0
    )
    jobs_restored = _restore_jobs(checkpoint)

    _record_index(
        {
            "session_id": session_id,
            "sequence": checkpoint.sequence,
            "phase": checkpoint.phase,
            "status": checkpoint.status,
            "bucket": s3.settings.bucket,
            "checkpoint_key": checkpoint_key(s3, session_id),
            "id_mappings_key": checkpoint.id_mappings_key,
            "version_id": "",
            "records_fetched": int(checkpoint.progress.get("records_fetched") or 0),
            "records_processed": int(checkpoint.progress.get("records_processed") or 0),
            "records_failed": int(checkpoint.progress.get("records_failed") or 0),
            "id_mapping_count": checkpoint.id_mapping_count,
            "completed_objects": json.dumps(checkpoint.completed_objects),
            "last_error": "",
            "saved_at": checkpoint.saved_at,
            "updated_at": _now(),
        }
    )

    # Whether this machine can continue without re-extracting.
    stage = _stage_fingerprint(checkpoint.selected_objects, checkpoint.source_type)
    stage_matches = _stage_matches(checkpoint.stage, stage)

    logger.info(
        "[checkpoint] restored session %s seq=%d — %d id mapping(s), %d config(s), "
        "stage %s",
        session_id,
        checkpoint.sequence,
        restored_ids,
        configs_restored,
        "reusable" if stage_matches else "must be re-extracted",
    )
    return {
        **checkpoint.summary(),
        "session_created": session_created,
        "id_mappings_restored": restored_ids,
        "mapping_configs_restored": configs_restored,
        "job_rows_restored": jobs_restored,
        "local_stage": stage,
        "stage_reusable": stage_matches,
        "skip_extraction": stage_matches,
    }


def _restore_session_row(checkpoint: Checkpoint) -> bool:
    """Create the session locally if absent; never clobber local credentials."""
    existing = db_client.fetch_one(
        "SELECT config FROM migration_sessions WHERE id = ?", (checkpoint.session_id,)
    )
    if existing:
        db_client.execute(
            "UPDATE migration_sessions SET status=?, phase=?, updated_at=? WHERE id=?",
            (
                checkpoint.status or "paused",
                checkpoint.phase or "",
                _now(),
                checkpoint.session_id,
            ),
        )
        return False

    db_client.insert(
        "INSERT INTO migration_sessions (id, source_type, selected_objects, config, "
        "status, phase, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            checkpoint.session_id,
            checkpoint.source_type,
            json.dumps(checkpoint.selected_objects),
            json.dumps(checkpoint.config),
            checkpoint.status or "paused",
            checkpoint.phase or "",
            _now(),
        ),
    )
    return True


def _restore_mapping_configs(checkpoint: Checkpoint) -> int:
    """Reinstate the configs the run obeyed, including agent-applied rewiring."""
    rows = [
        {
            "name": entry.get("name", ""),
            "source_type": entry.get("source_type") or checkpoint.source_type,
            "config": json.dumps(entry.get("config") or {}),
            "priority": int(entry.get("priority") or 9999),
            "is_system": int(entry.get("is_system") or 0),
            "updated_at": _now(),
        }
        for entry in checkpoint.mapping_configs
        if entry.get("name")
    ]
    if not rows:
        return 0
    db_client.bulk_upsert("mapping_configs", rows, ["name"], ignore_cols={"id", "created_at"})
    return len(rows)


def _restore_jobs(checkpoint: Checkpoint) -> int:
    """Reinstate batch-level progress, preserving the original row ids.

    Ids are kept because ``job_tables`` and ``job_table_batches`` reference each
    other by id; renumbering them would silently detach the batch history from
    its table.
    """
    jobs = checkpoint.jobs or {}
    total = 0
    for table, rows, conflict in (
        ("jobs", jobs.get("jobs") or [], ["id"]),
        ("job_tables", jobs.get("job_tables") or [], ["id"]),
        ("job_table_batches", jobs.get("batches") or [], ["id"]),
        ("job_table_sub_batches", jobs.get("sub_batches") or [], ["id"]),
    ):
        if not rows:
            continue
        db_client.bulk_upsert(table, rows, conflict, ignore_cols=set())
        total += len(rows)
    return total


def _stage_matches(recorded: dict, current: dict) -> bool:
    """Whether the local extract is the one the checkpoint was taken against.

    Row counts per driving table are compared rather than file hashes: the
    extract is written in size-bounded batches whose filenames already encode
    their row counts, so a differing count is the signal that matters and a
    cheap comparison is enough to decide between reusing and re-extracting.
    """
    if not recorded.get("available") or not current.get("available"):
        return False
    recorded_rows = {
        t["table"]: t.get("rows", 0)
        for t in recorded.get("tables") or []
        if t.get("is_driving") and t.get("rows", 0) > 0
    }
    # Nothing was staged when the checkpoint was taken, so there is nothing to
    # reuse. Without this an empty stage compares equal to an empty stage and a
    # resume would skip extraction with no data to transform.
    if not recorded_rows:
        return False
    current_rows = {t["table"]: t.get("rows", 0) for t in current.get("tables") or []}
    return all(
        current_rows.get(table, 0) >= rows for table, rows in recorded_rows.items()
    )


def delete(session_id: str, client: S3Client | None = None) -> dict:
    """Remove a session's checkpoint from the bucket and the local index."""
    s3 = client or S3Client()
    deleted = s3.delete_prefix(session_prefix(s3, session_id))
    db_client.execute(
        "DELETE FROM session_checkpoints WHERE session_id = ?", (session_id,)
    )
    return {"session_id": session_id, "objects_deleted": deleted}


def status() -> dict:
    """Whether checkpointing is usable, for the UI and for diagnostics."""
    settings = load_settings()
    report: dict[str, Any] = {**settings.redacted(), "reachable": False, "error": ""}
    if not settings.configured:
        report["error"] = (
            "No bucket configured. Set MIGRATION_S3_BUCKET to enable resumable "
            "sessions."
        )
        return report
    try:
        S3Client(settings)
        report["reachable"] = True
    except S3Error as exc:
        report["error"] = str(exc)
    return report
