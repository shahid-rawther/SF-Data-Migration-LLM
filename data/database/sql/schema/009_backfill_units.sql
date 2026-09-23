-- Lookups that were deliberately left empty on the first pass, and the state of
-- the follow-up update that fills them.
--
-- A circular reference has no parent-first order: whichever object loads first
-- cannot resolve its lookup to the other, because the other has no destination
-- ids yet. The first pass therefore writes those fields blank and records the
-- affected rows here, so a second pass can attach them once both sides exist.
--
-- One row per (session, mapping, field) rather than per record: the unit a
-- person approves is "fill Account.ParentId", and the per-record detail lives in
-- the deferred CSVs the transform writes.
--
-- Nothing here runs on its own. A unit stays 'pending' until someone approves
-- it, which is the whole point of the table: the backfill issues updates against
-- records already in the destination, so it is not something to do silently.
CREATE TABLE IF NOT EXISTS backfill_units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    mapping_name TEXT NOT NULL,
    destination_object TEXT NOT NULL DEFAULT '',
    source_table TEXT NOT NULL DEFAULT '',
    field TEXT NOT NULL,
    -- Why the field was deferred: 'dependency_cycle', 'self_reference' or
    -- 'parent_not_migrated'.
    reason TEXT NOT NULL DEFAULT '',
    -- The destination object whose ids this field is waiting for.
    --
    -- Kept as the object rather than the id-map table it will eventually read,
    -- because that table's name is built from the future parent mapping's own
    -- driving table and transformation name. For 'parent_not_migrated' no such
    -- mapping has run, so the name cannot be written down when the unit is
    -- created -- only the object can. Resolving object -> table at check time
    -- is what lets the wait end on its own once something migrates it.
    awaiting_object TEXT NOT NULL DEFAULT '',
    -- Rows whose source held a reference that did not resolve on the first pass.
    rows_deferred INTEGER NOT NULL DEFAULT 0,
    -- How many of those can be resolved now, from the id map as it currently
    -- stands. Recomputed whenever the plan is read, since loading more objects
    -- changes the answer.
    rows_resolvable INTEGER NOT NULL DEFAULT 0,
    -- pending | approved | running | completed | failed | skipped
    status TEXT NOT NULL DEFAULT 'pending',
    records_updated INTEGER NOT NULL DEFAULT 0,
    records_failed INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at TEXT,
    completed_at TEXT,
    UNIQUE (session_id, mapping_name, field)
);

CREATE INDEX IF NOT EXISTS idx_backfill_units_session ON backfill_units (session_id);
CREATE INDEX IF NOT EXISTS idx_backfill_units_status ON backfill_units (status);
