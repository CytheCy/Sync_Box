BEGIN IMMEDIATE;

CREATE TABLE baseline_generations (
    id INTEGER PRIMARY KEY,
    local_root TEXT NOT NULL,
    box_root_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE TABLE baseline_items (
    generation_id INTEGER NOT NULL REFERENCES baseline_generations(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    item_type TEXT NOT NULL CHECK (item_type IN ('file', 'folder')),
    local_size INTEGER, local_mtime_ns INTEGER, local_sha1 TEXT,
    local_device INTEGER, local_inode INTEGER, local_mode INTEGER,
    box_item_id TEXT, box_version_id TEXT, box_etag TEXT, box_sha1 TEXT,
    box_sequence_id TEXT, box_size INTEGER, box_modified_at TEXT,
    PRIMARY KEY (generation_id, relative_path),
    UNIQUE (generation_id, box_item_id)
) STRICT;

CREATE TABLE current_baseline (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation_id INTEGER NOT NULL REFERENCES baseline_generations(id)
) STRICT;

CREATE TABLE sync_operations (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','completed','failed')),
    detail TEXT,
    UNIQUE (run_id, relative_path, action)
) STRICT;

PRAGMA user_version = 3;
COMMIT;
