DROP TRIGGER events_no_update;

ALTER TABLE events ADD COLUMN event_seq INTEGER;

UPDATE events
SET event_seq = (
  SELECT count(*)
  FROM events AS earlier
  WHERE earlier.project_id = events.project_id
    AND (earlier.created_at < events.created_at
      OR (earlier.created_at = events.created_at AND earlier.id <= events.id))
);

CREATE UNIQUE INDEX events_project_seq ON events(project_id,event_seq);

CREATE TABLE project_event_cursors (
  project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  next_seq INTEGER NOT NULL CHECK(next_seq >= 1)
);

INSERT INTO project_event_cursors(project_id,next_seq)
SELECT p.id,coalesce(max(e.event_seq),0)+1
FROM projects p LEFT JOIN events e ON e.project_id=p.id
GROUP BY p.id;

CREATE TRIGGER project_event_cursor_create AFTER INSERT ON projects
BEGIN
  INSERT INTO project_event_cursors(project_id,next_seq) VALUES(new.id,1);
END;

CREATE TRIGGER events_require_seq BEFORE INSERT ON events
WHEN new.event_seq IS NULL
BEGIN SELECT RAISE(ABORT, 'event_seq is required'); END;

CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
