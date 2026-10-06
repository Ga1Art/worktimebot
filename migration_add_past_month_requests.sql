-- Apply migration_add_project_accounting.sql first.
BEGIN;
CREATE TABLE IF NOT EXISTS past_month_requests (
    id SERIAL PRIMARY KEY,
    submission_key TEXT NOT NULL UNIQUE,
    worker_id INT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    full_name TEXT NOT NULL,
    request_date DATE NOT NULL,
    entry_type TEXT NOT NULL CHECK (entry_type IN ('shift', 'install', 'expense')),
    project_id INT REFERENCES active_projects(id),
    project_name TEXT,
    hours NUMERIC,
    amount NUMERIC,
    description TEXT,
    source_platform TEXT NOT NULL,
    source_peer_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    archive_sync_pending BOOLEAN NOT NULL DEFAULT FALSE,
    work_log_id INT REFERENCES work_logs(id) ON DELETE SET NULL,
    expense_id INT REFERENCES expenses(id) ON DELETE SET NULL,
    decided_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_past_requests_pending ON past_month_requests(status, archive_sync_pending);
COMMIT;
