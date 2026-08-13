CREATE TABLE investigations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  scope_id TEXT REFERENCES scopes(id) ON DELETE SET NULL,
  question TEXT NOT NULL,
  reason TEXT NOT NULL,
  decision_to_inform TEXT NOT NULL,
  constraints_json TEXT NOT NULL DEFAULT '[]',
  initiator TEXT NOT NULL DEFAULT 'unknown',
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','completed')),
  started_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX investigations_project_started ON investigations(project_id, started_at DESC);

CREATE TABLE source_analyses (
  id TEXT PRIMARY KEY,
  investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
  source_type TEXT NOT NULL,
  stable_source_id TEXT NOT NULL,
  canonical_uri TEXT,
  source_version TEXT,
  source_updated_at TEXT,
  retrieved_at TEXT NOT NULL,
  section_anchor TEXT,
  access_reason TEXT NOT NULL,
  analysis_method TEXT NOT NULL,
  content_fingerprint TEXT,
  identity_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(investigation_id, identity_key)
);

CREATE TABLE investigation_claims (
  id TEXT PRIMARY KEY,
  investigation_id TEXT NOT NULL REFERENCES investigations(id) ON DELETE CASCADE,
  source_analysis_id TEXT NOT NULL REFERENCES source_analyses(id) ON DELETE CASCADE,
  claim_key TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('evidence','inference','action','decision','rationale','outcome')),
  event_id TEXT NOT NULL UNIQUE REFERENCES events(id) ON DELETE RESTRICT,
  memory_id TEXT REFERENCES memories(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  UNIQUE(source_analysis_id, claim_key)
);

CREATE TABLE investigation_claim_links (
  from_claim_id TEXT NOT NULL REFERENCES investigation_claims(id) ON DELETE CASCADE,
  to_claim_id TEXT NOT NULL REFERENCES investigation_claims(id) ON DELETE CASCADE,
  relation TEXT NOT NULL CHECK(relation IN ('derived_from','informed','supports')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(from_claim_id, to_claim_id, relation)
);

CREATE TRIGGER source_analyses_no_update BEFORE UPDATE ON source_analyses
BEGIN SELECT RAISE(ABORT, 'source analyses are append-only'); END;
CREATE TRIGGER source_analyses_no_delete BEFORE DELETE ON source_analyses
BEGIN SELECT RAISE(ABORT, 'source analyses are append-only'); END;
CREATE TRIGGER investigation_claims_no_update BEFORE UPDATE ON investigation_claims
BEGIN SELECT RAISE(ABORT, 'investigation claims are append-only'); END;
CREATE TRIGGER investigation_claims_no_delete BEFORE DELETE ON investigation_claims
BEGIN SELECT RAISE(ABORT, 'investigation claims are append-only'); END;
CREATE TRIGGER investigation_claim_links_no_update BEFORE UPDATE ON investigation_claim_links
BEGIN SELECT RAISE(ABORT, 'investigation claim links are append-only'); END;
CREATE TRIGGER investigation_claim_links_no_delete BEFORE DELETE ON investigation_claim_links
BEGIN SELECT RAISE(ABORT, 'investigation claim links are append-only'); END;
