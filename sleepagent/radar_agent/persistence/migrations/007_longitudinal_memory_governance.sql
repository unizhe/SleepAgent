CREATE TABLE IF NOT EXISTS product_episode_result_revisions (
  terminal_result_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  receipt_revision INTEGER NOT NULL,
  subject_id TEXT NOT NULL,
  source_result_hash TEXT NOT NULL,
  result_json JSONB NOT NULL,
  terminal_recorded_at TIMESTAMPTZ NOT NULL,
  UNIQUE (episode_id, receipt_revision),
  UNIQUE (source_result_hash)
);

CREATE INDEX IF NOT EXISTS idx_product_episode_result_revisions_episode
  ON product_episode_result_revisions(episode_id, receipt_revision);

CREATE TABLE IF NOT EXISTS product_induction_manifests (
  manifest_id TEXT PRIMARY KEY,
  manifest_hash TEXT NOT NULL UNIQUE,
  subject_id TEXT NOT NULL,
  terminal_result_id TEXT NOT NULL,
  ciphertext TEXT,
  nonce TEXT,
  wrapped_data_key TEXT,
  key_id TEXT,
  expires_at TIMESTAMPTZ NOT NULL,
  purged_at TIMESTAMPTZ,
  purge_receipt_ref TEXT
);

CREATE TABLE IF NOT EXISTS product_longitudinal_operational_state (
  store_id TEXT PRIMARY KEY,
  state_json JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS product_induction_jobs (
  job_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  episode_id TEXT NOT NULL,
  terminal_result_id TEXT NOT NULL,
  state TEXT NOT NULL,
  attempt_count INTEGER NOT NULL,
  processing_generation INTEGER NOT NULL,
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  next_attempt_at TIMESTAMPTZ NOT NULL,
  job_json JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS product_induction_job_events (
  event_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  event_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS product_induction_receipts (
  receipt_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  processing_generation INTEGER NOT NULL,
  status TEXT NOT NULL,
  receipt_json JSONB NOT NULL,
  completed_at TIMESTAMPTZ NOT NULL,
  UNIQUE (job_id, processing_generation, status)
);

CREATE TABLE IF NOT EXISTS product_episode_digests (
  digest_id TEXT PRIMARY KEY,
  digest_hash TEXT NOT NULL UNIQUE,
  subject_id TEXT NOT NULL,
  episode_id TEXT NOT NULL,
  source_receipt_revision INTEGER NOT NULL,
  digest_json JSONB NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS product_episode_digest_status_events (
  event_id TEXT PRIMARY KEY,
  digest_id TEXT NOT NULL,
  status_sequence INTEGER NOT NULL,
  event_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (digest_id, status_sequence)
);

CREATE TABLE IF NOT EXISTS product_memory_read_receipts (
  receipt_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  receipt_json JSONB NOT NULL,
  completed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS product_pending_profile_candidates (
  candidate_hash TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  status TEXT NOT NULL,
  candidate_json JSONB NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (subject_id, candidate_hash)
);

CREATE TABLE IF NOT EXISTS product_skill_outcomes (
  outcome_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  status TEXT NOT NULL,
  outcome_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS product_publication_journal (
  intent_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  draft_hash TEXT NOT NULL,
  state TEXT NOT NULL,
  entry_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  UNIQUE (episode_id, draft_hash)
);

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('007_longitudinal_memory_governance', CURRENT_TIMESTAMP)
ON CONFLICT(version) DO NOTHING;
