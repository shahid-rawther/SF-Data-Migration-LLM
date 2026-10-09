-- Every write a readiness suggestion made to the source system. record_ids
-- holds the records that were changed, so a revert touches exactly those.
CREATE TABLE IF NOT EXISTS agent_source_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    readiness_id INTEGER,
    run_id TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    table_name TEXT NOT NULL,
    field_name TEXT NOT NULL DEFAULT '',
    value TEXT NOT NULL DEFAULT '',
    record_ids TEXT NOT NULL DEFAULT '[]',
    rationale TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reverted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_source_changes_readiness ON agent_source_changes (readiness_id);
