BEGIN IMMEDIATE;

CREATE TABLE inventory_runs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('local', 'box')),
    root_identifier TEXT NOT NULL,
    scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE TABLE inventory_items (
    run_id INTEGER NOT NULL REFERENCES inventory_runs(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    item_type TEXT NOT NULL,
    size INTEGER,
    modified_at TEXT,
    content_id TEXT,
    version_id TEXT,
    etag TEXT,
    sha1 TEXT,
    sequence_id TEXT,
    device INTEGER,
    inode INTEGER,
    mode INTEGER,
    mtime_ns INTEGER,
    PRIMARY KEY (run_id, relative_path)
) STRICT;

CREATE INDEX inventory_items_content_id
ON inventory_items(run_id, content_id)
WHERE content_id IS NOT NULL;

PRAGMA user_version = 2;
COMMIT;
