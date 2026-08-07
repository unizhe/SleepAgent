CREATE TABLE IF NOT EXISTS radar_agent_schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS radar_subjects (
  subject_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  timezone_name TEXT NOT NULL DEFAULT 'UTC',
  subject_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_user_roles (
  role_binding_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  subject_id TEXT NOT NULL REFERENCES radar_subjects(subject_id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  display_name TEXT NOT NULL,
  permissions_json JSONB NOT NULL,
  role_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_data_authorizations (
  authorization_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES radar_subjects(subject_id) ON DELETE CASCADE,
  granted_by_user_id TEXT NOT NULL,
  granted_by_role TEXT NOT NULL,
  status TEXT NOT NULL,
  scopes_json JSONB NOT NULL,
  authorization_json JSONB NOT NULL,
  granted_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS radar_tasks (
  task_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL,
  subject_id TEXT NOT NULL REFERENCES radar_subjects(subject_id) ON DELETE CASCADE,
  radar_device_id TEXT NOT NULL,
  role TEXT NOT NULL,
  scenario TEXT NOT NULL,
  status TEXT NOT NULL,
  idempotency_key TEXT,
  task_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_task_events (
  event_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  trace_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  event_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE(task_id, sequence)
);

CREATE TABLE IF NOT EXISTS radar_human_confirmations (
  confirmation_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  action_type TEXT NOT NULL,
  requested_role TEXT NOT NULL,
  status TEXT NOT NULL,
  confirmation_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  resolved_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS radar_task_artifact_versions (
  artifact_version_id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  artifact_type TEXT NOT NULL,
  version_number INTEGER NOT NULL,
  artifact_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE(artifact_id, version_number)
);

CREATE TABLE IF NOT EXISTS radar_devices (
  radar_device_id TEXT PRIMARY KEY,
  subject_id TEXT REFERENCES radar_subjects(subject_id) ON DELETE SET NULL,
  provider TEXT NOT NULL,
  status TEXT NOT NULL,
  device_json JSONB NOT NULL,
  registered_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_vital_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  radar_device_id TEXT NOT NULL REFERENCES radar_devices(radar_device_id) ON DELETE CASCADE,
  subject_id TEXT REFERENCES radar_subjects(subject_id) ON DELETE SET NULL,
  measured_at TIMESTAMPTZ NOT NULL,
  received_at TIMESTAMPTZ NOT NULL,
  snapshot_json JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_night_summaries (
  summary_id TEXT PRIMARY KEY,
  radar_device_id TEXT NOT NULL REFERENCES radar_devices(radar_device_id) ON DELETE CASCADE,
  subject_id TEXT REFERENCES radar_subjects(subject_id) ON DELETE SET NULL,
  night_of DATE NOT NULL,
  data_coverage_ratio DOUBLE PRECISION NOT NULL,
  summary_json JSONB NOT NULL,
  generated_at TIMESTAMPTZ NOT NULL,
  UNIQUE(radar_device_id, night_of)
);

CREATE TABLE IF NOT EXISTS radar_evidence_ledgers (
  ledger_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  review_status TEXT NOT NULL,
  confidence DOUBLE PRECISION NOT NULL,
  ledger_json JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_evidence_claims (
  claim_id TEXT PRIMARY KEY,
  ledger_id TEXT NOT NULL REFERENCES radar_evidence_ledgers(ledger_id) ON DELETE CASCADE,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  generated_by TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  confidence DOUBLE PRECISION NOT NULL,
  claim_json JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_a2a_messages (
  message_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  target_task_id TEXT,
  sender TEXT NOT NULL,
  receiver TEXT NOT NULL,
  intent TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  collaboration_round INTEGER NOT NULL DEFAULT 1,
  routed_by TEXT NOT NULL DEFAULT 'orchestrator',
  shared_artifact_type TEXT,
  message_status TEXT NOT NULL,
  message_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_conflict_records (
  conflict_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  final_status TEXT NOT NULL,
  requires_human_confirmation BOOLEAN NOT NULL,
  conflict_json JSONB NOT NULL,
  decided_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_alerts (
  alert_id TEXT PRIMARY KEY,
  task_id TEXT REFERENCES radar_tasks(task_id) ON DELETE SET NULL,
  subject_id TEXT REFERENCES radar_subjects(subject_id) ON DELETE CASCADE,
  risk_level TEXT NOT NULL,
  status TEXT NOT NULL,
  alert_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  resolved_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS radar_report_artifacts (
  artifact_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES radar_tasks(task_id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  title TEXT NOT NULL,
  generation_mode TEXT NOT NULL,
  current_version_id TEXT,
  artifact_json JSONB NOT NULL,
  generated_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_report_artifact_versions (
  artifact_version_id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL REFERENCES radar_report_artifacts(artifact_id) ON DELETE CASCADE,
  version_number INTEGER NOT NULL,
  source_claim_ids JSONB NOT NULL,
  prompt_version TEXT NOT NULL,
  model_provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  generation_mode TEXT NOT NULL,
  version_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE(artifact_id, version_number)
);

CREATE TABLE IF NOT EXISTS radar_audit_logs (
  audit_id TEXT PRIMARY KEY,
  task_id TEXT REFERENCES radar_tasks(task_id) ON DELETE SET NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  audit_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_memory_summaries (
  memory_summary_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL REFERENCES radar_subjects(subject_id) ON DELETE CASCADE,
  task_id TEXT REFERENCES radar_tasks(task_id) ON DELETE SET NULL,
  memory_type TEXT NOT NULL,
  memory_json JSONB NOT NULL,
  generated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_radar_user_roles_subject ON radar_user_roles(subject_id);
CREATE INDEX IF NOT EXISTS idx_radar_authorizations_subject
  ON radar_data_authorizations(subject_id, status);
CREATE INDEX IF NOT EXISTS idx_radar_tasks_subject ON radar_tasks(subject_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_radar_tasks_idempotency
  ON radar_tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_radar_task_events_sequence
  ON radar_task_events(task_id, sequence);
CREATE INDEX IF NOT EXISTS idx_radar_confirmations_task
  ON radar_human_confirmations(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_radar_task_artifacts_task
  ON radar_task_artifact_versions(task_id, artifact_type, version_number);
CREATE INDEX IF NOT EXISTS idx_radar_snapshots_device_time ON radar_vital_snapshots(radar_device_id, measured_at);
CREATE INDEX IF NOT EXISTS idx_radar_night_summaries_subject ON radar_night_summaries(subject_id, night_of);
CREATE INDEX IF NOT EXISTS idx_radar_claims_task ON radar_evidence_claims(task_id);
CREATE INDEX IF NOT EXISTS idx_radar_a2a_task ON radar_a2a_messages(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_radar_artifacts_task ON radar_report_artifacts(task_id);
CREATE INDEX IF NOT EXISTS idx_radar_audit_task ON radar_audit_logs(task_id, created_at);

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('001_radar_agent_persistence', CURRENT_TIMESTAMP)
ON CONFLICT(version) DO NOTHING;
