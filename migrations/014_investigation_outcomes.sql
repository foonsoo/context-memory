ALTER TABLE investigation_claims ADD COLUMN expected_outcome TEXT;
ALTER TABLE investigation_claims ADD COLUMN outcome_effect TEXT
  CHECK(outcome_effect IS NULL OR outcome_effect IN ('confirms','weakens','disputes','supersedes'));
