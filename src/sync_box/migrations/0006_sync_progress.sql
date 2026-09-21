BEGIN IMMEDIATE;

CREATE TABLE sync_progress (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    phase TEXT NOT NULL,
    completed INTEGER NOT NULL CHECK (completed >= 0),
    total INTEGER CHECK (total IS NULL OR total >= 0),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

PRAGMA user_version = 6;
COMMIT;
