BEGIN IMMEDIATE;

CREATE TABLE conflict_resolution_runs (
    id INTEGER PRIMARY KEY,
    resolution_key TEXT NOT NULL UNIQUE,
    baseline_generation INTEGER NOT NULL,
    policy TEXT NOT NULL,
    original_path TEXT NOT NULL,
    conflict_copy_path TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('in_progress', 'completed')),
    new_baseline_generation INTEGER
) STRICT;

CREATE UNIQUE INDEX one_incomplete_conflict_resolution
ON conflict_resolution_runs(outcome)
WHERE outcome = 'in_progress';

CREATE TABLE conflict_resolution_operations (
    resolution_run_id INTEGER NOT NULL
        REFERENCES conflict_resolution_runs(id) ON DELETE CASCADE,
    step_order INTEGER NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'started', 'completed')),
    detail TEXT,
    PRIMARY KEY (resolution_run_id, step_order),
    UNIQUE (resolution_run_id, operation)
) STRICT;

PRAGMA user_version = 4;
COMMIT;
