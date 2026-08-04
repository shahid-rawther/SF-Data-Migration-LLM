"""The staging boundary between extraction and everything downstream.

The pipeline is staged on purpose: all selected source data is pulled and written
to disk first, and transform and load then read only from that copy. That is what
makes a failed load re-runnable without touching the source again, and what lets
the repair agent re-transform an object from exactly the bytes that were migrated.

This module makes the boundary explicit rather than implied. It verifies that
every selected mapping's driving table really was staged before transform starts —
so a missing extract is a clear failure at the boundary instead of a
FileNotFoundError from inside a worker thread — and records a manifest of what
was staged for that run.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from common.logger import logger
from config.settings import PATH_TO_DATA, PATH_TO_DOWNLOADS
from services.internal.mappings_service import cfg_source_tables, get_mapping_config


class StagingIncomplete(RuntimeError):
    """Selected data was not staged, so transform and load must not start."""


@dataclass
class StagedTable:
    table: str
    path: str
    files: int = 0
    rows: int = 0
    bytes: int = 0
    is_driving: bool = True
    required_by: list[str] = field(default_factory=list)

    @property
    def is_present(self) -> bool:
        return self.files > 0

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "path": self.path,
            "files": self.files,
            "rows": self.rows,
            "bytes": self.bytes,
            "is_driving": self.is_driving,
            "required_by": self.required_by,
        }


@dataclass
class StageManifest:
    source_type: str
    created_at: str
    tables: list[StagedTable] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(table.rows for table in self.tables)

    @property
    def missing_driving(self) -> list[StagedTable]:
        return [t for t in self.tables if t.is_driving and not t.is_present]

    @property
    def missing_joins(self) -> list[StagedTable]:
        return [t for t in self.tables if not t.is_driving and not t.is_present]

    def to_dict(self) -> dict:
        return {
            "source_type": self.source_type,
            "created_at": self.created_at,
            "total_rows": self.total_rows,
            "table_count": len(self.tables),
            "tables": [table.to_dict() for table in self.tables],
            "missing_driving_tables": [t.table for t in self.missing_driving],
            "missing_join_tables": [t.table for t in self.missing_joins],
        }

    def message(self) -> str:
        missing = ", ".join(
            f"{t.table} (needed by {', '.join(t.required_by)})" for t in self.missing_driving
        )
        return (
            "Extraction did not stage every selected object. No data was found for: "
            f"{missing}. Transform and load read only from the staged copy, so the run "
            "was stopped here. Check the source credentials, the object's filters and "
            "the date window, then re-run extraction."
        )


def _count_rows(csv_path: Path) -> int:
    """Data rows in an extracted CSV, excluding the header."""
    try:
        with csv_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            total = sum(1 for _ in handle)
    except OSError as exc:
        logger.warning("Could not read staged file %s: %s", csv_path, exc)
        return 0
    return max(total - 1, 0)


def inspect(
    selected_objects: list[str],
    source_type: str = "servicenow",
    count_rows: bool = True,
) -> StageManifest:
    """Describe what is currently staged for the given mappings."""
    root = PATH_TO_DOWNLOADS / source_type
    manifest = StageManifest(
        source_type=source_type, created_at=datetime.now(timezone.utc).isoformat()
    )
    seen: dict[str, StagedTable] = {}

    for name in selected_objects:
        config = get_mapping_config(name)
        if not config:
            continue
        for index, table in enumerate(cfg_source_tables(config)):
            entry = seen.get(table)
            if entry is None:
                directory = root / table
                entry = StagedTable(
                    table=table, path=str(directory), is_driving=index == 0
                )
                if directory.exists():
                    for csv_path in sorted(directory.glob("*.csv")):
                        entry.files += 1
                        entry.bytes += csv_path.stat().st_size
                        if count_rows:
                            entry.rows += _count_rows(csv_path)
                seen[table] = entry
                manifest.tables.append(entry)
            # A table that drives any mapping is required, even when another
            # mapping only joins to it.
            entry.is_driving = entry.is_driving or index == 0
            if name not in entry.required_by:
                entry.required_by.append(name)

    return manifest


def verify(
    selected_objects: list[str],
    source_type: str = "servicenow",
    count_rows: bool = True,
) -> StageManifest:
    """Assert the staged copy is complete enough to transform.

    Raises :class:`StagingIncomplete` when a driving table has no staged data.
    Missing join tables are logged rather than fatal: a mapping can legitimately
    join a table that has no rows in this window.
    """
    manifest = inspect(selected_objects, source_type=source_type, count_rows=count_rows)
    if manifest.missing_driving:
        raise StagingIncomplete(manifest.message())
    if manifest.missing_joins:
        logger.warning(
            "Staged extract has no data for join table(s): %s",
            ", ".join(t.table for t in manifest.missing_joins),
        )
    logger.info(
        "Staged extract verified — %d table(s), %s row(s) on disk",
        len(manifest.tables),
        f"{manifest.total_rows:,}" if count_rows else "uncounted",
    )
    return manifest


def write_manifest(manifest: StageManifest, job_id: int) -> Path:
    """Persist the manifest next to the staged data for audit."""
    path = PATH_TO_DATA / "staging" / f"job_{job_id}_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
    logger.info("Stage manifest written to %s", path)
    return path
