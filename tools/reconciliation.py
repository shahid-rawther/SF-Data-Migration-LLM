"""
Migration reconciliation tracker.

Records every data modification made during transformation so that admins
can review what was changed and why.  Thread-safe for use with the
concurrent transformation batches.
"""

import csv
import threading
from pathlib import Path
from typing import Optional

from config.settings import PATH_TO_RESULTS


class ReconciliationTracker:
    _FIELDNAMES = [
        "object",
        "record_id",
        "field",
        "original_value",
        "migrated_value",
        "reason",
    ]

    def __init__(self) -> None:
        self._rows: list[dict] = []
        self._lock = threading.Lock()

    def record(
        self,
        sf_object: str,
        record_id: str,
        field: str,
        original_value: str,
        migrated_value: str,
        reason: str,
    ) -> None:
        with self._lock:
            self._rows.append(
                {
                    "object": sf_object,
                    "record_id": record_id,
                    "field": field,
                    "original_value": str(original_value)[:200],
                    "migrated_value": str(migrated_value)[:200],
                    "reason": reason,
                }
            )

    def flush(self, object_name: Optional[str] = None) -> Path | None:
        """Write collected entries to a CSV and clear the buffer.

        Returns the path to the report, or None if there was nothing to write.
        """
        with self._lock:
            if not self._rows:
                return None
            rows_to_write = list(self._rows)
            self._rows.clear()

        report_dir = PATH_TO_RESULTS / "_reconciliation"
        report_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{object_name}_reconciliation.csv" if object_name else "reconciliation.csv"
        report_path = report_dir / filename

        file_exists = report_path.exists() and report_path.stat().st_size > 0
        with open(report_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self._FIELDNAMES, quoting=csv.QUOTE_ALL)
            if not file_exists:
                writer.writeheader()
            writer.writerows(rows_to_write)

        return report_path

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()


reconciliation_tracker = ReconciliationTracker()
