-- Canonical SQLite test schema, squashed from development migrations 001-020.

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
CREATE INDEX IF NOT EXISTS idx_radar_artifacts_task ON radar_report_artifacts(task_id);
CREATE INDEX IF NOT EXISTS idx_radar_audit_task ON radar_audit_logs(task_id, created_at);

ALTER TABLE radar_tasks ADD COLUMN runtime_kind TEXT NOT NULL DEFAULT 'product_episode';
ALTER TABLE radar_tasks ADD COLUMN runtime_contract_version TEXT NOT NULL DEFAULT 'product-episode.v1';
ALTER TABLE radar_tasks ADD COLUMN execution_mode TEXT;
ALTER TABLE radar_tasks ADD COLUMN completion_status TEXT;
ALTER TABLE radar_tasks ADD COLUMN task_version INTEGER NOT NULL DEFAULT 1;

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

CREATE INDEX IF NOT EXISTS idx_radar_user_input_task
  ON radar_user_input_requests(task_id, created_at);

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

CREATE TABLE IF NOT EXISTS product_habit_question_episodes (
  episode_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  question_count INTEGER NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (episode_id, subject_id)
);

CREATE TABLE IF NOT EXISTS product_habit_question_selections (
  selection_id TEXT PRIMARY KEY,
  episode_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  role TEXT NOT NULL,
  receipt_json JSONB NOT NULL,
  consumed BOOLEAN NOT NULL DEFAULT FALSE,
  issued_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_product_habit_question_selections_episode
  ON product_habit_question_selections(episode_id, subject_id, issued_at);

CREATE TABLE IF NOT EXISTS product_habit_question_cooldowns (
  subject_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  role TEXT NOT NULL,
  trigger TEXT NOT NULL,
  concept_id TEXT NOT NULL,
  last_asked_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (subject_id, actor_id, role, trigger, concept_id)
);

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

CREATE TABLE IF NOT EXISTS sleep_domain_adapters (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  adapter_id TEXT NOT NULL,
  adapter_version TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  contract_version TEXT NOT NULL,
  configuration_fingerprint TEXT NOT NULL,
  deployment_status TEXT NOT NULL,
  descriptor_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (namespace_id, data_mode, adapter_id, adapter_version),
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE TABLE IF NOT EXISTS sleep_domain_provider_accounts (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  provider_account_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  configuration_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL,
  account_metadata_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (namespace_id, data_mode, provider_account_id),
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE TABLE IF NOT EXISTS sleep_domain_device_bindings (
  device_binding_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
  device_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  provider_account_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  timezone_name TEXT NOT NULL,
  effective_from TIMESTAMPTZ NOT NULL,
  effective_until TIMESTAMPTZ,
  status TEXT NOT NULL,
  binding_json JSONB NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL,
  UNIQUE (device_binding_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, device_id, binding_version),
  FOREIGN KEY (namespace_id, data_mode, provider_account_id)
    REFERENCES sleep_domain_provider_accounts (
      namespace_id, data_mode, provider_account_id
    ) ON DELETE RESTRICT,
  CHECK (effective_until IS NULL OR effective_until > effective_from),
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_binding_interval
  ON sleep_domain_device_bindings (
    namespace_id, data_mode, device_id, effective_from, effective_until
  );

CREATE TABLE IF NOT EXISTS sleep_domain_raw_inbox (
  raw_ingress_record_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  provider_id TEXT NOT NULL,
  provider_account_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message_id TEXT,
  request_signed_at TIMESTAMPTZ,
  measurement_at TIMESTAMPTZ,
  event_occurred_at TIMESTAMPTZ,
  received_at TIMESTAMPTZ NOT NULL,
  signature_profile TEXT,
  signature_verification TEXT NOT NULL,
  idempotency_identity TEXT NOT NULL,
  idempotency_version TEXT NOT NULL,
  pre_normalization_payload_sha256 TEXT NOT NULL,
  encrypted_payload BYTEA NOT NULL,
  encryption_key_id TEXT NOT NULL,
  encrypted_at TIMESTAMPTZ NOT NULL,
  content_type TEXT NOT NULL,
  payload_size_bytes INTEGER NOT NULL CHECK (payload_size_bytes >= 0),
  retention_until TIMESTAMPTZ NOT NULL,
  raw_metadata_json JSONB NOT NULL,
  UNIQUE (raw_ingress_record_id, namespace_id, data_mode),
  UNIQUE (
    namespace_id, data_mode, provider_account_id, idempotency_identity
  ),
  FOREIGN KEY (namespace_id, data_mode, provider_account_id)
    REFERENCES sleep_domain_provider_accounts (
      namespace_id, data_mode, provider_account_id
    ) ON DELETE RESTRICT,
  CHECK (retention_until > received_at),
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_raw_retention
  ON sleep_domain_raw_inbox (
    namespace_id, data_mode, retention_until, raw_ingress_record_id
  );

CREATE OR REPLACE FUNCTION sleep_domain_reject_raw_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'sleep_domain_raw_inbox is immutable';
END;
$$;

CREATE TRIGGER sleep_domain_raw_inbox_immutable_update
BEFORE UPDATE ON sleep_domain_raw_inbox
FOR EACH ROW EXECUTE FUNCTION sleep_domain_reject_raw_mutation();

CREATE TRIGGER sleep_domain_raw_inbox_immutable_delete
BEFORE DELETE ON sleep_domain_raw_inbox
FOR EACH ROW EXECUTE FUNCTION sleep_domain_reject_raw_mutation();

CREATE TABLE IF NOT EXISTS sleep_domain_processing_receipts (
  receipt_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  raw_ingress_record_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  outcome TEXT NOT NULL,
  quarantine_reason TEXT,
  receipt_json JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  UNIQUE (receipt_id, namespace_id, data_mode),
  FOREIGN KEY (raw_ingress_record_id, namespace_id, data_mode)
    REFERENCES sleep_domain_raw_inbox (
      raw_ingress_record_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_receipts_raw
  ON sleep_domain_processing_receipts (
    namespace_id, data_mode, raw_ingress_record_id, occurred_at, receipt_id
  );

CREATE TABLE IF NOT EXISTS sleep_domain_quarantine (
  quarantine_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  raw_ingress_record_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  detail_code TEXT,
  receipt_id TEXT NOT NULL,
  quarantine_json JSONB NOT NULL,
  quarantined_at TIMESTAMPTZ NOT NULL,
  UNIQUE (quarantine_id, namespace_id, data_mode),
  FOREIGN KEY (raw_ingress_record_id, namespace_id, data_mode)
    REFERENCES sleep_domain_raw_inbox (
      raw_ingress_record_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (receipt_id, namespace_id, data_mode)
    REFERENCES sleep_domain_processing_receipts (
      receipt_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_quarantine_raw
  ON sleep_domain_quarantine (
    namespace_id, data_mode, raw_ingress_record_id, quarantined_at
  );

CREATE TABLE IF NOT EXISTS sleep_domain_normalization_work (
  work_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  raw_ingress_record_id TEXT NOT NULL,
  work_generation INTEGER NOT NULL CHECK (work_generation >= 1),
  status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  available_at TIMESTAMPTZ NOT NULL,
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  last_error_code TEXT,
  work_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  UNIQUE (work_id, namespace_id, data_mode),
  UNIQUE (
    namespace_id, data_mode, raw_ingress_record_id, work_generation
  ),
  FOREIGN KEY (raw_ingress_record_id, namespace_id, data_mode)
    REFERENCES sleep_domain_raw_inbox (
      raw_ingress_record_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_normalization_queue
  ON sleep_domain_normalization_work (
    namespace_id, data_mode, status, available_at, lease_expires_at, created_at
  );

CREATE TABLE IF NOT EXISTS sleep_domain_processing_outbox (
  intent_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  raw_ingress_record_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  available_at TIMESTAMPTZ NOT NULL,
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  intent_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  delivered_at TIMESTAMPTZ,
  UNIQUE (intent_id, namespace_id, data_mode),
  FOREIGN KEY (raw_ingress_record_id, namespace_id, data_mode)
    REFERENCES sleep_domain_raw_inbox (
      raw_ingress_record_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_processing_outbox_queue
  ON sleep_domain_processing_outbox (
    namespace_id, data_mode, status, available_at, lease_expires_at, created_at
  );

CREATE TABLE IF NOT EXISTS sleep_domain_adapter_candidates (
  candidate_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  raw_ingress_record_id TEXT NOT NULL,
  provider_account_id TEXT NOT NULL,
  source_key TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  observation_type TEXT NOT NULL,
  candidate_json JSONB NOT NULL,
  received_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (candidate_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, source_key),
  UNIQUE (namespace_id, data_mode, idempotency_key),
  FOREIGN KEY (raw_ingress_record_id, namespace_id, data_mode)
    REFERENCES sleep_domain_raw_inbox (
      raw_ingress_record_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (namespace_id, data_mode, provider_account_id)
    REFERENCES sleep_domain_provider_accounts (
      namespace_id, data_mode, provider_account_id
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE TABLE IF NOT EXISTS sleep_domain_canonical_observations (
  observation_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  candidate_id TEXT NOT NULL,
  raw_ingress_record_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  device_binding_id TEXT NOT NULL,
  binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
  observation_type TEXT NOT NULL,
  source_key TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  observation_json JSONB NOT NULL,
  measurement_at TIMESTAMPTZ,
  event_occurred_at TIMESTAMPTZ,
  received_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (observation_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, idempotency_key),
  FOREIGN KEY (candidate_id, namespace_id, data_mode)
    REFERENCES sleep_domain_adapter_candidates (
      candidate_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (raw_ingress_record_id, namespace_id, data_mode)
    REFERENCES sleep_domain_raw_inbox (
      raw_ingress_record_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (device_binding_id, namespace_id, data_mode)
    REFERENCES sleep_domain_device_bindings (
      device_binding_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_observations_subject_time
  ON sleep_domain_canonical_observations (
    namespace_id, data_mode, subject_id, measurement_at, event_occurred_at
  );

CREATE TABLE IF NOT EXISTS sleep_domain_night_episodes (
  night_episode_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  night_key TEXT NOT NULL,
  state TEXT NOT NULL,
  current_revision_id TEXT,
  current_revision_number INTEGER,
  cas_version INTEGER NOT NULL DEFAULT 0 CHECK (cas_version >= 0),
  episode_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  UNIQUE (night_episode_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, subject_id, night_key),
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_night_subject
  ON sleep_domain_night_episodes (
    namespace_id, data_mode, subject_id, night_key
  );

CREATE TABLE IF NOT EXISTS sleep_domain_night_episode_revisions (
  night_episode_revision_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  night_episode_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
  parent_revision_id TEXT,
  revision_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (night_episode_revision_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, night_episode_id, revision_number),
  FOREIGN KEY (night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE TABLE IF NOT EXISTS sleep_domain_analysis_revisions (
  analysis_revision_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  night_episode_id TEXT NOT NULL,
  night_episode_revision_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
  parent_analysis_revision_id TEXT,
  analysis_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (analysis_revision_id, namespace_id, data_mode),
  UNIQUE (
    namespace_id, data_mode, night_episode_revision_id, revision_number
  ),
  FOREIGN KEY (night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (night_episode_revision_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episode_revisions (
      night_episode_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE TABLE IF NOT EXISTS sleep_domain_operations (
  operation_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  operation_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  service_principal_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  target_resource_id TEXT,
  target_resource_key TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  cas_version INTEGER NOT NULL DEFAULT 0 CHECK (cas_version >= 0),
  operation_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  UNIQUE (operation_id, namespace_id, data_mode),
  UNIQUE (
    namespace_id, data_mode, service_principal_id, actor_id,
    operation_type, target_resource_key, idempotency_key
  ),
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_operation_queue
  ON sleep_domain_operations (
    namespace_id, data_mode, status, lease_expires_at, created_at
  );

CREATE TABLE IF NOT EXISTS sleep_domain_domain_outbox (
  delivery_offset BIGSERIAL PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  event_type TEXT NOT NULL,
  aggregate_type TEXT NOT NULL,
  aggregate_id TEXT NOT NULL,
  aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 1),
  per_aggregate_sequence INTEGER NOT NULL CHECK (per_aggregate_sequence >= 1),
  subject_id TEXT NOT NULL,
  operation_id TEXT,
  status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  available_at TIMESTAMPTZ NOT NULL,
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  event_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  delivered_at TIMESTAMPTZ,
  UNIQUE (
    namespace_id, data_mode, aggregate_type, aggregate_id,
    per_aggregate_sequence
  ),
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_outbox_queue
  ON sleep_domain_domain_outbox (
    namespace_id, data_mode, status, available_at, lease_expires_at,
    delivery_offset
  );

CREATE TABLE IF NOT EXISTS sleep_domain_adapter_deployment_events (
  deployment_event_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  adapter_id TEXT NOT NULL,
  adapter_version TEXT NOT NULL,
  previous_status TEXT NOT NULL,
  deployment_status TEXT NOT NULL CHECK (
    deployment_status IN ('registered', 'enabled', 'disabled', 'superseded')
  ),
  event_json JSONB NOT NULL,
  changed_at TIMESTAMPTZ NOT NULL,
  UNIQUE (deployment_event_id, namespace_id, data_mode),
  FOREIGN KEY (namespace_id, data_mode, adapter_id, adapter_version)
    REFERENCES sleep_domain_adapters (
      namespace_id, data_mode, adapter_id, adapter_version
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_adapter_deployment_latest
  ON sleep_domain_adapter_deployment_events (
    namespace_id, data_mode, adapter_id, adapter_version,
    changed_at, deployment_event_id
  );

CREATE TABLE IF NOT EXISTS sleep_domain_capability_verification_receipts (
  receipt_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  adapter_id TEXT NOT NULL,
  adapter_version TEXT NOT NULL,
  capability TEXT NOT NULL,
  environment TEXT NOT NULL,
  verification_status TEXT NOT NULL CHECK (
    verification_status IN ('unverified', 'pending', 'verified', 'failed')
  ),
  receipt_json JSONB NOT NULL,
  reviewed_at TIMESTAMPTZ NOT NULL,
  UNIQUE (receipt_id, namespace_id, data_mode),
  FOREIGN KEY (namespace_id, data_mode, adapter_id, adapter_version)
    REFERENCES sleep_domain_adapters (
      namespace_id, data_mode, adapter_id, adapter_version
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_capability_verification_latest
  ON sleep_domain_capability_verification_receipts (
    namespace_id, data_mode, adapter_id, adapter_version,
    capability, environment, reviewed_at, receipt_id
  );

CREATE TABLE IF NOT EXISTS sleep_domain_adapter_resolution_locks (
  adapter_resolution_lock_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  provider_id TEXT NOT NULL,
  provider_account_id TEXT NOT NULL,
  adapter_id TEXT NOT NULL,
  adapter_version TEXT NOT NULL,
  environment TEXT NOT NULL,
  resolution_request_id TEXT NOT NULL,
  lock_json JSONB NOT NULL,
  resolved_at TIMESTAMPTZ NOT NULL,
  UNIQUE (adapter_resolution_lock_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, resolution_request_id),
  FOREIGN KEY (namespace_id, data_mode, adapter_id, adapter_version)
    REFERENCES sleep_domain_adapters (
      namespace_id, data_mode, adapter_id, adapter_version
    ) ON DELETE RESTRICT,
  FOREIGN KEY (namespace_id, data_mode, provider_account_id)
    REFERENCES sleep_domain_provider_accounts (
      namespace_id, data_mode, provider_account_id
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_adapter_resolution_retention
  ON sleep_domain_adapter_resolution_locks (
    namespace_id, data_mode, adapter_id, adapter_version, resolved_at
  );

CREATE TABLE IF NOT EXISTS sleep_domain_device_identities (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  provider_id TEXT NOT NULL,
  provider_account_id TEXT NOT NULL,
  provider_device_key TEXT NOT NULL,
  device_id TEXT NOT NULL,
  provider_device_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (
    namespace_id, data_mode, provider_id, provider_account_id,
    provider_device_key
  ),
  UNIQUE (namespace_id, data_mode, device_id),
  FOREIGN KEY (namespace_id, data_mode, provider_account_id)
    REFERENCES sleep_domain_provider_accounts (
      namespace_id, data_mode, provider_account_id
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE TABLE IF NOT EXISTS sleep_domain_device_binding_audit (
  audit_event_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  command_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('created', 'rebound')),
  provider_id TEXT NOT NULL,
  provider_account_id TEXT NOT NULL,
  provider_device_key TEXT NOT NULL,
  device_id TEXT NOT NULL,
  previous_binding_version INTEGER,
  new_binding_version INTEGER NOT NULL CHECK (new_binding_version >= 1),
  actor_id TEXT NOT NULL,
  authorization_id TEXT NOT NULL,
  audit_json JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  UNIQUE (audit_event_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, command_id),
  FOREIGN KEY (
    namespace_id, data_mode, provider_id, provider_account_id,
    provider_device_key
  ) REFERENCES sleep_domain_device_identities (
    namespace_id, data_mode, provider_id, provider_account_id,
    provider_device_key
  ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_binding_audit_device
  ON sleep_domain_device_binding_audit (
    namespace_id, data_mode, device_id, occurred_at, audit_event_id
  );

CREATE TABLE IF NOT EXISTS sleep_domain_quarantine_reprocess_audit (
  request_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  actor_id TEXT NOT NULL,
  authorization_id TEXT NOT NULL,
  request_json JSONB NOT NULL,
  requested_at TIMESTAMPTZ NOT NULL,
  UNIQUE (request_id, namespace_id, data_mode),
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE TABLE IF NOT EXISTS sleep_domain_ingress_nonces (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  provider_id TEXT NOT NULL,
  provider_account_id TEXT NOT NULL,
  compatibility_profile_id TEXT NOT NULL,
  nonce TEXT NOT NULL,
  idempotency_identity TEXT NOT NULL,
  pre_normalization_payload_sha256 TEXT NOT NULL,
  raw_ingress_record_id TEXT NOT NULL,
  request_signed_at TIMESTAMPTZ NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (
    namespace_id, data_mode, provider_id, provider_account_id,
    compatibility_profile_id, nonce
  ),
  FOREIGN KEY (raw_ingress_record_id, namespace_id, data_mode)
    REFERENCES sleep_domain_raw_inbox (
      raw_ingress_record_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_ingress_nonce_retention
  ON sleep_domain_ingress_nonces (
    namespace_id, data_mode, recorded_at, raw_ingress_record_id
  );

CREATE TABLE IF NOT EXISTS sleep_domain_source_reports (
  source_report_version_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  provider_id TEXT NOT NULL,
  provider_account_id TEXT NOT NULL,
  provider_device_key TEXT NOT NULL,
  local_report_date DATE NOT NULL,
  report_version INTEGER NOT NULL CHECK (report_version >= 1),
  content_sha256 TEXT NOT NULL,
  raw_ingress_record_id TEXT NOT NULL,
  is_empty BOOLEAN NOT NULL,
  fetched_at TIMESTAMPTZ NOT NULL,
  UNIQUE (
    namespace_id, data_mode, provider_id, provider_account_id,
    provider_device_key, local_report_date, report_version
  ),
  UNIQUE (
    namespace_id, data_mode, provider_id, provider_account_id,
    provider_device_key, local_report_date, content_sha256
  ),
  FOREIGN KEY (raw_ingress_record_id, namespace_id, data_mode)
    REFERENCES sleep_domain_raw_inbox (
      raw_ingress_record_id, namespace_id, data_mode
    ) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_source_reports_device_date
  ON sleep_domain_source_reports (
    namespace_id, data_mode, provider_account_id,
    provider_device_key, local_report_date, report_version
  );

CREATE TABLE IF NOT EXISTS sleep_domain_pull_checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  provider_id TEXT NOT NULL,
  provider_account_id TEXT NOT NULL,
  stream_key TEXT NOT NULL,
  cursor_at TIMESTAMPTZ NOT NULL,
  lateness_watermark_at TIMESTAMPTZ NOT NULL,
  cas_version INTEGER NOT NULL CHECK (cas_version >= 0),
  updated_at TIMESTAMPTZ NOT NULL,
  UNIQUE (
    namespace_id, data_mode, provider_id, provider_account_id, stream_key
  )
);

CREATE TABLE IF NOT EXISTS sleep_domain_observation_fact_values (
  fact_value_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  fact_slot_key TEXT NOT NULL,
  value_sha256 TEXT NOT NULL,
  observation_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (namespace_id, data_mode, fact_slot_key, value_sha256),
  FOREIGN KEY (observation_id, namespace_id, data_mode)
    REFERENCES sleep_domain_canonical_observations (
      observation_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (candidate_id, namespace_id, data_mode)
    REFERENCES sleep_domain_adapter_candidates (
      candidate_id, namespace_id, data_mode
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS sleep_domain_observation_acquisitions (
  acquisition_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  fact_slot_key TEXT NOT NULL,
  value_sha256 TEXT NOT NULL,
  observation_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  raw_ingress_record_id TEXT NOT NULL,
  acquisition_channel TEXT NOT NULL,
  acquired_at TIMESTAMPTZ NOT NULL,
  UNIQUE (
    namespace_id, data_mode, raw_ingress_record_id, candidate_id,
    fact_slot_key, value_sha256
  ),
  FOREIGN KEY (observation_id, namespace_id, data_mode)
    REFERENCES sleep_domain_canonical_observations (
      observation_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (candidate_id, namespace_id, data_mode)
    REFERENCES sleep_domain_adapter_candidates (
      candidate_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (raw_ingress_record_id, namespace_id, data_mode)
    REFERENCES sleep_domain_raw_inbox (
      raw_ingress_record_id, namespace_id, data_mode
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS sleep_domain_observation_conflicts (
  conflict_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  fact_slot_key TEXT NOT NULL,
  first_value_sha256 TEXT NOT NULL,
  second_value_sha256 TEXT NOT NULL,
  first_observation_id TEXT NOT NULL,
  second_observation_id TEXT NOT NULL,
  detected_at TIMESTAMPTZ NOT NULL,
  UNIQUE (
    namespace_id, data_mode, fact_slot_key,
    first_value_sha256, second_value_sha256
  ),
  CHECK (first_value_sha256 < second_value_sha256)
);

CREATE TABLE IF NOT EXISTS sleep_domain_monitoring_snapshots (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('dormant', 'active')),
  active_night_episode_id TEXT,
  cas_version INTEGER NOT NULL CHECK (cas_version >= 0),
  snapshot_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (namespace_id, data_mode, subject_id),
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sleep_domain_one_active_episode
  ON sleep_domain_monitoring_snapshots (
    namespace_id, data_mode, active_night_episode_id
  )
  WHERE active_night_episode_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS sleep_domain_subject_lifecycle_leases (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  lease_owner TEXT NOT NULL,
  lease_token TEXT NOT NULL,
  lease_expires_at TIMESTAMPTZ NOT NULL,
  acquired_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (namespace_id, data_mode, subject_id),
  UNIQUE (lease_token),
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_lifecycle_lease_expiry
  ON sleep_domain_subject_lifecycle_leases (
    namespace_id, data_mode, lease_expires_at, subject_id
  );

CREATE TABLE IF NOT EXISTS sleep_domain_lifecycle_transition_receipts (
  transition_receipt_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  component TEXT NOT NULL CHECK (
    component IN ('monitoring', 'night_episode')
  ),
  aggregate_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  trigger_id TEXT NOT NULL,
  from_state TEXT NOT NULL,
  to_state TEXT NOT NULL,
  receipt_json JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  committed_at TIMESTAMPTZ NOT NULL,
  UNIQUE (transition_receipt_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, component, aggregate_id, trigger_id),
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_transition_subject
  ON sleep_domain_lifecycle_transition_receipts (
    namespace_id, data_mode, subject_id, committed_at,
    transition_receipt_id
  );

CREATE TABLE IF NOT EXISTS sleep_domain_episode_observation_memberships (
  membership_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  night_episode_id TEXT NOT NULL,
  observation_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  device_binding_id TEXT NOT NULL,
  binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
  event_at TIMESTAMPTZ NOT NULL,
  received_at TIMESTAMPTZ NOT NULL,
  lateness_watermark_at TIMESTAMPTZ,
  late_after_watermark BOOLEAN NOT NULL,
  membership_json JSONB NOT NULL,
  associated_at TIMESTAMPTZ NOT NULL,
  UNIQUE (membership_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, observation_id),
  FOREIGN KEY (night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (observation_id, namespace_id, data_mode)
    REFERENCES sleep_domain_canonical_observations (
      observation_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (device_binding_id, namespace_id, data_mode)
    REFERENCES sleep_domain_device_bindings (
      device_binding_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_membership_episode_time
  ON sleep_domain_episode_observation_memberships (
    namespace_id, data_mode, night_episode_id, event_at, observation_id
  );

CREATE TABLE IF NOT EXISTS sleep_domain_episode_source_reports (
  link_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  night_episode_id TEXT NOT NULL,
  source_report_version_id TEXT NOT NULL,
  report_version INTEGER NOT NULL CHECK (report_version >= 1),
  content_sha256 TEXT NOT NULL,
  linked_at TIMESTAMPTZ NOT NULL,
  UNIQUE (link_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, source_report_version_id),
  FOREIGN KEY (night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (source_report_version_id)
    REFERENCES sleep_domain_source_reports (
      source_report_version_id
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_episode_reports
  ON sleep_domain_episode_source_reports (
    namespace_id, data_mode, night_episode_id, report_version
  );

CREATE TABLE IF NOT EXISTS sleep_domain_pending_episode_associations (
  association_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  association_kind TEXT NOT NULL CHECK (
    association_kind IN ('observation', 'source_report')
  ),
  source_resource_id TEXT NOT NULL,
  subject_id TEXT,
  status TEXT NOT NULL CHECK (
    status IN ('pending', 'associated', 'quarantined')
  ),
  reason_code TEXT NOT NULL,
  association_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  resolved_at TIMESTAMPTZ,
  UNIQUE (association_id, namespace_id, data_mode),
  UNIQUE (
    namespace_id, data_mode, association_kind, source_resource_id
  ),
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_pending_association
  ON sleep_domain_pending_episode_associations (
    namespace_id, data_mode, status, association_kind, created_at
  );

CREATE TABLE IF NOT EXISTS sleep_domain_revision_publications (
  publication_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  night_episode_id TEXT NOT NULL,
  night_episode_revision_id TEXT NOT NULL,
  trigger_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (publication_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, night_episode_id, trigger_id),
  FOREIGN KEY (night_episode_revision_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episode_revisions (
      night_episode_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE TABLE IF NOT EXISTS sleep_domain_quality_assessments (
  assessment_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  assessed_at TIMESTAMPTZ NOT NULL,
  policy_version TEXT NOT NULL,
  assessment_json JSONB NOT NULL,
  UNIQUE (assessment_id, namespace_id, data_mode),
  FOREIGN KEY (night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_quality_episode
  ON sleep_domain_quality_assessments (
    namespace_id, data_mode, night_episode_id, assessed_at, assessment_id
  );

CREATE TABLE IF NOT EXISTS sleep_domain_current_quality (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  assessment_id TEXT NOT NULL,
  cas_version INTEGER NOT NULL CHECK (cas_version >= 1),
  assessment_json JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (namespace_id, data_mode, night_episode_id),
  FOREIGN KEY (assessment_id, namespace_id, data_mode)
    REFERENCES sleep_domain_quality_assessments (
      assessment_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE TABLE IF NOT EXISTS sleep_domain_risk_assessments (
  current_risk_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  policy_version TEXT NOT NULL,
  risk_json JSONB NOT NULL,
  UNIQUE (current_risk_id, namespace_id, data_mode),
  FOREIGN KEY (night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_risk_episode
  ON sleep_domain_risk_assessments (
    namespace_id, data_mode, night_episode_id, observed_at, current_risk_id
  );

CREATE TABLE IF NOT EXISTS sleep_domain_current_risk (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  current_risk_id TEXT NOT NULL,
  cas_version INTEGER NOT NULL CHECK (cas_version >= 1),
  risk_json JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (namespace_id, data_mode, night_episode_id),
  FOREIGN KEY (current_risk_id, namespace_id, data_mode)
    REFERENCES sleep_domain_risk_assessments (
      current_risk_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE TABLE IF NOT EXISTS sleep_domain_vendor_alert_instances (
  alert_instance_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  provider_account_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  vendor_alert_instance_id TEXT NOT NULL,
  alert_code TEXT NOT NULL,
  is_open BOOLEAN NOT NULL,
  cas_version INTEGER NOT NULL CHECK (cas_version >= 1),
  instance_json JSONB NOT NULL,
  opened_at TIMESTAMPTZ NOT NULL,
  closed_at TIMESTAMPTZ,
  UNIQUE (alert_instance_id, namespace_id, data_mode),
  UNIQUE (
    namespace_id, data_mode, provider_id, provider_account_id,
    device_id, vendor_alert_instance_id
  ),
  FOREIGN KEY (night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_open_alerts
  ON sleep_domain_vendor_alert_instances (
    namespace_id, data_mode, subject_id, night_episode_id, is_open,
    alert_code, opened_at
  );

CREATE TABLE IF NOT EXISTS sleep_domain_alert_correlation_receipts (
  receipt_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  observation_id TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (
    outcome IN (
      'opened', 'duplicate_open', 'closed_unique_instance', 'orphan_stop',
      'unkeyed_signal'
    )
  ),
  receipt_json JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  persisted_at TIMESTAMPTZ NOT NULL,
  UNIQUE (receipt_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, observation_id),
  FOREIGN KEY (observation_id, namespace_id, data_mode)
    REFERENCES sleep_domain_canonical_observations (
      observation_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE TABLE IF NOT EXISTS sleep_domain_fast_path_signal_projections (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  signal_type TEXT NOT NULL CHECK (
    signal_type IN ('risk', 'bed', 'offline', 'quality')
  ),
  current_state TEXT NOT NULL,
  cas_version INTEGER NOT NULL CHECK (cas_version >= 1),
  projection_json JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (
    namespace_id, data_mode, night_episode_id, signal_type
  ),
  FOREIGN KEY (night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE TABLE IF NOT EXISTS sleep_domain_fast_path_signal_receipts (
  receipt_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  signal_type TEXT NOT NULL CHECK (
    signal_type IN ('risk', 'bed', 'offline', 'quality')
  ),
  evaluation_id TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (
    outcome IN (
      'emitted', 'suppressed_repeat', 'suppressed_hysteresis'
    )
  ),
  receipt_json JSONB NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  persisted_at TIMESTAMPTZ NOT NULL,
  UNIQUE (receipt_id, namespace_id, data_mode),
  UNIQUE (
    namespace_id, data_mode, night_episode_id, signal_type, evaluation_id
  ),
  FOREIGN KEY (night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_fast_path_receipts
  ON sleep_domain_fast_path_signal_receipts (
    namespace_id, data_mode, night_episode_id, signal_type, persisted_at
  );

CREATE TABLE IF NOT EXISTS sleep_domain_care_followups (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (
    state IN (
      'none', 'pending_feedback', 'following_up', 'completed', 'ended'
    )
  ),
  cas_version INTEGER NOT NULL CHECK (cas_version >= 1),
  snapshot_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (namespace_id, data_mode, night_episode_id),
  FOREIGN KEY (night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_open_followups
  ON sleep_domain_care_followups (
    namespace_id, data_mode, subject_id, state, updated_at
  );

CREATE TABLE IF NOT EXISTS sleep_domain_care_followup_transition_receipts (
  receipt_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  command_id TEXT NOT NULL,
  from_state TEXT NOT NULL,
  to_state TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  authorization_id TEXT NOT NULL,
  receipt_json JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  UNIQUE (receipt_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, night_episode_id, command_id),
  FOREIGN KEY (night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

ALTER TABLE sleep_domain_analysis_revisions
  ADD COLUMN analysis_run_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_sleep_domain_analysis_run
  ON sleep_domain_analysis_revisions (
    namespace_id, data_mode, analysis_run_id
  )
  WHERE analysis_run_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS sleep_domain_analysis_role_views (
  role_view_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  analysis_revision_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  night_episode_revision_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('elder', 'family', 'doctor')),
  status TEXT NOT NULL CHECK (
    status IN ('ready', 'degraded', 'pending', 'blocked')
  ),
  product_agent_episode_id TEXT NOT NULL,
  view_json JSONB NOT NULL,
  generated_at TIMESTAMPTZ NOT NULL,
  UNIQUE (role_view_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, analysis_revision_id, role),
  FOREIGN KEY (analysis_revision_id, namespace_id, data_mode)
    REFERENCES sleep_domain_analysis_revisions (
      analysis_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (night_episode_revision_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episode_revisions (
      night_episode_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_role_view_subject
  ON sleep_domain_analysis_role_views (
    namespace_id, data_mode, subject_id, role, generated_at
  );

CREATE INDEX IF NOT EXISTS idx_sleep_domain_agent_operation_fairness
  ON sleep_domain_operations (
    namespace_id, data_mode, operation_type, subject_id,
    status, lease_expires_at, updated_at, created_at
  );

CREATE TABLE IF NOT EXISTS sleep_api_actor_assertion_replays (
  issuer TEXT NOT NULL,
  assertion_id TEXT NOT NULL,
  nonce TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  consumed_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (issuer, assertion_id),
  UNIQUE (issuer, nonce)
);

CREATE INDEX IF NOT EXISTS idx_sleep_api_replay_expiry
  ON sleep_api_actor_assertion_replays (expires_at);

CREATE TABLE IF NOT EXISTS sleep_api_operation_commands (
  operation_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  route_template TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (
    actor_role IN ('elder', 'family', 'caregiver', 'doctor')
  ),
  authorization_id TEXT NOT NULL,
  authorization_epoch INTEGER NOT NULL CHECK (authorization_epoch >= 0),
  request_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (operation_id, namespace_id, data_mode)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE TABLE IF NOT EXISTS sleep_api_feedback (
  feedback_id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL UNIQUE,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  night_episode_revision_id TEXT,
  actor_id TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (
    actor_role IN ('elder', 'family', 'caregiver')
  ),
  authorization_id TEXT NOT NULL,
  authorization_epoch INTEGER NOT NULL CHECK (authorization_epoch >= 0),
  provenance_category TEXT NOT NULL CHECK (
    provenance_category IN (
      'elder_self_report', 'family_report', 'caregiver_report'
    )
  ),
  event_at TIMESTAMPTZ NOT NULL,
  received_at TIMESTAMPTZ NOT NULL,
  feedback_json JSONB NOT NULL,
  FOREIGN KEY (operation_id, namespace_id, data_mode)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_api_feedback_subject
  ON sleep_api_feedback (
    namespace_id, data_mode, subject_id, received_at
  );

CREATE TABLE IF NOT EXISTS sleep_api_event_cursor_sessions (
  cursor_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  consumer_service_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (
    actor_role IN ('elder', 'family', 'caregiver', 'doctor')
  ),
  scope_projection_sha256 TEXT NOT NULL,
  authorization_epoch INTEGER NOT NULL CHECK (authorization_epoch >= 0),
  event_schema_generation TEXT NOT NULL,
  issued_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  last_used_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ,
  revocation_reason TEXT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  )
);

CREATE INDEX IF NOT EXISTS idx_sleep_api_event_cursor_binding
  ON sleep_api_event_cursor_sessions (
    namespace_id, data_mode, consumer_service_id, subject_id, actor_role
  );

CREATE INDEX IF NOT EXISTS idx_sleep_api_event_cursor_expiry
  ON sleep_api_event_cursor_sessions (expires_at);

CREATE TABLE IF NOT EXISTS sleep_api_event_cursor_tombstones (
  cursor_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  effective_at TIMESTAMPTZ NOT NULL,
  reason_code TEXT NOT NULL,
  FOREIGN KEY (cursor_id)
    REFERENCES sleep_api_event_cursor_sessions (cursor_id) ON DELETE RESTRICT
);

ALTER TABLE radar_night_summaries
  ADD COLUMN source_night_episode_revision_id TEXT;

ALTER TABLE radar_night_summaries
  ADD COLUMN compatibility_projection_version TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_radar_night_summary_current_revision
  ON radar_night_summaries (source_night_episode_revision_id)
  WHERE source_night_episode_revision_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS sleep_domain_legacy_import_runs (
  import_run_id TEXT PRIMARY KEY,
  source_fingerprint_sha256 TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  confirmed_namespace_id TEXT NOT NULL,
  confirmed_data_mode TEXT NOT NULL CHECK (
    confirmed_data_mode IN ('live', 'replay')
  ),
  quarantine_namespace_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN ('running', 'succeeded', 'failed')
  ),
  source_record_count INTEGER NOT NULL CHECK (source_record_count >= 0),
  imported_record_count INTEGER NOT NULL CHECK (imported_record_count >= 0),
  quarantined_record_count INTEGER NOT NULL CHECK (
    quarantined_record_count >= 0
  ),
  duplicate_record_count INTEGER NOT NULL CHECK (duplicate_record_count >= 0),
  failure_code TEXT,
  started_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  UNIQUE (
    source_fingerprint_sha256, manifest_sha256,
    confirmed_namespace_id, quarantine_namespace_id
  )
);

CREATE TABLE IF NOT EXISTS sleep_domain_legacy_import_records (
  import_run_id TEXT NOT NULL,
  source_record_key TEXT NOT NULL,
  legacy_raw_event_id TEXT NOT NULL,
  target_namespace_id TEXT NOT NULL,
  target_data_mode TEXT NOT NULL CHECK (target_data_mode IN ('live', 'replay')),
  raw_ingress_record_id TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  legacy_normalized_event_type TEXT,
  legacy_normalized_payload_sha256 TEXT,
  disposition TEXT NOT NULL CHECK (
    disposition IN ('normalization_pending', 'quarantined', 'duplicate')
  ),
  quarantine_reason TEXT,
  detail_code TEXT,
  imported_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (import_run_id, source_record_key),
  FOREIGN KEY (import_run_id)
    REFERENCES sleep_domain_legacy_import_runs (import_run_id)
    ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_legacy_import_raw
  ON sleep_domain_legacy_import_records (
    target_namespace_id, target_data_mode, raw_ingress_record_id
  );

CREATE TABLE IF NOT EXISTS sleep_domain_shadow_read_comparisons (
  comparison_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  legacy_summary_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  night_episode_revision_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN ('matched', 'accepted_difference', 'failed')
  ),
  critical_mismatch_count INTEGER NOT NULL CHECK (
    critical_mismatch_count >= 0
  ),
  comparison_sha256 TEXT NOT NULL,
  comparison_json JSONB NOT NULL,
  compared_at TIMESTAMPTZ NOT NULL,
  UNIQUE (
    namespace_id, data_mode, legacy_summary_id,
    night_episode_revision_id, comparison_sha256
  )
);

CREATE TABLE IF NOT EXISTS sleep_domain_authority_cutover_events (
  cutover_event_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  phase TEXT NOT NULL CHECK (
    phase IN (
      'expanded', 'backfilled', 'shadow_verified',
      'cutover', 'rollback_rehearsed', 'rolled_back'
    )
  ),
  previous_authority TEXT NOT NULL CHECK (
    previous_authority IN ('legacy_compatibility', 'canonical')
  ),
  selected_authority TEXT NOT NULL CHECK (
    selected_authority IN ('legacy_compatibility', 'canonical')
  ),
  configuration_sha256 TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  event_json JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS sleep_domain_authority_cutover_state (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  phase TEXT NOT NULL CHECK (
    phase IN (
      'expanded', 'backfilled', 'shadow_verified',
      'cutover', 'rollback_rehearsed', 'rolled_back'
    )
  ),
  selected_authority TEXT NOT NULL CHECK (
    selected_authority IN ('legacy_compatibility', 'canonical')
  ),
  configuration_sha256 TEXT NOT NULL,
  state_version INTEGER NOT NULL CHECK (state_version >= 1),
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (namespace_id, data_mode)
);

CREATE INDEX IF NOT EXISTS idx_sleep_domain_cutover_phase
  ON sleep_domain_authority_cutover_events (
    namespace_id, data_mode, occurred_at, cutover_event_id
  );


CREATE TABLE IF NOT EXISTS sleepagent_schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);
INSERT INTO sleepagent_schema_migrations (version, applied_at)
VALUES ('001_initial_schema', CURRENT_TIMESTAMP)
ON CONFLICT(version) DO NOTHING;
