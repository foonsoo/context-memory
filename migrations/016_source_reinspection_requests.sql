CREATE TABLE source_reinspection_requests (
  id TEXT PRIMARY KEY,
  source_analysis_id TEXT NOT NULL REFERENCES source_analyses(id) ON DELETE RESTRICT,
  reason TEXT NOT NULL CHECK(reason IN ('old','unavailable','newer_version_known')),
  details TEXT,
  known_source_version TEXT,
  requested_at TEXT NOT NULL,
  CHECK(reason = 'newer_version_known' OR known_source_version IS NULL),
  CHECK(reason <> 'newer_version_known' OR length(trim(known_source_version)) > 0)
);
CREATE INDEX source_reinspection_requests_analysis
  ON source_reinspection_requests(source_analysis_id, requested_at DESC, id);

CREATE TRIGGER source_reinspection_requests_no_update BEFORE UPDATE ON source_reinspection_requests
BEGIN SELECT RAISE(ABORT, 'source reinspection requests are append-only'); END;
CREATE TRIGGER source_reinspection_requests_no_delete BEFORE DELETE ON source_reinspection_requests
BEGIN SELECT RAISE(ABORT, 'source reinspection requests are append-only'); END;
