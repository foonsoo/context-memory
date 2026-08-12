ALTER TABLE project_policies ADD COLUMN maintenance_interval_seconds INTEGER NOT NULL DEFAULT 0
  CHECK(maintenance_interval_seconds = 0 OR maintenance_interval_seconds BETWEEN 300 AND 2592000);

CREATE TABLE maintenance_runs (
  project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  last_started_at TEXT,
  last_completed_at TEXT,
  last_error TEXT
);

INSERT INTO maintenance_runs(project_id)
SELECT id FROM projects;

CREATE TRIGGER maintenance_run_create AFTER INSERT ON projects
BEGIN
  INSERT INTO maintenance_runs(project_id) VALUES(new.id);
END;
