CREATE TABLE project_policies (
  project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  max_context_chars INTEGER NOT NULL DEFAULT 12000 CHECK(max_context_chars BETWEEN 1000 AND 20000),
  max_context_items INTEGER NOT NULL DEFAULT 20 CHECK(max_context_items BETWEEN 1 AND 50),
  audit_keep_entries INTEGER NOT NULL DEFAULT 10000 CHECK(audit_keep_entries BETWEEN 100 AND 100000),
  terminal_memory_days INTEGER NOT NULL DEFAULT 180 CHECK(terminal_memory_days BETWEEN 1 AND 3650),
  updated_at TEXT NOT NULL
);

INSERT INTO project_policies(project_id,updated_at)
SELECT id,created_at FROM projects;

CREATE TRIGGER project_policy_create AFTER INSERT ON projects
BEGIN
  INSERT INTO project_policies(project_id,updated_at) VALUES(new.id,new.created_at);
END;

CREATE TABLE audit_checkpoints (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  from_seq INTEGER NOT NULL,
  through_seq INTEGER NOT NULL,
  entry_count INTEGER NOT NULL,
  previous_digest TEXT,
  digest TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX audit_checkpoints_project ON audit_checkpoints(project_id,through_seq);
CREATE TRIGGER audit_checkpoints_no_update BEFORE UPDATE ON audit_checkpoints
BEGIN SELECT RAISE(ABORT, 'audit checkpoints are append-only'); END;
CREATE TRIGGER audit_checkpoints_no_delete BEFORE DELETE ON audit_checkpoints
BEGIN SELECT RAISE(ABORT, 'audit checkpoints are append-only'); END;

CREATE TABLE maintenance_control (
  id INTEGER PRIMARY KEY CHECK(id=1),
  audit_prune_enabled INTEGER NOT NULL DEFAULT 0 CHECK(audit_prune_enabled IN (0,1))
);
INSERT INTO maintenance_control(id,audit_prune_enabled) VALUES(1,0);

DROP TRIGGER audit_no_delete;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
WHEN (SELECT audit_prune_enabled FROM maintenance_control WHERE id=1)=0
BEGIN SELECT RAISE(ABORT, 'audit log is append-only outside checkpointed maintenance'); END;

CREATE TRIGGER memories_fts_insert AFTER INSERT ON memories
BEGIN
  INSERT INTO memories_fts(memory_id,title,content,tags)
  VALUES(new.id,new.title,new.content,
    replace(replace(replace(new.tags_json,'[',''),']',''),'"',''));
END;

CREATE TRIGGER memories_fts_update AFTER UPDATE OF title,content,tags_json ON memories
BEGIN
  DELETE FROM memories_fts WHERE memory_id=old.id;
  INSERT INTO memories_fts(memory_id,title,content,tags)
  VALUES(new.id,new.title,new.content,
    replace(replace(replace(new.tags_json,'[',''),']',''),'"',''));
END;

CREATE TRIGGER memories_fts_delete AFTER DELETE ON memories
BEGIN
  DELETE FROM memories_fts WHERE memory_id=old.id;
END;

