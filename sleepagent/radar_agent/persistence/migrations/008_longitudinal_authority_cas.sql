CREATE TABLE IF NOT EXISTS product_longitudinal_control (
  store_id TEXT PRIMARY KEY,
  retrieval_policy_epoch INTEGER NOT NULL,
  digest_read_enabled BOOLEAN NOT NULL,
  control_revision INTEGER NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS product_longitudinal_subject_epochs (
  subject_id TEXT PRIMARY KEY,
  privacy_epoch INTEGER NOT NULL,
  authorization_epoch INTEGER NOT NULL,
  state_revision INTEGER NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS product_offline_skill_outcomes (
  envelope_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  source_result_hash TEXT NOT NULL,
  withdrawn BOOLEAN NOT NULL,
  envelope_json JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_product_offline_skill_outcomes_subject
  ON product_offline_skill_outcomes(subject_id, withdrawn);

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('008_longitudinal_authority_cas', CURRENT_TIMESTAMP)
ON CONFLICT(version) DO NOTHING;
