ALTER TABLE project_policies ADD COLUMN checkpoint_soft_usage REAL NOT NULL DEFAULT 0.60 CHECK(checkpoint_soft_usage BETWEEN 0 AND 1);
ALTER TABLE project_policies ADD COLUMN checkpoint_hard_usage REAL NOT NULL DEFAULT 0.75 CHECK(checkpoint_hard_usage BETWEEN 0 AND 1);
ALTER TABLE project_policies ADD COLUMN checkpoint_elapsed_seconds INTEGER NOT NULL DEFAULT 1800 CHECK(checkpoint_elapsed_seconds BETWEEN 60 AND 86400);
ALTER TABLE project_policies ADD COLUMN checkpoint_event_count INTEGER NOT NULL DEFAULT 25 CHECK(checkpoint_event_count BETWEEN 1 AND 10000);
ALTER TABLE project_policies ADD COLUMN checkpoint_max_age_seconds INTEGER NOT NULL DEFAULT 3600 CHECK(checkpoint_max_age_seconds BETWEEN 60 AND 604800);

