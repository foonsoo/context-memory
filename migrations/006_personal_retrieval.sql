ALTER TABLE memories ADD COLUMN observed_at TEXT;
ALTER TABLE memories ADD COLUMN last_confirmed_at TEXT;

UPDATE memories SET observed_at=created_at WHERE observed_at IS NULL;
UPDATE memories SET last_confirmed_at=updated_at WHERE last_confirmed_at IS NULL AND status='active';

CREATE TABLE memory_embeddings (
  memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  dimensions INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  vector_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE memory_usage (
  memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
  retrieved_count INTEGER NOT NULL DEFAULT 0,
  used_count INTEGER NOT NULL DEFAULT 0,
  helpful_count INTEGER NOT NULL DEFAULT 0,
  incorrect_count INTEGER NOT NULL DEFAULT 0,
  last_retrieved_at TEXT,
  last_used_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE INDEX memory_embeddings_provider ON memory_embeddings(provider, dimensions);
