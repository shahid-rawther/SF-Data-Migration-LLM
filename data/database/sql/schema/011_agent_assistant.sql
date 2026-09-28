-- What the agent concluded about load failures nothing fixed automatically.
-- suggestions holds a JSON list of {title, explanation, resolutions[]}, where
-- each resolution has already been through the validator.
CREATE TABLE IF NOT EXISTS agent_diagnoses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    mapping_name TEXT NOT NULL,
    digest TEXT NOT NULL DEFAULT '{}',
    analysis TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    suggestions TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_diagnoses_session ON agent_diagnoses (session_id);

-- The per-session conversation with the assistant agent.
CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    proposal_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_messages_session ON agent_messages (session_id);

-- Mapping changes the agent proposed and the expert has not yet decided on.
-- Nothing here touches a mapping until it is applied.
CREATE TABLE IF NOT EXISTS agent_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    mapping_name TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    resolutions TEXT NOT NULL DEFAULT '[]',
    preview TEXT NOT NULL DEFAULT '[]',
    rerun TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    run_id TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at TEXT,
    -- 'diagnosis' or 'chat'; empty for proposals stored before it was recorded
    origin TEXT NOT NULL DEFAULT '',
    diagnosis_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_agent_proposals_session ON agent_proposals (session_id);
