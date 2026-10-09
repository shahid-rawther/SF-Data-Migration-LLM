-- A data readiness check: whether the source can supply every field and parent
-- the destination requires, with suggestions for the gaps. Runs before a
-- session exists, so it is keyed by the mappings it examined.
-- report holds the deterministic classification; gaps holds a JSON list of
-- {key, analysis, suggestions[], manual_tasks[]}, every action and resolution
-- in it already validated. applied maps "gap:suggestion" to the run that
-- applied it.
CREATE TABLE IF NOT EXISTS agent_readiness (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL DEFAULT '',
    mapping_names TEXT NOT NULL DEFAULT '[]',
    report TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL DEFAULT '',
    gaps TEXT NOT NULL DEFAULT '[]',
    applied TEXT NOT NULL DEFAULT '{}',
    model_invoked INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    usage TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Every write a readiness suggestion made to Salesforce itself. Mapping
-- changes are journalled in agent_changes as usual; this is the other half.
-- A created record can be reverted by deleting it; a created field cannot,
-- because dropping it destroys whatever has been loaded into it since.
CREATE TABLE IF NOT EXISTS agent_sf_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    readiness_id INTEGER,
    run_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    object_name TEXT NOT NULL,
    record_id TEXT NOT NULL DEFAULT '',
    field_name TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    rationale TEXT NOT NULL DEFAULT '',
    revertible INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reverted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_sf_changes_readiness ON agent_sf_changes (readiness_id);
