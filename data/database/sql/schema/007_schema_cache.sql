-- Cached field metadata for source tables and destination objects.
-- Walking a prerequisite chain describes many objects, and a Salesforce
-- describe costs an OAuth round trip plus the describe itself, so the traversal
-- is only affordable if repeat describes are served locally.
-- connection_key fingerprints the instance or org the fields came from so two
-- environments never share an entry.
DROP TABLE IF EXISTS source_schema_cache;

CREATE TABLE IF NOT EXISTS schema_cache (
    -- 'source' for an extract-side table, 'destination' for a Salesforce object.
    scope TEXT NOT NULL,
    system TEXT NOT NULL,
    connection_key TEXT NOT NULL,
    object_name TEXT NOT NULL,
    fields TEXT NOT NULL DEFAULT '[]',
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (scope, system, connection_key, object_name)
);
