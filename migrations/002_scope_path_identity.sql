CREATE UNIQUE INDEX scopes_unique_path ON scopes(path) WHERE path IS NOT NULL;
