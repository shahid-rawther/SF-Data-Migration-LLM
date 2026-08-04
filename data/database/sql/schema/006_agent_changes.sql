-- Journal of every mapping-config change made by an agent.
-- previous_value/new_value hold the JSON of the affected field mapping (target
-- 'field') or of a top-level config key (target 'config'), which is what makes
-- an individual change reversible.
CREATE TABLE IF NOT EXISTS agent_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    mapping_name TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT 'field',
    field TEXT NOT NULL DEFAULT '',
    previous_value TEXT NOT NULL DEFAULT 'null',
    new_value TEXT NOT NULL DEFAULT 'null',
    rationale TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'info',
    applied INTEGER NOT NULL DEFAULT 1,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reverted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_changes_run ON agent_changes (run_id);
CREATE INDEX IF NOT EXISTS idx_agent_changes_mapping ON agent_changes (mapping_name);
