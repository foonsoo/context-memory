ALTER TABLE project_policies ADD COLUMN checkpoint_cooldown_seconds INTEGER NOT NULL DEFAULT 300 CHECK(checkpoint_cooldown_seconds BETWEEN 0 AND 86400);
ALTER TABLE project_policies ADD COLUMN checkpoint_hysteresis REAL NOT NULL DEFAULT 0.05 CHECK(checkpoint_hysteresis BETWEEN 0 AND 0.5);

