CREATE TABLE IF NOT EXISTS product_care_context_states (
  subject_id TEXT PRIMARY KEY,
  version INTEGER NOT NULL,
  state_json JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS product_memory_context_states (
  subject_id TEXT PRIMARY KEY,
  version INTEGER NOT NULL,
  state_json JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS product_commit_journal (
  idempotency_key TEXT PRIMARY KEY,
  tool_name TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  fact_snapshot_hash TEXT NOT NULL,
  state TEXT NOT NULL,
  entry_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS product_episode_results (
  result_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  result_json JSONB NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_product_episode_results_episode
  ON product_episode_results(episode_id, recorded_at, result_id);

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('006_product_agent_state', CURRENT_TIMESTAMP)
ON CONFLICT(version) DO NOTHING;
