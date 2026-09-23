-- Local index of the checkpoints held in S3.
--
-- The checkpoint itself lives in the bucket; this table records what was last
-- written for each session so the UI can show resume state without a network
-- call, and so a save failure is visible rather than silent.
CREATE TABLE IF NOT EXISTS session_checkpoints (
    session_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    bucket TEXT NOT NULL DEFAULT '',
    checkpoint_key TEXT NOT NULL DEFAULT '',
    id_mappings_key TEXT NOT NULL DEFAULT '',
    version_id TEXT NOT NULL DEFAULT '',
    -- Row counts carried by the checkpoint, kept here for cheap display.
    records_fetched INTEGER NOT NULL DEFAULT 0,
    records_processed INTEGER NOT NULL DEFAULT 0,
    records_failed INTEGER NOT NULL DEFAULT 0,
    id_mapping_count INTEGER NOT NULL DEFAULT 0,
    completed_objects TEXT NOT NULL DEFAULT '[]',
    -- Empty when the last save succeeded.
    last_error TEXT NOT NULL DEFAULT '',
    saved_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
