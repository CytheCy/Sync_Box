BEGIN IMMEDIATE;

ALTER TABLE sync_runs ADD COLUMN baseline_generation INTEGER;
ALTER TABLE sync_runs ADD COLUMN local_root TEXT;
ALTER TABLE sync_runs ADD COLUMN plan_json TEXT;
ALTER TABLE sync_operations ADD COLUMN step_order INTEGER;

-- Version 4 had no process lock or safe resume token. Treat any surviving
-- running row as an interrupted legacy execution before enforcing uniqueness.
UPDATE sync_runs
SET finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP),
    outcome = 'failed',
    summary = COALESCE(summary, 'interrupted before schema v5 migration')
WHERE outcome = 'running';

CREATE UNIQUE INDEX one_running_sync
ON sync_runs(outcome)
WHERE outcome = 'running';

CREATE UNIQUE INDEX sync_operation_step
ON sync_operations(run_id, step_order)
WHERE step_order IS NOT NULL;

PRAGMA user_version = 5;
COMMIT;
