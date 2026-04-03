CREATE TABLE IF NOT EXISTS migration_sessions (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    selected_objects TEXT NOT NULL DEFAULT '[]',
    config TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    phase TEXT,
    started_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);
