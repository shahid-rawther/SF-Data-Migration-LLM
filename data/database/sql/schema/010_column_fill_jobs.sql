-- Deliberate, after-the-fact writes to a single column on records that have
-- already been migrated.
--
-- Distinct from backfill_units, which exists because the loader itself had to
-- leave a lookup empty and owes a second pass. Nothing here is owed: a person
-- decided a column needs a value it was never mapped to receive, or needs a
-- value corrected, and asked for it. So there is no 'reason' and no approval
-- gate, but there is a preview: the spec is evaluated against the staged source
-- and shown before anything is sent.
--
-- One row per requested fill rather than per record. The spec is kept verbatim
-- so a past fill can be read back and understood, or repeated, long after the
-- run that produced it -- which matters more here than for a backfill, because
-- the intent lived in a person's head rather than in the mapping.
CREATE TABLE IF NOT EXISTS column_fill_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    -- The mapping whose already-loaded records are being updated. Its config
    -- supplies the source table, the id-map table holding each record's
    -- destination id, and the credentials the update goes out on.
    mapping_name TEXT NOT NULL,
    destination_object TEXT NOT NULL DEFAULT '',
    source_table TEXT NOT NULL DEFAULT '',
    -- The single destination column this job writes.
    field TEXT NOT NULL,
    -- How the value is decided: 'constant', 'value_map' or 'rules'.
    mode TEXT NOT NULL DEFAULT 'constant',
    -- The full fill definition as JSON, exactly as it was submitted.
    spec TEXT NOT NULL DEFAULT '{}',
    -- draft | running | completed | failed | cancelled
    --
    -- A job is created as 'draft' and only leaves that state when it is run, so
    -- an abandoned drawer leaves a readable record of what was being considered
    -- rather than a half-applied change.
    status TEXT NOT NULL DEFAULT 'draft',
    -- Rows the preview expected to write, so the outcome can be compared
    -- against the intent rather than only reported on its own.
    rows_targeted INTEGER NOT NULL DEFAULT 0,
    records_updated INTEGER NOT NULL DEFAULT 0,
    records_failed INTEGER NOT NULL DEFAULT 0,
    -- Whether the field entry was also written into the mapping, which decides
    -- if a later run of this object repeats the fill or not.
    saved_to_mapping INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_column_fill_jobs_session ON column_fill_jobs (session_id);
CREATE INDEX IF NOT EXISTS idx_column_fill_jobs_status ON column_fill_jobs (status);
