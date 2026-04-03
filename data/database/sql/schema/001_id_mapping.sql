CREATE TABLE IF NOT EXISTS id_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    servicenow_id TEXT NOT NULL,
    salesforce_id TEXT NOT NULL,
    hash_key TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (table_name, servicenow_id)
);