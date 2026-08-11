ALTER TABLE memories ADD COLUMN visibility TEXT NOT NULL DEFAULT 'project'
  CHECK(visibility IN ('project','global'));

CREATE TABLE review_conflicts (
  candidate_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  existing_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  similarity REAL NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(candidate_memory_id, existing_memory_id)
);

CREATE INDEX memories_visibility_status ON memories(visibility, status, updated_at DESC);
