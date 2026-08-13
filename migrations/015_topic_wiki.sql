CREATE TABLE wiki_pages (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  scope_id TEXT REFERENCES scopes(id) ON DELETE SET NULL,
  topic TEXT NOT NULL,
  title TEXT NOT NULL,
  manual_notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, topic)
);

CREATE TABLE wiki_revisions (
  id TEXT PRIMARY KEY,
  page_id TEXT NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
  revision_no INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('proposed','published','stale','rejected')),
  question TEXT NOT NULL,
  sections_json TEXT NOT NULL,
  generation_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  published_at TEXT,
  stale_reason TEXT,
  UNIQUE(page_id, revision_no)
);

CREATE TABLE wiki_revision_citations (
  revision_id TEXT NOT NULL REFERENCES wiki_revisions(id) ON DELETE CASCADE,
  section_name TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,
  event_id TEXT NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
  PRIMARY KEY(revision_id, section_name, ordinal, memory_id, event_id)
);
CREATE INDEX wiki_revision_citations_memory ON wiki_revision_citations(memory_id, revision_id);

CREATE TRIGGER wiki_revisions_content_immutable BEFORE UPDATE ON wiki_revisions
WHEN OLD.page_id IS NOT NEW.page_id OR OLD.revision_no IS NOT NEW.revision_no
  OR OLD.question IS NOT NEW.question OR OLD.sections_json IS NOT NEW.sections_json
  OR OLD.generation_json IS NOT NEW.generation_json OR OLD.created_at IS NOT NEW.created_at
BEGIN SELECT RAISE(ABORT, 'wiki revision content is immutable'); END;

CREATE TRIGGER wiki_revision_citations_no_update BEFORE UPDATE ON wiki_revision_citations
BEGIN SELECT RAISE(ABORT, 'wiki revision citations are immutable'); END;
CREATE TRIGGER wiki_revision_citations_no_delete BEFORE DELETE ON wiki_revision_citations
BEGIN SELECT RAISE(ABORT, 'wiki revision citations are immutable'); END;
