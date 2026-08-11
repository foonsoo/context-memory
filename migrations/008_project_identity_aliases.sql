CREATE TABLE project_aliases (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('path','name')),
  value TEXT NOT NULL,
  normalized TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(project_id, kind, normalized)
);

CREATE INDEX project_aliases_lookup ON project_aliases(kind, normalized, project_id);

INSERT OR IGNORE INTO project_aliases(project_id,kind,value,normalized,created_at,updated_at)
SELECT project_id,'path',path,path,created_at,created_at FROM scopes WHERE path IS NOT NULL;

INSERT OR IGNORE INTO project_aliases(project_id,kind,value,normalized,created_at,updated_at)
SELECT id,'name',name,lower(name),created_at,created_at FROM projects;
