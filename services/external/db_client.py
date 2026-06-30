from concurrent.futures import Future
import sqlite3
from pathlib import Path
from typing import Optional, Iterable
from config.settings import (
    EXTRACTION_JOB_NUMBER,
    LOADING_JOB_NUMBER,
    PATH_TO_DATA,
    TRANSFORMATION_JOB_NUMBER,
)
from queue import Queue
from threading import Thread


class DBClient:
    def __init__(
        self,
    ):
        self.db_path = PATH_TO_DATA / "database" / "db" / "data.db"
        self.schema_dir = PATH_TO_DATA / "database" / "sql" / "schema"
        self._init_db()
        self._run_migrations()

    def _connect(self):
        conn = sqlite3.connect(
            self.db_path,
            timeout=60,
            check_same_thread=False
        )
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self):
        """
        Initialize DB by executing all .sql files in schema directory
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        sql_files = sorted(self.schema_dir.glob("*.sql"))

        if not sql_files:
            raise RuntimeError(f"No SQL schema files found in {self.schema_dir}")

        with self._connect() as conn:
            for sql_file in sql_files:
                sql = sql_file.read_text(encoding="utf-8")
                conn.executescript(sql)
            conn.commit()

    # -------------------------
    # MIGRATIONS
    # -------------------------

    def _run_migrations(self):
        """Run incremental SQLite-safe migrations"""
        with self._connect() as conn:
            self._add_batch_operation_column(conn)
            self._add_results_column(conn)
            self._add_batch_salesforce_job_id_column(conn)
            self._seed_mapping_configs(conn)
            conn.commit()

    def _seed_mapping_configs(self, conn: sqlite3.Connection):
        """Seed mapping_configs from the JSON files shipped in PATH_TO_MAPPING.

        Runs only once: when the mapping_configs table is empty. Each JSON
        file becomes a system mapping (is_system=1). The priority is derived
        from the position of the object in MAPPING_PRIORITY +
        ATTACHMENT_MAPPING_PRIORITY so the original migration ordering is
        preserved. Seeding is self-contained (reads files directly) to avoid
        importing higher-level services from this low-level DB layer.
        """
        import json
        from datetime import datetime, timezone
        from config.settings import (
            PATH_TO_MAPPING,
            MAPPING_PRIORITY,
            ATTACHMENT_MAPPING_PRIORITY,
        )

        row = conn.execute("SELECT COUNT(*) FROM mapping_configs").fetchone()
        if row and row[0] > 0:
            return

        # Build a flat name -> priority index from the priority groups.
        priority_map: dict[str, int] = {}
        for idx, group in enumerate(MAPPING_PRIORITY + ATTACHMENT_MAPPING_PRIORITY):
            for name in group:
                priority_map.setdefault(name, idx)

        mapping_dir = PATH_TO_MAPPING
        if not mapping_dir.exists():
            return

        now = datetime.now(timezone.utc).isoformat()
        rows: list[tuple] = []
        for json_file in sorted(mapping_dir.glob("*.json")):
            name = json_file.stem
            try:
                config_text = json_file.read_text(encoding="utf-8")
                config = json.loads(config_text) if config_text.strip() else {}
            except Exception:
                continue
            # Priority precedence: explicit position in priority groups, then
            # the file's own "priority" key, then a large default.
            priority = priority_map.get(name, config.get("priority", 9999))
            rows.append((name, "servicenow", json.dumps(config), priority, 1, now, now))

        if rows:
            conn.executemany(
                """
                INSERT OR IGNORE INTO mapping_configs
                    (name, source_type, config, priority, is_system, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def _add_batch_operation_column(self, conn: sqlite3.Connection):
        """
        Adds batch_operation column to job_table_batches if it does not exist
        """
        cursor = conn.execute("PRAGMA table_info(job_table_batches)")
        columns = {row[1] for row in cursor.fetchall()}

        if "batch_operation" not in columns:
            conn.execute(
                """
                ALTER TABLE job_table_batches
                ADD COLUMN batch_operation TEXT
            """
            )

            query = "UPDATE job_table_batches set batch_operation='extraction' WHERE job_table_id IN (SELECT id FROM job_tables WHERE job_id=?)"
            conn.execute(query, (EXTRACTION_JOB_NUMBER,))

            query = "UPDATE job_table_batches set batch_operation='transformation' WHERE job_table_id IN (SELECT id FROM job_tables WHERE job_id=?)"
            conn.execute(query, (TRANSFORMATION_JOB_NUMBER,))

            query = "UPDATE job_table_batches set batch_operation='insert' WHERE job_table_id IN (SELECT id FROM job_tables WHERE job_id=?)"
            conn.execute(query, (LOADING_JOB_NUMBER,))
    def _add_results_column(self, conn: sqlite3.Connection):
        """
        Adds batch_operation column to job_table_batches if it does not exist
        """
        cursor = conn.execute("PRAGMA table_info(job_table_batches)")
        columns = {row[1] for row in cursor.fetchall()}

        if "results" not in columns:
            conn.execute(
                """
                ALTER TABLE job_table_batches
                ADD COLUMN results TEXT DEFAULT ''
            """
            )

    def _add_batch_salesforce_job_id_column(self, conn: sqlite3.Connection):
        """
        Adds batch_salesforce_job_id column to job_table_batches if it does not exist
        """
        cursor = conn.execute("PRAGMA table_info(job_table_batches)")
        columns = {row[1] for row in cursor.fetchall()}

        if "salesforce_job_id" not in columns:
            conn.execute(
                """
                ALTER TABLE job_table_batches
                ADD COLUMN salesforce_job_id TEXT  DEFAULT '';
            """
            )

    # -------------------------
    # DATA METHODS
    # -------------------------

    def bulk_upsert(
        self,
        table_name: str,
        rows: list[dict],
        conflict_cols: list[str],
        ignore_cols: set[str] = {"id", "created_at"},
    ):
        if not rows:
            return

        columns = rows[0].keys()
        column_list = ", ".join(columns)
        placeholders = ", ".join(f":{col}" for col in columns)

        update_cols = [
            c for c in columns
            if c not in conflict_cols and c not in ignore_cols
        ]

        if not update_cols:
            raise ValueError("No columns available for update")

        update_clause = ", ".join(
            f"{col} = excluded.{col}" for col in update_cols
        )

        conflict_target = ", ".join(conflict_cols)

        sql = f"""
            INSERT INTO {table_name} ({column_list})
            VALUES ({placeholders})
            ON CONFLICT ({conflict_target})
            DO UPDATE SET {update_clause};
        """

        with self._connect() as conn:
            conn.executemany(sql, rows)
            conn.commit()

    def insert(self, sql: str, params: tuple = ()):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            inserted_id = cursor.lastrowid
            conn.commit()
            return inserted_id

    def fetch_one(self, query: str, params: tuple = ()):
        with self._connect() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchone()

    def fetch_all(self, query: str, params: tuple = ()):
        with self._connect() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall()

    def execute(self, sql: str, params: tuple = ()):
        """Generic executor (for admin/debug use)"""
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.fetchall()


class SQLiteWriter:
    def __init__(self, db_client: DBClient):
        self.queue = Queue()
        self.db_client = db_client
        self.thread = Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        conn = self.db_client._connect()
        cur = conn.cursor()

        while True:
            item = self.queue.get()
            if item is None:
                break

            sql, params, future = item

            try:
                cur.execute(sql, params)
                conn.commit()

                future.set_result({
                    "lastrowid": cur.lastrowid,
                    "rowcount": cur.rowcount,
                })
            except Exception as e:
                future.set_exception(e)
            finally:
                self.queue.task_done()

        conn.close()
    
    def execute(self, sql, params):
        future = Future()
        self.queue.put((sql, params, future))
        return future


db_client = DBClient()
writer = SQLiteWriter(db_client)
