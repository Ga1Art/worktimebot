BEGIN;

CREATE TABLE IF NOT EXISTS active_projects (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
ALTER TABLE active_projects ADD COLUMN IF NOT EXISTS yougile_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_yougile_id ON active_projects(yougile_id);

ALTER TABLE work_logs ADD COLUMN IF NOT EXISTS project_name TEXT;
ALTER TABLE work_logs ADD COLUMN IF NOT EXISTS project_id INT REFERENCES active_projects(id);
CREATE INDEX IF NOT EXISTS idx_work_logs_project_id ON work_logs(project_id);

ALTER TABLE expenses ADD COLUMN IF NOT EXISTS project_id INT REFERENCES active_projects(id);
CREATE INDEX IF NOT EXISTS idx_expenses_project_id ON expenses(project_id);

-- Match historical installations only when the normalized name identifies one project.
UPDATE work_logs wl SET project_id = p.id
FROM (
    SELECT MIN(id) AS id, LOWER(TRIM(name)) AS name
    FROM active_projects GROUP BY LOWER(TRIM(name)) HAVING COUNT(*) = 1
) p
WHERE wl.work_type = 'install' AND wl.project_id IS NULL
  AND LOWER(TRIM(wl.project_name)) = p.name;

-- Historical expenses remain unassigned; do not guess their project from descriptions.
COMMIT;
