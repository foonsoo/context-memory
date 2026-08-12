ALTER TABLE project_policies ADD COLUMN message_ttl_seconds INTEGER NOT NULL DEFAULT 0
  CHECK(message_ttl_seconds BETWEEN 0 AND 2592000);

CREATE TABLE event_receipts (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  consumer_id TEXT NOT NULL,
  scope_key TEXT NOT NULL DEFAULT '',
  kinds_json TEXT NOT NULL,
  acknowledged_cursor INTEGER NOT NULL DEFAULT 0 CHECK(acknowledged_cursor >= 0),
  delivered_cursor INTEGER NOT NULL DEFAULT 0 CHECK(delivered_cursor >= acknowledged_cursor),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(project_id,consumer_id,scope_key,kinds_json)
);

CREATE INDEX event_receipts_project_consumer ON event_receipts(project_id,consumer_id);
