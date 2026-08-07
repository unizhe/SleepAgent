CREATE TABLE IF NOT EXISTS product_habit_profile_states (
  subject_id TEXT PRIMARY KEY,
  version INTEGER NOT NULL,
  state_json JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS product_habit_profile_commits (
  idempotency_key TEXT PRIMARY KEY,
  payload_hash TEXT NOT NULL,
  confirmation_id TEXT NOT NULL UNIQUE,
  subject_id TEXT NOT NULL,
  receipt_json JSONB NOT NULL,
  committed_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_product_habit_profile_commits_subject
  ON product_habit_profile_commits(subject_id, committed_at);

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('003_habit_profile', CURRENT_TIMESTAMP)
ON CONFLICT(version) DO NOTHING;
