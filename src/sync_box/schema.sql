BEGIN IMMEDIATE;

-- Baseline pairs record the last state known to be identical on both sides.
-- Step two will use these snapshots to distinguish edits from conflicts.
CREATE TABLE IF NOT EXISTS item_baselines (
    relative_path TEXT PRIMARY KEY,
    box_item_id TEXT UNIQUE,
    item_type TEXT NOT NULL CHECK (item_type IN ('file', 'folder')),
    local_size INTEGER,
    local_mtime_ns INTEGER,
    local_sha1 TEXT,
    box_etag TEXT,
    box_sha1 TEXT,
    synced_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    dry_run INTEGER NOT NULL CHECK (dry_run IN (0, 1)),
    outcome TEXT CHECK (outcome IN ('running', 'completed', 'failed')),
    summary TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY,
    relative_path TEXT NOT NULL,
    box_item_id TEXT,
    detected_at TEXT NOT NULL,
    local_fingerprint TEXT,
    box_fingerprint TEXT,
    reason TEXT NOT NULL,
    resolution TEXT,
    resolved_at TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS conflicts_unresolved_path
ON conflicts(relative_path)
WHERE resolved_at IS NULL;

PRAGMA user_version = 1;
COMMIT;
