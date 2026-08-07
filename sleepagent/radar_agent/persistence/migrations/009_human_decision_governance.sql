CREATE TABLE IF NOT EXISTS product_human_decisions (
  decision_id TEXT PRIMARY KEY,
  task_id TEXT REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  episode_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  status TEXT NOT NULL,
  target_hash TEXT NOT NULL,
  decision_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_product_human_decisions_task
  ON product_human_decisions(task_id, created_at, decision_id);

CREATE INDEX IF NOT EXISTS idx_product_human_decisions_inbox
  ON product_human_decisions(subject_id, status, created_at, decision_id);

CREATE TABLE IF NOT EXISTS product_human_decision_events (
  event_id TEXT PRIMARY KEY,
  decision_id TEXT NOT NULL REFERENCES product_human_decisions(decision_id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  actor_id TEXT,
  event_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_product_human_decision_events_decision
  ON product_human_decision_events(decision_id, created_at, event_id);

CREATE TABLE IF NOT EXISTS product_pending_habit_change_sets (
  change_set_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  state TEXT NOT NULL,
  payload_json JSONB NOT NULL,
  snapshot_json JSONB NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_product_pending_habit_subject
  ON product_pending_habit_change_sets(subject_id, state, created_at, change_set_id);

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('009_human_decision_governance', CURRENT_TIMESTAMP)
ON CONFLICT(version) DO NOTHING;
