CREATE TABLE IF NOT EXISTS product_habit_question_suppressions (
  subject_id TEXT NOT NULL,
  concept_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  confirmation_ref TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  suppression_json JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (subject_id, concept_id, scope)
);

CREATE INDEX IF NOT EXISTS idx_product_habit_question_suppressions_expiry
  ON product_habit_question_suppressions(subject_id, expires_at);

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('004_habit_question_suppression', CURRENT_TIMESTAMP)
ON CONFLICT(version) DO NOTHING;
