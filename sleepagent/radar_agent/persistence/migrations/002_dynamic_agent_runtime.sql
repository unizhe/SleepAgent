ALTER TABLE radar_tasks ADD COLUMN runtime_kind TEXT NOT NULL DEFAULT 'legacy_fixed';
ALTER TABLE radar_tasks ADD COLUMN runtime_contract_version TEXT NOT NULL DEFAULT 'radar-legacy.v1';
ALTER TABLE radar_tasks ADD COLUMN execution_mode TEXT;
ALTER TABLE radar_tasks ADD COLUMN completion_status TEXT;
ALTER TABLE radar_tasks ADD COLUMN task_version INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS radar_dynamic_goals (
  goal_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  goal_type TEXT NOT NULL,
  goal_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_dynamic_plan_revisions (
  plan_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  goal_id TEXT NOT NULL REFERENCES radar_dynamic_goals(goal_id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  supersedes_plan_id TEXT,
  plan_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE(task_id, revision)
);

CREATE TABLE IF NOT EXISTS radar_dynamic_steps (
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  plan_id TEXT NOT NULL REFERENCES radar_dynamic_plan_revisions(plan_id) ON DELETE CASCADE,
  step_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  status TEXT NOT NULL,
  step_json JSONB NOT NULL,
  output_json JSONB,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  PRIMARY KEY(plan_id, step_id)
);

CREATE TABLE IF NOT EXISTS radar_model_invocations (
  invocation_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  purpose TEXT NOT NULL,
  agent TEXT NOT NULL,
  client_idempotency_key TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  provider_request_id TEXT,
  invocation_json JSONB NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  finished_at TIMESTAMPTZ,
  UNIQUE(client_idempotency_key, attempt)
);

CREATE TABLE IF NOT EXISTS radar_agent_invocations (
  invocation_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  plan_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  status TEXT NOT NULL,
  caused_by_a2a_message_id TEXT,
  invocation_json JSONB NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS radar_tool_invocations (
  invocation_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  plan_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  client_idempotency_key TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  invocation_json JSONB NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  finished_at TIMESTAMPTZ,
  UNIQUE(client_idempotency_key, attempt)
);

CREATE TABLE IF NOT EXISTS radar_user_input_requests (
  request_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  question_id TEXT NOT NULL,
  status TEXT NOT NULL,
  request_json JSONB NOT NULL,
  response_json JSONB,
  created_at TIMESTAMPTZ NOT NULL,
  resolved_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS radar_runtime_budgets (
  task_id TEXT PRIMARY KEY REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  budget_json JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_runtime_checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL,
  state_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE(task_id, sequence)
);

CREATE TABLE IF NOT EXISTS radar_job_leases (
  task_id TEXT PRIMARY KEY REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  worker_id TEXT NOT NULL,
  lease_token TEXT NOT NULL,
  lease_expires_at TIMESTAMPTZ NOT NULL,
  heartbeat_at TIMESTAMPTZ NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS radar_fact_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  sha256 TEXT NOT NULL,
  snapshot_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_completion_receipts (
  receipt_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  execution_mode TEXT NOT NULL,
  completion_status TEXT NOT NULL,
  receipt_json JSONB NOT NULL,
  completed_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_radar_dynamic_goals_task
  ON radar_dynamic_goals(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_radar_dynamic_plans_task
  ON radar_dynamic_plan_revisions(task_id, revision);
CREATE INDEX IF NOT EXISTS idx_radar_model_invocations_task
  ON radar_model_invocations(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_radar_agent_invocations_task
  ON radar_agent_invocations(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_radar_tool_invocations_task
  ON radar_tool_invocations(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_radar_user_input_task
  ON radar_user_input_requests(task_id, created_at);

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('002_dynamic_agent_runtime', CURRENT_TIMESTAMP)
ON CONFLICT(version) DO NOTHING;
