BEGIN IMMEDIATE;
CREATE TABLE upload_receipts (
    id INTEGER PRIMARY KEY,
    generation_id INTEGER NOT NULL,
    local_root TEXT NOT NULL,
    box_root_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    local_json TEXT NOT NULL,
    box_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(generation_id, local_root, box_root_id, relative_path)
) STRICT;
CREATE TABLE file_resolutions (
    token TEXT PRIMARY KEY,
    generation_id INTEGER NOT NULL,
    local_root TEXT NOT NULL,
    box_root_id TEXT NOT NULL,
    original_path TEXT NOT NULL,
    policy TEXT,
    snapshot_json TEXT NOT NULL,
    desired_json TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('preview','in_progress','completed','cancelled')),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;
CREATE UNIQUE INDEX one_active_file_resolution ON file_resolutions(outcome) WHERE outcome='in_progress';
PRAGMA user_version = 7;
COMMIT;
