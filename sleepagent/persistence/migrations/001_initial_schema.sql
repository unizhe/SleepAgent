-- sleepagent:transactional=true
-- Canonical SleepAgent schema baseline, squashed from development migrations 001-025.

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

-- sleepagent:transactional=true
-- The bootstrap runner validates this file against its release-pinned SHA-256
-- before executing the canonical baseline.

CREATE TABLE IF NOT EXISTS sleepagent_schema_migrations (
  version INTEGER PRIMARY KEY CHECK (version >= 1),
  migration_name TEXT NOT NULL UNIQUE,
  sql_sha256 TEXT NOT NULL CHECK (
    sql_sha256 ~ '^[0-9a-f]{64}$'
  ),
  status TEXT NOT NULL CHECK (
    status IN ('started', 'applied', 'failed')
  ),
  transactional BOOLEAN NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  finished_at TIMESTAMPTZ,
  applied_by TEXT NOT NULL,
  error_code TEXT,
  CHECK (
    (status = 'started' AND finished_at IS NULL)
    OR (status IN ('applied', 'failed')
        AND finished_at IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_sleepagent_migration_status
  ON sleepagent_schema_migrations (status, version);

CREATE OR REPLACE FUNCTION sleepagent_protect_migration_ledger()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'migration ledger rows are immutable';
  END IF;
  IF NEW.version <> OLD.version
     OR NEW.migration_name <> OLD.migration_name
     OR NEW.sql_sha256 <> OLD.sql_sha256
     OR NEW.transactional <> OLD.transactional
     OR NEW.started_at <> OLD.started_at
     OR NEW.applied_by <> OLD.applied_by THEN
    RAISE EXCEPTION 'migration identity is immutable';
  END IF;
  IF OLD.status IN ('applied', 'failed') THEN
    RAISE EXCEPTION 'finished migration rows are immutable';
  END IF;
  IF OLD.status = 'started'
     AND NEW.status NOT IN ('applied', 'failed') THEN
    RAISE EXCEPTION 'started migration may only finish applied or failed';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS sleepagent_migration_immutable
  ON sleepagent_schema_migrations;

CREATE TRIGGER sleepagent_migration_immutable
BEFORE UPDATE OR DELETE ON sleepagent_schema_migrations
FOR EACH ROW EXECUTE FUNCTION sleepagent_protect_migration_ledger();


-- sleepagent:transactional=true
-- Database-owned namespace, identity, grant and governance epochs.

CREATE TABLE IF NOT EXISTS backend_namespaces (
  namespace_id TEXT PRIMARY KEY,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  current_generation BIGINT NOT NULL DEFAULT 1 CHECK (current_generation >= 1),
  status TEXT NOT NULL DEFAULT 'active' CHECK (
    status IN ('active', 'suspended', 'retired')
  ),
  synthetic_non_release BOOLEAN NOT NULL,
  max_worker_concurrency INTEGER NOT NULL DEFAULT 4 CHECK (
    max_worker_concurrency BETWEEN 1 AND 256
  ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (namespace_id, data_mode),
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%'
      AND synthetic_non_release = FALSE)
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%'
      AND synthetic_non_release = TRUE)
  )
);

CREATE TABLE IF NOT EXISTS backend_namespace_generations (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  generation BIGINT NOT NULL CHECK (generation >= 1),
  status TEXT NOT NULL CHECK (status IN ('active', 'sealed', 'retired')),
  configuration_sha256 TEXT NOT NULL CHECK (
    configuration_sha256 ~ '^[0-9a-f]{64}$'
  ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  sealed_at TIMESTAMPTZ,
  PRIMARY KEY (namespace_id, data_mode, generation),
  FOREIGN KEY (namespace_id, data_mode)
    REFERENCES backend_namespaces (namespace_id, data_mode) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_backend_active_namespace_generation
  ON backend_namespace_generations (namespace_id, data_mode)
  WHERE status = 'active';

CREATE TABLE IF NOT EXISTS backend_replay_runs (
  run_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  scenario_id TEXT NOT NULL,
  scenario_sha256 TEXT NOT NULL CHECK (scenario_sha256 ~ '^[0-9a-f]{64}$'),
  generation INTEGER NOT NULL CHECK (generation >= 1),
  status TEXT NOT NULL CHECK (status IN ('active', 'sealed', 'reset')),
  synthetic_non_release BOOLEAN NOT NULL DEFAULT TRUE CHECK (
    synthetic_non_release = TRUE
  ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  sealed_at TIMESTAMPTZ,
  UNIQUE (namespace_id, namespace_generation, run_id),
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
    REFERENCES backend_namespace_generations (
      namespace_id, data_mode, generation
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS backend_replay_arms (
  arm_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES backend_replay_runs(run_id) ON DELETE RESTRICT,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  arm_name TEXT NOT NULL,
  configuration_sha256 TEXT NOT NULL CHECK (
    configuration_sha256 ~ '^[0-9a-f]{64}$'
  ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (run_id, arm_name),
  UNIQUE (namespace_id, namespace_generation, run_id, arm_id),
  FOREIGN KEY (namespace_id, namespace_generation, run_id)
    REFERENCES backend_replay_runs (
      namespace_id, namespace_generation, run_id
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS backend_service_principals (
  principal_id TEXT PRIMARY KEY,
  database_role_name NAME NOT NULL UNIQUE,
  principal_kind TEXT NOT NULL CHECK (
    principal_kind IN ('bff', 'external_service', 'worker', 'demo_controller')
  ),
  status TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'revoked')),
  credential_generation INTEGER NOT NULL DEFAULT 1 CHECK (
    credential_generation >= 1
  ),
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS backend_actors (
  actor_id TEXT PRIMARY KEY,
  actor_kind TEXT NOT NULL CHECK (actor_kind IN ('human', 'service')),
  status TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'revoked')),
  external_issuer TEXT,
  external_subject TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (external_issuer, external_subject)
);

CREATE TABLE IF NOT EXISTS backend_subjects (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  timezone_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'forgotten')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (namespace_id, data_mode, subject_id),
  FOREIGN KEY (namespace_id, data_mode)
    REFERENCES backend_namespaces (namespace_id, data_mode) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS backend_actor_subject_bindings (
  binding_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  actor_id TEXT NOT NULL REFERENCES backend_actors(actor_id) ON DELETE RESTRICT,
  subject_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('elder', 'family', 'doctor')),
  status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired')),
  purpose_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  scopes_json JSONB NOT NULL,
  authorization_epoch BIGINT NOT NULL DEFAULT 1 CHECK (
    authorization_epoch >= 1
  ),
  valid_from TIMESTAMPTZ NOT NULL,
  valid_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (namespace_id, data_mode, actor_id, subject_id, role),
  FOREIGN KEY (namespace_id, data_mode, subject_id)
    REFERENCES backend_subjects (namespace_id, data_mode, subject_id)
    ON DELETE RESTRICT,
  CHECK (jsonb_typeof(purpose_json) = 'array'),
  CHECK (jsonb_typeof(scopes_json) = 'array'),
  CHECK (valid_until IS NULL OR valid_until > valid_from)
);

CREATE TABLE IF NOT EXISTS backend_principal_grants (
  grant_id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL
    REFERENCES backend_service_principals(principal_id) ON DELETE RESTRICT,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  purpose TEXT NOT NULL,
  scopes_json JSONB NOT NULL,
  allowed_handlers_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  authorization_epoch BIGINT NOT NULL DEFAULT 1 CHECK (
    authorization_epoch >= 1
  ),
  status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired')),
  valid_from TIMESTAMPTZ NOT NULL,
  valid_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (principal_id, namespace_id, data_mode, purpose),
  FOREIGN KEY (namespace_id, data_mode)
    REFERENCES backend_namespaces (namespace_id, data_mode) ON DELETE RESTRICT,
  CHECK (jsonb_typeof(scopes_json) = 'array'),
  CHECK (jsonb_typeof(allowed_handlers_json) = 'array'),
  CHECK (valid_until IS NULL OR valid_until > valid_from)
);

CREATE TABLE IF NOT EXISTS backend_subject_epochs (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  authorization_epoch BIGINT NOT NULL DEFAULT 1 CHECK (
    authorization_epoch >= 1
  ),
  privacy_epoch BIGINT NOT NULL DEFAULT 1 CHECK (privacy_epoch >= 1),
  retrieval_policy_epoch BIGINT NOT NULL DEFAULT 1 CHECK (
    retrieval_policy_epoch >= 1
  ),
  cas_version BIGINT NOT NULL DEFAULT 1 CHECK (cas_version >= 1),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (namespace_id, data_mode, subject_id),
  FOREIGN KEY (namespace_id, data_mode, subject_id)
    REFERENCES backend_subjects (namespace_id, data_mode, subject_id)
    ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS backend_authorization_audit (
  audit_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  actor_id TEXT,
  binding_id TEXT,
  decision TEXT NOT NULL CHECK (decision IN ('allow', 'deny', 'revoke')),
  reason_code TEXT NOT NULL,
  policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  authorization_epoch BIGINT NOT NULL,
  privacy_epoch BIGINT NOT NULL,
  retrieval_policy_epoch BIGINT NOT NULL,
  audit_json JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  UNIQUE (namespace_id, data_mode, audit_id)
);

CREATE OR REPLACE FUNCTION sleepagent_scope_setting(setting_name TEXT)
RETURNS TEXT
LANGUAGE SQL
STABLE
PARALLEL SAFE
AS $$
  SELECT NULLIF(current_setting(setting_name, TRUE), '')
$$;

CREATE OR REPLACE FUNCTION sleepagent_principal_context_allows()
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT EXISTS (
    SELECT 1
    FROM public.backend_service_principals AS principal
    WHERE principal.principal_id =
      NULLIF(current_setting('sleepagent.service_principal_id', TRUE), '')
      AND principal.database_role_name::text = session_user::text
      AND principal.status = 'active'
      AND (
        (NULLIF(current_setting('sleepagent.process_role', TRUE), '') =
          'worker' AND principal.principal_kind = 'worker')
        OR
        (NULLIF(current_setting('sleepagent.process_role', TRUE), '') = 'api'
          AND principal.principal_kind IN (
            'bff', 'external_service', 'demo_controller'
          ))
      )
  )
$$;

CREATE OR REPLACE FUNCTION sleepagent_namespace_scope_allows(
  row_namespace_id TEXT,
  row_data_mode TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT
    public.sleepagent_principal_context_allows()
    AND NULLIF(current_setting('sleepagent.namespace_id', TRUE), '') =
      row_namespace_id
    AND NULLIF(current_setting('sleepagent.data_mode', TRUE), '') =
      row_data_mode
    AND EXISTS (
      SELECT 1
      FROM public.backend_principal_grants AS grant_row
      WHERE grant_row.principal_id = NULLIF(
          current_setting('sleepagent.service_principal_id', TRUE), ''
        )
        AND grant_row.namespace_id = row_namespace_id
        AND grant_row.data_mode = row_data_mode
        AND grant_row.purpose = NULLIF(
          current_setting('sleepagent.purpose', TRUE), ''
        )
        AND grant_row.authorization_epoch::text = NULLIF(
          current_setting('sleepagent.authorization_epoch', TRUE), ''
        )
        AND grant_row.status = 'active'
        AND grant_row.valid_from <= CURRENT_TIMESTAMP
        AND (
          grant_row.valid_until IS NULL
          OR grant_row.valid_until > CURRENT_TIMESTAMP
        )
    )
$$;

CREATE OR REPLACE FUNCTION sleepagent_namespace_generation_scope_allows(
  row_namespace_id TEXT,
  row_data_mode TEXT,
  row_namespace_generation BIGINT,
  row_run_id TEXT,
  row_arm_id TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT
    public.sleepagent_namespace_scope_allows(row_namespace_id, row_data_mode)
    AND row_namespace_generation::text = NULLIF(
      current_setting('sleepagent.namespace_generation', TRUE), ''
    )
    AND EXISTS (
      SELECT 1
      FROM public.backend_namespace_generations AS generation_row
      WHERE generation_row.namespace_id = row_namespace_id
        AND generation_row.data_mode = row_data_mode
        AND generation_row.generation = row_namespace_generation
        AND generation_row.status IN ('active', 'sealed')
    )
    AND (
      (row_data_mode = 'live'
        AND row_run_id IS NULL
        AND row_arm_id IS NULL
        AND NULLIF(current_setting('sleepagent.run_id', TRUE), '') IS NULL
        AND NULLIF(current_setting('sleepagent.arm_id', TRUE), '') IS NULL)
      OR
      (row_data_mode = 'replay'
        AND row_run_id = NULLIF(
          current_setting('sleepagent.run_id', TRUE), ''
        )
        AND row_arm_id = NULLIF(
          current_setting('sleepagent.arm_id', TRUE), ''
        )
        AND EXISTS (
          SELECT 1
          FROM public.backend_replay_arms AS replay_arm
          WHERE replay_arm.namespace_id = row_namespace_id
            AND replay_arm.data_mode = row_data_mode
            AND replay_arm.namespace_generation = row_namespace_generation
            AND replay_arm.run_id = row_run_id
            AND replay_arm.arm_id = row_arm_id
        ))
    )
$$;

CREATE OR REPLACE FUNCTION sleepagent_subject_scope_allows(
  row_namespace_id TEXT,
  row_data_mode TEXT,
  row_subject_id TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT
    public.sleepagent_namespace_scope_allows(row_namespace_id, row_data_mode)
    AND NULLIF(current_setting('sleepagent.subject_id', TRUE), '') =
      row_subject_id
    AND EXISTS (
      SELECT 1
      FROM public.backend_subject_epochs AS epoch_row
      WHERE epoch_row.namespace_id = row_namespace_id
        AND epoch_row.data_mode = row_data_mode
        AND epoch_row.subject_id = row_subject_id
        AND epoch_row.authorization_epoch::text = NULLIF(
          current_setting('sleepagent.authorization_epoch', TRUE), ''
        )
        AND epoch_row.privacy_epoch::text = NULLIF(
          current_setting('sleepagent.privacy_epoch', TRUE), ''
        )
        AND epoch_row.retrieval_policy_epoch::text = NULLIF(
          current_setting('sleepagent.retrieval_policy_epoch', TRUE), ''
        )
    )
    AND (
      (NULLIF(current_setting('sleepagent.process_role', TRUE), '') = 'worker'
        AND NULLIF(current_setting('sleepagent.actor_id', TRUE), '') IS NULL)
      OR
      (NULLIF(current_setting('sleepagent.process_role', TRUE), '') = 'api'
        AND EXISTS (
          SELECT 1
          FROM public.backend_actors AS actor_row
          JOIN public.backend_actor_subject_bindings AS binding_row
            ON binding_row.actor_id = actor_row.actor_id
          WHERE actor_row.actor_id = NULLIF(
              current_setting('sleepagent.actor_id', TRUE), ''
            )
            AND actor_row.status = 'active'
            AND binding_row.namespace_id = row_namespace_id
            AND binding_row.data_mode = row_data_mode
            AND binding_row.subject_id = row_subject_id
            AND binding_row.status = 'active'
            AND binding_row.authorization_epoch::text = NULLIF(
              current_setting('sleepagent.authorization_epoch', TRUE), ''
            )
            AND binding_row.valid_from <= CURRENT_TIMESTAMP
            AND (
              binding_row.valid_until IS NULL
              OR binding_row.valid_until > CURRENT_TIMESTAMP
            )
            AND binding_row.purpose_json ? NULLIF(
              current_setting('sleepagent.purpose', TRUE), ''
            )
        ))
    )
$$;

CREATE OR REPLACE FUNCTION sleepagent_subject_generation_scope_allows(
  row_namespace_id TEXT,
  row_data_mode TEXT,
  row_subject_id TEXT,
  row_namespace_generation BIGINT,
  row_run_id TEXT,
  row_arm_id TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT
    public.sleepagent_subject_scope_allows(
      row_namespace_id, row_data_mode, row_subject_id
    )
    AND public.sleepagent_namespace_generation_scope_allows(
      row_namespace_id, row_data_mode, row_namespace_generation,
      row_run_id, row_arm_id
    )
$$;

-- Pre-scope authority resolution is deliberately narrow: the signed actor
-- assertion supplies actor/subject/role, while namespace and epochs come only
-- from active database authority.  Zero or ambiguous matches fail closed.
CREATE OR REPLACE FUNCTION sleepagent_resolve_actor_authority(
  asserted_actor_id TEXT,
  asserted_subject_id TEXT,
  asserted_role TEXT,
  requested_purpose TEXT
)
RETURNS TABLE (
  namespace_id TEXT,
  data_mode TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  binding_id TEXT,
  role TEXT,
  effective_scopes_json JSONB,
  authorization_epoch BIGINT,
  privacy_epoch BIGINT,
  retrieval_policy_epoch BIGINT
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  matched_rows INTEGER;
  trusted_principal TEXT := NULLIF(
    current_setting('sleepagent.service_principal_id', TRUE), ''
  );
  trusted_data_mode TEXT := NULLIF(
    current_setting('sleepagent.data_mode', TRUE), ''
  );
BEGIN
  IF asserted_actor_id IS NULL OR asserted_actor_id = ''
     OR asserted_subject_id IS NULL OR asserted_subject_id = ''
     OR asserted_role NOT IN ('elder', 'family', 'doctor')
     OR requested_purpose IS NULL OR requested_purpose = ''
     OR requested_purpose <> NULLIF(
       current_setting('sleepagent.purpose', TRUE), ''
     )
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'api'
     OR trusted_data_mode NOT IN ('live', 'replay')
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid or untrusted authority resolution context';
  END IF;

  RETURN QUERY
  SELECT grant_row.namespace_id,
    grant_row.data_mode,
    namespace_row.current_generation,
    replay_run.run_id,
    replay_arm.arm_id,
    binding_row.binding_id,
    binding_row.role,
    COALESCE(
      (
        SELECT jsonb_agg(granted.scope_value ORDER BY granted.scope_value)
        FROM jsonb_array_elements_text(
          grant_row.scopes_json
        ) AS granted(scope_value)
        WHERE binding_row.scopes_json ? granted.scope_value
      ),
      '[]'::jsonb
    ),
    epoch_row.authorization_epoch,
    epoch_row.privacy_epoch,
    epoch_row.retrieval_policy_epoch
  FROM public.backend_principal_grants AS grant_row
  JOIN public.backend_namespaces AS namespace_row
    ON namespace_row.namespace_id = grant_row.namespace_id
   AND namespace_row.data_mode = grant_row.data_mode
  JOIN public.backend_actor_subject_bindings AS binding_row
    ON binding_row.namespace_id = grant_row.namespace_id
   AND binding_row.data_mode = grant_row.data_mode
   AND binding_row.actor_id = asserted_actor_id
   AND binding_row.subject_id = asserted_subject_id
   AND binding_row.role = asserted_role
  JOIN public.backend_actors AS actor_row
    ON actor_row.actor_id = binding_row.actor_id
  JOIN public.backend_subjects AS subject_row
    ON subject_row.namespace_id = binding_row.namespace_id
   AND subject_row.data_mode = binding_row.data_mode
   AND subject_row.subject_id = binding_row.subject_id
  JOIN public.backend_subject_epochs AS epoch_row
    ON epoch_row.namespace_id = binding_row.namespace_id
   AND epoch_row.data_mode = binding_row.data_mode
   AND epoch_row.subject_id = binding_row.subject_id
  LEFT JOIN public.backend_replay_runs AS replay_run
    ON grant_row.data_mode = 'replay'
   AND replay_run.namespace_id = grant_row.namespace_id
   AND replay_run.namespace_generation = namespace_row.current_generation
   AND replay_run.status = 'active'
  LEFT JOIN public.backend_replay_arms AS replay_arm
    ON replay_arm.namespace_id = replay_run.namespace_id
   AND replay_arm.namespace_generation = replay_run.namespace_generation
   AND replay_arm.run_id = replay_run.run_id
  WHERE grant_row.principal_id = trusted_principal
    AND grant_row.data_mode = trusted_data_mode
    AND grant_row.purpose = requested_purpose
    AND grant_row.status = 'active'
    AND grant_row.authorization_epoch = epoch_row.authorization_epoch
    AND grant_row.valid_from <= CURRENT_TIMESTAMP
    AND (
      grant_row.valid_until IS NULL
      OR grant_row.valid_until > CURRENT_TIMESTAMP
    )
    AND namespace_row.status = 'active'
    AND actor_row.status = 'active'
    AND subject_row.status = 'active'
    AND binding_row.status = 'active'
    AND binding_row.authorization_epoch = epoch_row.authorization_epoch
    AND binding_row.purpose_json ? requested_purpose
    AND binding_row.valid_from <= CURRENT_TIMESTAMP
    AND (
      binding_row.valid_until IS NULL
      OR binding_row.valid_until > CURRENT_TIMESTAMP
    )
    AND (
      (grant_row.data_mode = 'live'
        AND replay_run.run_id IS NULL AND replay_arm.arm_id IS NULL)
      OR (grant_row.data_mode = 'replay'
        AND replay_run.run_id IS NOT NULL AND replay_arm.arm_id IS NOT NULL)
    );
  GET DIAGNOSTICS matched_rows = ROW_COUNT;
  IF matched_rows <> 1 THEN
    RAISE EXCEPTION 'authority resolution returned % rows', matched_rows;
  END IF;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_resolve_actor_authority(
  TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;

-- Assertion replay protection remains globally unique by issuer/JTI and
-- issuer/nonce.  API roles have no direct table privilege; this narrow entry
-- point binds consumption to a registered database principal and uses the
-- PostgreSQL control clock for both expiry and consumption time.
CREATE OR REPLACE FUNCTION sleepagent_consume_actor_assertion(
  asserted_issuer TEXT,
  asserted_jti TEXT,
  asserted_nonce TEXT,
  asserted_expires_at TIMESTAMPTZ
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  inserted_rows INTEGER;
  control_now TIMESTAMPTZ := clock_timestamp();
BEGIN
  IF asserted_issuer IS NULL OR asserted_issuer = ''
     OR asserted_jti IS NULL OR asserted_jti = ''
     OR asserted_nonce IS NULL OR asserted_nonce = ''
     OR asserted_expires_at IS NULL
     OR asserted_expires_at <= control_now
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'api'
     OR COALESCE(
       NULLIF(current_setting('sleepagent.data_mode', TRUE), ''), ''
     ) NOT IN ('live', 'replay')
     OR NULLIF(current_setting('sleepagent.purpose', TRUE), '') IS NULL
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid or untrusted actor assertion context';
  END IF;

  DELETE FROM public.sleep_api_actor_assertion_replays
  WHERE expires_at <= control_now;

  INSERT INTO public.sleep_api_actor_assertion_replays (
    issuer, assertion_id, nonce, expires_at, consumed_at
  ) VALUES (
    asserted_issuer, asserted_jti, asserted_nonce,
    asserted_expires_at, control_now
  )
  ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS inserted_rows = ROW_COUNT;
  RETURN inserted_rows = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_consume_actor_assertion(
  TEXT, TEXT, TEXT, TIMESTAMPTZ
) FROM PUBLIC;

-- Existing product-state tables predate namespace columns.  New production
-- writes must populate these columns; null legacy rows remain migration-owner
-- only until an explicit compatibility migration has scoped them.
ALTER TABLE product_habit_profile_states
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_habit_profile_states
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_care_context_states
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_care_context_states
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_memory_context_states
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_memory_context_states
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_episode_result_revisions
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_episode_result_revisions
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_induction_manifests
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_induction_manifests
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_episode_digests
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_episode_digests
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_memory_read_receipts
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_memory_read_receipts
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_pending_profile_candidates
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_pending_profile_candidates
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_human_decisions
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_human_decisions
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_pending_habit_change_sets
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_pending_habit_change_sets
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_longitudinal_subject_epochs
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_longitudinal_subject_epochs
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_offline_skill_outcomes
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_offline_skill_outcomes
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );

-- RLS is deliberately forced: API and Worker roles must always set exact
-- transaction-local scope; only the migration owner may bypass it.
ALTER TABLE backend_namespaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_namespaces FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_namespaces_scope ON backend_namespaces;
CREATE POLICY backend_namespaces_scope ON backend_namespaces
  USING (sleepagent_namespace_scope_allows(namespace_id, data_mode))
  WITH CHECK (sleepagent_namespace_scope_allows(namespace_id, data_mode));

ALTER TABLE backend_namespace_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_namespace_generations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_namespace_generations_scope
  ON backend_namespace_generations;
CREATE POLICY backend_namespace_generations_scope
  ON backend_namespace_generations
  USING (
    sleepagent_namespace_scope_allows(namespace_id, data_mode)
    AND generation::text = sleepagent_scope_setting(
      'sleepagent.namespace_generation'
    )
  )
  WITH CHECK (
    sleepagent_namespace_scope_allows(namespace_id, data_mode)
    AND generation::text = sleepagent_scope_setting(
      'sleepagent.namespace_generation'
    )
  );

ALTER TABLE backend_replay_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_replay_runs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_replay_runs_scope ON backend_replay_runs;
CREATE POLICY backend_replay_runs_scope ON backend_replay_runs
  USING (
    sleepagent_namespace_scope_allows(namespace_id, data_mode)
    AND namespace_generation::text = sleepagent_scope_setting(
      'sleepagent.namespace_generation'
    )
    AND run_id = sleepagent_scope_setting('sleepagent.run_id')
  )
  WITH CHECK (
    sleepagent_namespace_scope_allows(namespace_id, data_mode)
    AND namespace_generation::text = sleepagent_scope_setting(
      'sleepagent.namespace_generation'
    )
    AND run_id = sleepagent_scope_setting('sleepagent.run_id')
  );

ALTER TABLE backend_replay_arms ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_replay_arms FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_replay_arms_scope ON backend_replay_arms;
CREATE POLICY backend_replay_arms_scope ON backend_replay_arms
  USING (
    sleepagent_namespace_generation_scope_allows(
      namespace_id, data_mode, namespace_generation, run_id, arm_id
    )
  )
  WITH CHECK (
    sleepagent_namespace_generation_scope_allows(
      namespace_id, data_mode, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_service_principals ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_service_principals FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_service_principal_self
  ON backend_service_principals;
CREATE POLICY backend_service_principal_self ON backend_service_principals
  USING (
    sleepagent_principal_context_allows()
    AND
    principal_id = sleepagent_scope_setting('sleepagent.service_principal_id')
  );

ALTER TABLE backend_actors ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_actors FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_actor_self ON backend_actors;
CREATE POLICY backend_actor_self ON backend_actors
  USING (
    sleepagent_principal_context_allows()
    AND actor_id = sleepagent_scope_setting('sleepagent.actor_id')
  );

ALTER TABLE backend_subjects ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_subjects FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_subjects_scope ON backend_subjects;
CREATE POLICY backend_subjects_scope ON backend_subjects
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE backend_actor_subject_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_actor_subject_bindings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_actor_subject_binding_scope
  ON backend_actor_subject_bindings;
CREATE POLICY backend_actor_subject_binding_scope
  ON backend_actor_subject_bindings
  USING (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
    AND (
      actor_id = sleepagent_scope_setting('sleepagent.actor_id')
      OR sleepagent_scope_setting('sleepagent.process_role') = 'worker'
    )
  )
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE backend_principal_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_principal_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_principal_grant_scope
  ON backend_principal_grants;
CREATE POLICY backend_principal_grant_scope ON backend_principal_grants
  USING (
    sleepagent_namespace_scope_allows(namespace_id, data_mode)
    AND principal_id =
      sleepagent_scope_setting('sleepagent.service_principal_id')
  );

ALTER TABLE backend_subject_epochs ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_subject_epochs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_subject_epoch_scope ON backend_subject_epochs;
CREATE POLICY backend_subject_epoch_scope ON backend_subject_epochs
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE backend_authorization_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_authorization_audit FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_authorization_audit_scope
  ON backend_authorization_audit;
CREATE POLICY backend_authorization_audit_scope ON backend_authorization_audit
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

-- Canonical health records.
ALTER TABLE sleep_domain_raw_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_raw_inbox FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_raw_inbox_scope ON sleep_domain_raw_inbox;
CREATE POLICY sleep_domain_raw_inbox_scope ON sleep_domain_raw_inbox
  USING (sleepagent_namespace_scope_allows(namespace_id, data_mode))
  WITH CHECK (sleepagent_namespace_scope_allows(namespace_id, data_mode));

ALTER TABLE sleep_domain_canonical_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_canonical_observations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_observation_scope
  ON sleep_domain_canonical_observations;
CREATE POLICY sleep_domain_observation_scope
  ON sleep_domain_canonical_observations
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE sleep_domain_night_episodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_night_episodes FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_night_episode_scope
  ON sleep_domain_night_episodes;
CREATE POLICY sleep_domain_night_episode_scope ON sleep_domain_night_episodes
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE sleep_domain_night_episode_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_night_episode_revisions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_night_revision_scope
  ON sleep_domain_night_episode_revisions;
CREATE POLICY sleep_domain_night_revision_scope
  ON sleep_domain_night_episode_revisions
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE sleep_domain_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_operations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_operation_scope ON sleep_domain_operations;
CREATE POLICY sleep_domain_operation_scope ON sleep_domain_operations
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE sleep_domain_analysis_role_views ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_analysis_role_views FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_analysis_role_view_scope
  ON sleep_domain_analysis_role_views;
CREATE POLICY sleep_domain_analysis_role_view_scope
  ON sleep_domain_analysis_role_views
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE sleep_domain_domain_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_domain_outbox FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_domain_outbox_scope
  ON sleep_domain_domain_outbox;
CREATE POLICY sleep_domain_domain_outbox_scope ON sleep_domain_domain_outbox
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

-- Product state carrying newly added scope columns.  Null legacy rows are not
-- visible to API/Worker roles and require an explicit migration-owner adapter.
ALTER TABLE product_habit_profile_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_habit_profile_states FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_habit_profile_scope
  ON product_habit_profile_states;
CREATE POLICY product_habit_profile_scope ON product_habit_profile_states
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_care_context_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_care_context_states FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_care_context_scope
  ON product_care_context_states;
CREATE POLICY product_care_context_scope ON product_care_context_states
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_memory_context_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_memory_context_states FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_memory_context_scope
  ON product_memory_context_states;
CREATE POLICY product_memory_context_scope ON product_memory_context_states
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_episode_result_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_episode_result_revisions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_episode_result_revision_scope
  ON product_episode_result_revisions;
CREATE POLICY product_episode_result_revision_scope
  ON product_episode_result_revisions
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_induction_manifests ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_induction_manifests FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_induction_manifest_scope
  ON product_induction_manifests;
CREATE POLICY product_induction_manifest_scope
  ON product_induction_manifests
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_episode_digests ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_episode_digests FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_episode_digest_scope
  ON product_episode_digests;
CREATE POLICY product_episode_digest_scope ON product_episode_digests
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_memory_read_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_memory_read_receipts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_memory_read_receipt_scope
  ON product_memory_read_receipts;
CREATE POLICY product_memory_read_receipt_scope
  ON product_memory_read_receipts
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_pending_profile_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_pending_profile_candidates FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_pending_profile_candidate_scope
  ON product_pending_profile_candidates;
CREATE POLICY product_pending_profile_candidate_scope
  ON product_pending_profile_candidates
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_human_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_human_decisions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_human_decision_scope
  ON product_human_decisions;
CREATE POLICY product_human_decision_scope ON product_human_decisions
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_pending_habit_change_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_pending_habit_change_sets FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_pending_habit_change_set_scope
  ON product_pending_habit_change_sets;
CREATE POLICY product_pending_habit_change_set_scope
  ON product_pending_habit_change_sets
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_longitudinal_subject_epochs ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_longitudinal_subject_epochs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_longitudinal_subject_epoch_scope
  ON product_longitudinal_subject_epochs;
CREATE POLICY product_longitudinal_subject_epoch_scope
  ON product_longitudinal_subject_epochs
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_offline_skill_outcomes ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_offline_skill_outcomes FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_offline_skill_outcome_scope
  ON product_offline_skill_outcomes;
CREATE POLICY product_offline_skill_outcome_scope
  ON product_offline_skill_outcomes
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

-- sleepagent:transactional=true
-- Durable command reservation, operation fencing, invocation reconciliation,
-- destination delivery and consumer deduplication.

ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN scope_protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN namespace_generation BIGINT;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN run_id TEXT;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN arm_id TEXT;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN subject_id TEXT;
ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_scope_v2_contract CHECK (
    scope_protocol_version < 2 OR (
      namespace_generation >= 1
      AND subject_id IS NOT NULL
      AND (
        (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
        OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
      )
    )
  ) NOT VALID;

ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN namespace_generation BIGINT;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN run_id TEXT;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN arm_id TEXT;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN subject_id TEXT;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN authorization_snapshot_json JSONB;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 8;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN lease_generation BIGINT NOT NULL DEFAULT 0;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN fencing_token TEXT;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN worker_instance TEXT;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN heartbeat_at TIMESTAMPTZ;
ALTER TABLE sleep_domain_normalization_work
  ADD CONSTRAINT sleep_domain_normalization_work_v2_contract CHECK (
    protocol_version < 2 OR (
      namespace_generation >= 1
      AND subject_id IS NOT NULL
      AND authorization_snapshot_json IS NOT NULL
      AND jsonb_typeof(authorization_snapshot_json) = 'object'
      AND authorization_snapshot_json ?& ARRAY[
        'authorization_epoch', 'privacy_epoch', 'retrieval_policy_epoch'
      ]
      AND max_attempts >= 1
      AND (
        (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
        OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
      )
      AND (
        status <> 'running'
        OR (lease_generation >= 1 AND fencing_token IS NOT NULL
          AND worker_instance IS NOT NULL AND lease_expires_at IS NOT NULL)
      )
    )
  ) NOT VALID;

ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_scope_v2_subject_fk
  FOREIGN KEY (namespace_id, data_mode, subject_id)
  REFERENCES backend_subjects (
    namespace_id, data_mode, subject_id
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_scope_v2_subject_unique
  UNIQUE (raw_ingress_record_id, namespace_id, data_mode, subject_id);
ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_scope_v2_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_scope_v2_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_normalization_work
  ADD CONSTRAINT sleep_domain_normalization_work_v2_subject_fk
  FOREIGN KEY (namespace_id, data_mode, subject_id)
  REFERENCES backend_subjects (
    namespace_id, data_mode, subject_id
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_normalization_work
  ADD CONSTRAINT sleep_domain_normalization_work_v2_raw_subject_fk
  FOREIGN KEY (
    raw_ingress_record_id, namespace_id, data_mode, subject_id
  ) REFERENCES sleep_domain_raw_inbox (
    raw_ingress_record_id, namespace_id, data_mode, subject_id
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_normalization_work
  ADD CONSTRAINT sleep_domain_normalization_work_v2_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_normalization_work
  ADD CONSTRAINT sleep_domain_normalization_work_v2_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT NOT VALID;

ALTER TABLE sleep_domain_operations
  ADD COLUMN protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_operations
  ADD COLUMN namespace_generation BIGINT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN run_id TEXT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN arm_id TEXT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN id_scheme TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE sleep_domain_operations
  ADD COLUMN origin_kind TEXT NOT NULL DEFAULT 'user';
ALTER TABLE sleep_domain_operations
  ADD COLUMN semantic_key TEXT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN queue_name TEXT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sleep_domain_operations
  ADD COLUMN available_at TIMESTAMPTZ;
ALTER TABLE sleep_domain_operations
  ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 5;
ALTER TABLE sleep_domain_operations
  ADD COLUMN lease_generation BIGINT NOT NULL DEFAULT 0;
ALTER TABLE sleep_domain_operations
  ADD COLUMN fencing_token TEXT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN worker_instance TEXT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN heartbeat_at TIMESTAMPTZ;
ALTER TABLE sleep_domain_operations
  ADD COLUMN authorization_snapshot_json JSONB;
ALTER TABLE sleep_domain_operations
  ADD COLUMN workload_authorization_snapshot_json JSONB;
ALTER TABLE sleep_domain_operations
  ADD COLUMN policy_sha256 TEXT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN outcome_class TEXT;
ALTER TABLE sleep_domain_operations
  ADD COLUMN dead_lettered_at TIMESTAMPTZ;

ALTER TABLE sleep_domain_operations ALTER COLUMN actor_id DROP NOT NULL;

ALTER TABLE sleep_domain_operations
  ADD CONSTRAINT sleep_domain_operation_subject_scope_unique
  UNIQUE (operation_id, namespace_id, data_mode, subject_id);

UPDATE sleep_domain_operations
SET available_at = COALESCE(available_at, created_at),
    queue_name = COALESCE(queue_name, operation_type)
WHERE available_at IS NULL OR queue_name IS NULL;

ALTER TABLE sleep_domain_operations
  ADD CONSTRAINT sleep_domain_operation_protocol_v2_check CHECK (
    protocol_version < 2 OR (
      id_scheme = 'uuidv7'
      AND operation_id ~
        '^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
      AND namespace_generation >= 1
      AND (
        (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
        OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
      )
      AND origin_kind IN ('user', 'system')
      AND semantic_key IS NOT NULL
      AND queue_name IS NOT NULL
      AND available_at IS NOT NULL
      AND max_attempts >= 1
      AND policy_sha256 ~ '^[0-9a-f]{64}$'
      AND (
        (origin_kind = 'user' AND actor_id IS NOT NULL
          AND authorization_snapshot_json IS NOT NULL
          AND workload_authorization_snapshot_json IS NULL
          AND jsonb_typeof(authorization_snapshot_json) = 'object'
          AND authorization_snapshot_json ?& ARRAY[
            'authorization_epoch', 'privacy_epoch',
            'retrieval_policy_epoch'
          ])
        OR
        (origin_kind = 'system' AND actor_id IS NULL
          AND authorization_snapshot_json IS NULL
          AND workload_authorization_snapshot_json IS NOT NULL
          AND jsonb_typeof(workload_authorization_snapshot_json) = 'object'
          AND workload_authorization_snapshot_json ?& ARRAY[
            'authorization_epoch', 'privacy_epoch',
            'retrieval_policy_epoch'
          ])
      )
      AND (
        status <> 'running'
        OR (lease_generation >= 1 AND fencing_token IS NOT NULL
          AND worker_instance IS NOT NULL AND lease_expires_at IS NOT NULL)
      )
    )
  ) NOT VALID;

ALTER TABLE sleep_domain_operations
  ADD CONSTRAINT sleep_domain_operation_v2_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_operations
  ADD CONSTRAINT sleep_domain_operation_v2_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT NOT VALID;

CREATE UNIQUE INDEX IF NOT EXISTS ux_sleep_domain_operation_semantic_v2
  ON sleep_domain_operations (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''),
    operation_type, semantic_key
  )
  WHERE protocol_version >= 2;

CREATE INDEX IF NOT EXISTS idx_sleep_domain_operation_claim_v2
  ON sleep_domain_operations (
    data_mode, queue_name, status, priority DESC, available_at, created_at
  )
  WHERE protocol_version >= 2;

CREATE TABLE IF NOT EXISTS backend_command_receipts (
  command_receipt_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  service_principal_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  route_template TEXT NOT NULL,
  caller_idempotency_key TEXT NOT NULL,
  request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  operation_id TEXT NOT NULL,
  authorization_snapshot_json JSONB NOT NULL,
  receipt_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  expires_at TIMESTAMPTZ,
  UNIQUE (
    service_principal_id, actor_id, route_template, caller_idempotency_key
  ),
  UNIQUE (namespace_id, data_mode, command_receipt_id),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    )
    ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  ),
  CHECK (expires_at IS NULL OR expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_backend_command_receipt_operation
  ON backend_command_receipts (namespace_id, data_mode, operation_id);

CREATE TABLE IF NOT EXISTS backend_invocations (
  invocation_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  invocation_kind TEXT NOT NULL CHECK (
    invocation_kind IN ('model', 'provider', 'external_sink')
  ),
  invocation_key TEXT NOT NULL,
  request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  provider_request_id TEXT,
  current_state TEXT NOT NULL CHECK (
    current_state IN (
      'reserved', 'send_started', 'outcome_possible', 'response_received',
      'known_failed',
      'outcome_unknown', 'reconciled'
    )
  ),
  cas_version BIGINT NOT NULL DEFAULT 1 CHECK (cas_version >= 1),
  lease_generation BIGINT NOT NULL,
  fencing_token TEXT NOT NULL,
  dispatch_permit_at TIMESTAMPTZ,
  response_sha256 TEXT CHECK (
    response_sha256 IS NULL OR response_sha256 ~ '^[0-9a-f]{64}$'
  ),
  reserved_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  UNIQUE (namespace_id, data_mode, operation_id, invocation_key),
  UNIQUE (invocation_id, namespace_id, data_mode, subject_id),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    )
    ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS backend_invocation_journal (
  invocation_event_id TEXT PRIMARY KEY,
  invocation_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  sequence BIGINT NOT NULL CHECK (sequence >= 1),
  from_state TEXT,
  to_state TEXT NOT NULL CHECK (
    to_state IN (
      'reserved', 'send_started', 'outcome_possible', 'response_received',
      'known_failed',
      'outcome_unknown', 'reconciled'
    )
  ),
  lease_generation BIGINT NOT NULL,
  fencing_token TEXT NOT NULL,
  event_json JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  UNIQUE (invocation_id, sequence),
  FOREIGN KEY (invocation_id, namespace_id, data_mode, subject_id)
    REFERENCES backend_invocations (
      invocation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS backend_operation_heartbeats (
  heartbeat_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  lease_generation BIGINT NOT NULL CHECK (lease_generation >= 1),
  fencing_token TEXT NOT NULL,
  worker_instance TEXT NOT NULL,
  lease_expires_at TIMESTAMPTZ NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (operation_id, lease_generation, recorded_at),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    )
    ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS backend_operation_checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  checkpoint_sequence BIGINT NOT NULL CHECK (checkpoint_sequence >= 1),
  lease_generation BIGINT NOT NULL CHECK (lease_generation >= 1),
  fencing_token TEXT NOT NULL,
  checkpoint_kind TEXT NOT NULL,
  checkpoint_sha256 TEXT NOT NULL CHECK (
    checkpoint_sha256 ~ '^[0-9a-f]{64}$'
  ),
  checkpoint_json JSONB NOT NULL,
  visible_to_queries BOOLEAN NOT NULL DEFAULT FALSE CHECK (
    visible_to_queries = FALSE
  ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (operation_id, checkpoint_sequence),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    )
    ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

ALTER TABLE sleep_domain_domain_outbox
  ADD COLUMN protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_domain_outbox
  ADD COLUMN namespace_generation BIGINT;
ALTER TABLE sleep_domain_domain_outbox
  ADD COLUMN run_id TEXT;
ALTER TABLE sleep_domain_domain_outbox
  ADD COLUMN arm_id TEXT;
ALTER TABLE sleep_domain_domain_outbox
  ADD CONSTRAINT sleep_domain_outbox_protocol_v2_scope CHECK (
    protocol_version < 2 OR (
      namespace_generation >= 1
      AND (
        (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
        OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
      )
    )
  ) NOT VALID;
ALTER TABLE sleep_domain_domain_outbox
  ADD CONSTRAINT sleep_domain_outbox_v2_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_domain_outbox
  ADD CONSTRAINT sleep_domain_outbox_v2_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT NOT VALID;

ALTER TABLE sleep_domain_domain_outbox
  ADD CONSTRAINT sleep_domain_outbox_subject_scope_unique
  UNIQUE (event_id, namespace_id, data_mode, subject_id);

CREATE TABLE IF NOT EXISTS backend_delivery_intents (
  delivery_intent_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  source_event_id TEXT NOT NULL,
  destination TEXT NOT NULL,
  handler_name TEXT NOT NULL,
  semantic_effect_key TEXT NOT NULL,
  aggregate_type TEXT NOT NULL,
  aggregate_id TEXT NOT NULL,
  aggregate_sequence BIGINT NOT NULL CHECK (aggregate_sequence >= 1),
  predecessor_sequence BIGINT CHECK (predecessor_sequence >= 1),
  status TEXT NOT NULL CHECK (
    status IN (
      'pending', 'running', 'dispatching', 'delivered', 'retry',
      'dead_letter', 'outcome_unknown', 'cancelled'
    )
  ),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts INTEGER NOT NULL DEFAULT 8 CHECK (max_attempts >= 1),
  priority INTEGER NOT NULL DEFAULT 0,
  available_at TIMESTAMPTZ NOT NULL,
  lease_generation BIGINT NOT NULL DEFAULT 0,
  fencing_token TEXT,
  worker_instance TEXT,
  lease_expires_at TIMESTAMPTZ,
  dispatch_permit_at TIMESTAMPTZ,
  authorization_snapshot_json JSONB NOT NULL CHECK (
    jsonb_typeof(authorization_snapshot_json) = 'object'
    AND authorization_snapshot_json ?& ARRAY[
      'authorization_epoch', 'privacy_epoch', 'retrieval_policy_epoch'
    ]
  ),
  payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  intent_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  delivered_at TIMESTAMPTZ,
  UNIQUE (destination, semantic_effect_key),
  UNIQUE (namespace_id, data_mode, delivery_intent_id),
  UNIQUE (delivery_intent_id, namespace_id, data_mode, subject_id),
  FOREIGN KEY (source_event_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_domain_outbox (
      event_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  CHECK (
    predecessor_sequence IS NULL
    OR predecessor_sequence < aggregate_sequence
  ),
  CHECK (
    status NOT IN ('running', 'dispatching')
    OR (lease_generation >= 1 AND fencing_token IS NOT NULL
      AND worker_instance IS NOT NULL AND lease_expires_at IS NOT NULL)
  ),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_backend_delivery_claim
  ON backend_delivery_intents (
    data_mode, destination, status, priority DESC, available_at, created_at
  );

CREATE TABLE IF NOT EXISTS backend_delivery_journal (
  delivery_event_id TEXT PRIMARY KEY,
  delivery_intent_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  sequence BIGINT NOT NULL CHECK (sequence >= 1),
  from_state TEXT,
  to_state TEXT NOT NULL,
  lease_generation BIGINT,
  fencing_token TEXT,
  event_json JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  UNIQUE (delivery_intent_id, sequence),
  FOREIGN KEY (
    delivery_intent_id, namespace_id, data_mode, subject_id
  ) REFERENCES backend_delivery_intents (
    delivery_intent_id, namespace_id, data_mode, subject_id
  ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS backend_consumer_inbox (
  consumer_id TEXT NOT NULL,
  delivery_intent_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  semantic_effect_key TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  received_at TIMESTAMPTZ NOT NULL,
  applied_at TIMESTAMPTZ,
  result_sha256 TEXT CHECK (
    result_sha256 IS NULL OR result_sha256 ~ '^[0-9a-f]{64}$'
  ),
  PRIMARY KEY (consumer_id, delivery_intent_id),
  UNIQUE (consumer_id, semantic_effect_key),
  FOREIGN KEY (
    delivery_intent_id, namespace_id, data_mode, subject_id
  ) REFERENCES backend_delivery_intents (
    delivery_intent_id, namespace_id, data_mode, subject_id
  ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS backend_consumer_checkpoints (
  consumer_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  aggregate_type TEXT NOT NULL,
  aggregate_id TEXT NOT NULL,
  last_applied_sequence BIGINT NOT NULL CHECK (last_applied_sequence >= 0),
  cas_version BIGINT NOT NULL DEFAULT 1 CHECK (cas_version >= 1),
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (
    consumer_id, namespace_id, data_mode, aggregate_type, aggregate_id
  ),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

-- Reuse the established role-view authority, but make v2 projections carry
-- their exact replay scope, source, policy and revocation epochs.
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN namespace_generation BIGINT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN run_id TEXT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN arm_id TEXT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN source_fact_snapshot_sha256 TEXT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN source_state_version BIGINT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN authorization_epoch BIGINT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN privacy_epoch BIGINT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN retrieval_policy_epoch BIGINT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN policy_sha256 TEXT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN projection_sha256 TEXT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD CONSTRAINT sleep_domain_role_view_v2_contract CHECK (
    protocol_version < 2 OR (
      namespace_generation >= 1
      AND source_fact_snapshot_sha256 ~ '^[0-9a-f]{64}$'
      AND source_state_version >= 1
      AND authorization_epoch >= 1
      AND privacy_epoch >= 1
      AND retrieval_policy_epoch >= 1
      AND policy_sha256 ~ '^[0-9a-f]{64}$'
      AND projection_sha256 ~ '^[0-9a-f]{64}$'
      AND (
        (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
        OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
      )
    )
  ) NOT VALID;

CREATE TABLE IF NOT EXISTS backend_product_attempts (
  product_attempt_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  night_episode_revision_id TEXT NOT NULL,
  attempt_sequence BIGINT NOT NULL CHECK (attempt_sequence >= 1),
  attempt_state TEXT NOT NULL CHECK (
    attempt_state IN (
      'prepared', 'committed', 'abandoned', 'outcome_unknown'
    )
  ),
  query_visible BOOLEAN NOT NULL DEFAULT FALSE,
  fact_snapshot_sha256 TEXT NOT NULL CHECK (
    fact_snapshot_sha256 ~ '^[0-9a-f]{64}$'
  ),
  state_version BIGINT NOT NULL CHECK (state_version >= 1),
  authorization_epoch BIGINT NOT NULL CHECK (authorization_epoch >= 1),
  privacy_epoch BIGINT NOT NULL CHECK (privacy_epoch >= 1),
  retrieval_policy_epoch BIGINT NOT NULL CHECK (
    retrieval_policy_epoch >= 1
  ),
  policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  lease_generation BIGINT NOT NULL CHECK (lease_generation >= 1),
  fencing_token TEXT NOT NULL,
  attempt_sha256 TEXT NOT NULL CHECK (attempt_sha256 ~ '^[0-9a-f]{64}$'),
  attempt_json JSONB NOT NULL,
  prepared_at TIMESTAMPTZ NOT NULL,
  committed_at TIMESTAMPTZ,
  UNIQUE (operation_id, attempt_sequence),
  CHECK (
    (attempt_state = 'committed' AND query_visible = TRUE
      AND committed_at IS NOT NULL)
    OR (attempt_state <> 'committed' AND query_visible = FALSE
      AND committed_at IS NULL)
  ),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  ),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (night_episode_revision_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episode_revisions (
      night_episode_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS backend_pending_handles (
  handle_id TEXT PRIMARY KEY,
  handle_kind TEXT NOT NULL CHECK (
    handle_kind IN ('confirmation', 'answer')
  ),
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('elder', 'family', 'doctor')),
  target_resource_type TEXT NOT NULL,
  target_resource_id TEXT NOT NULL,
  target_state_version BIGINT NOT NULL CHECK (target_state_version >= 1),
  target_sha256 TEXT NOT NULL CHECK (target_sha256 ~ '^[0-9a-f]{64}$'),
  fact_snapshot_sha256 TEXT NOT NULL CHECK (
    fact_snapshot_sha256 ~ '^[0-9a-f]{64}$'
  ),
  care_profile_state_version BIGINT NOT NULL CHECK (
    care_profile_state_version >= 1
  ),
  authorization_epoch BIGINT NOT NULL CHECK (authorization_epoch >= 1),
  privacy_epoch BIGINT NOT NULL CHECK (privacy_epoch >= 1),
  retrieval_policy_epoch BIGINT NOT NULL CHECK (
    retrieval_policy_epoch >= 1
  ),
  policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  status TEXT NOT NULL CHECK (
    status IN ('pending', 'consumed', 'expired', 'revoked')
  ),
  cas_version BIGINT NOT NULL DEFAULT 1 CHECK (cas_version >= 1),
  expires_at TIMESTAMPTZ NOT NULL,
  consumed_at TIMESTAMPTZ,
  consumed_by_command_receipt_id TEXT,
  handle_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (
    namespace_id, data_mode, namespace_generation, target_resource_type,
    target_resource_id, role, handle_kind, target_state_version
  ),
  CHECK (
    (status = 'pending' AND consumed_at IS NULL
      AND consumed_by_command_receipt_id IS NULL)
    OR (status = 'consumed' AND consumed_at IS NOT NULL
      AND consumed_by_command_receipt_id IS NOT NULL)
    OR (status IN ('expired', 'revoked') AND consumed_at IS NULL
      AND consumed_by_command_receipt_id IS NULL)
  ),
  CHECK (expires_at > created_at),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_backend_pending_handle_expiry
  ON backend_pending_handles (
    namespace_id, data_mode, status, expires_at, handle_id
  );

-- ScenarioClock is replay observation time only.  ControlClock remains
-- PostgreSQL server time and is never mutable through this table.
CREATE TABLE IF NOT EXISTS backend_replay_scenario_clocks (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT NOT NULL,
  arm_id TEXT NOT NULL,
  scenario_now TIMESTAMPTZ NOT NULL,
  clock_version BIGINT NOT NULL DEFAULT 1 CHECK (clock_version >= 1),
  last_command_operation_id TEXT NOT NULL,
  command_lease_generation BIGINT NOT NULL CHECK (
    command_lease_generation >= 1
  ),
  command_fencing_token TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (
    namespace_id, data_mode, namespace_generation, run_id, arm_id
  ),
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
    REFERENCES backend_replay_arms (
      namespace_id, namespace_generation, run_id, arm_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (last_command_operation_id, namespace_id, data_mode)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS backend_demo_seed_allowlist (
  seed_id TEXT PRIMARY KEY,
  seed_sha256 TEXT NOT NULL UNIQUE CHECK (seed_sha256 ~ '^[0-9a-f]{64}$'),
  schema_version TEXT NOT NULL,
  generator_version TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CHECK (NOT (metadata_json ?| ARRAY[
    'oracle', 'oracle_expected', 'expected_outcome', 'gold_answer'
  ]))
);

CREATE TABLE IF NOT EXISTS backend_demo_traces (
  demo_trace_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT NOT NULL,
  arm_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  seed_id TEXT NOT NULL
    REFERENCES backend_demo_seed_allowlist(seed_id) ON DELETE RESTRICT,
  command_operation_id TEXT NOT NULL,
  scenario_clock_version BIGINT NOT NULL CHECK (scenario_clock_version >= 1),
  trace_state TEXT NOT NULL CHECK (
    trace_state IN ('requested', 'running', 'committed', 'failed')
  ),
  trace_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (command_operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  CHECK (NOT (trace_json ?| ARRAY[
    'oracle', 'oracle_expected', 'expected_outcome', 'gold_answer'
  ]))
);

CREATE OR REPLACE FUNCTION sleepagent_reject_append_only_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;

DROP TRIGGER IF EXISTS backend_invocation_journal_immutable
  ON backend_invocation_journal;
CREATE TRIGGER backend_invocation_journal_immutable
BEFORE UPDATE OR DELETE ON backend_invocation_journal
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

DROP TRIGGER IF EXISTS backend_delivery_journal_immutable
  ON backend_delivery_journal;
CREATE TRIGGER backend_delivery_journal_immutable
BEFORE UPDATE OR DELETE ON backend_delivery_journal
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

DROP TRIGGER IF EXISTS backend_operation_checkpoint_immutable
  ON backend_operation_checkpoints;
CREATE TRIGGER backend_operation_checkpoint_immutable
BEFORE UPDATE OR DELETE ON backend_operation_checkpoints
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

DROP TRIGGER IF EXISTS backend_operation_heartbeat_immutable
  ON backend_operation_heartbeats;
CREATE TRIGGER backend_operation_heartbeat_immutable
BEFORE UPDATE OR DELETE ON backend_operation_heartbeats
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

CREATE OR REPLACE FUNCTION sleepagent_protect_committed_event_v2()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF OLD.protocol_version >= 2 THEN
    RAISE EXCEPTION 'v2 committed domain events are append-only';
  END IF;
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS sleep_domain_committed_event_v2_immutable
  ON sleep_domain_domain_outbox;
CREATE TRIGGER sleep_domain_committed_event_v2_immutable
BEFORE UPDATE OR DELETE ON sleep_domain_domain_outbox
FOR EACH ROW EXECUTE FUNCTION sleepagent_protect_committed_event_v2();

CREATE OR REPLACE FUNCTION sleepagent_claim_normalization_work(
  claimant_worker_instance TEXT,
  requested_lease_seconds INTEGER
)
RETURNS TABLE (
  work_id TEXT,
  namespace_id TEXT,
  data_mode TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT,
  raw_ingress_record_id TEXT,
  authorization_epoch BIGINT,
  privacy_epoch BIGINT,
  retrieval_policy_epoch BIGINT,
  lease_generation BIGINT,
  fencing_token TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  principal TEXT := NULLIF(
    current_setting('sleepagent.service_principal_id', TRUE), ''
  );
  deployment_mode TEXT := NULLIF(
    current_setting('sleepagent.data_mode', TRUE), ''
  );
  request_purpose TEXT := NULLIF(
    current_setting('sleepagent.purpose', TRUE), ''
  );
BEGIN
  IF claimant_worker_instance IS NULL OR claimant_worker_instance = ''
     OR requested_lease_seconds < 1 OR requested_lease_seconds > 3600
     OR principal IS NULL OR deployment_mode NOT IN ('live', 'replay')
     OR request_purpose IS NULL
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <>
       'worker'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid or untrusted normalization claim context';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT work.work_id,
      epoch_row.authorization_epoch AS claim_authorization_epoch,
      epoch_row.privacy_epoch AS claim_privacy_epoch,
      epoch_row.retrieval_policy_epoch AS claim_retrieval_policy_epoch
    FROM public.sleep_domain_normalization_work AS work
    JOIN public.backend_namespaces AS namespace_row
      ON namespace_row.namespace_id = work.namespace_id
     AND namespace_row.data_mode = work.data_mode
    JOIN public.backend_subject_epochs AS epoch_row
      ON epoch_row.namespace_id = work.namespace_id
     AND epoch_row.data_mode = work.data_mode
     AND epoch_row.subject_id = work.subject_id
    WHERE work.protocol_version >= 2
      AND work.data_mode = deployment_mode
      AND (
        work.status IN ('pending', 'retry')
        OR (work.status = 'running'
          AND work.lease_expires_at <= clock_timestamp())
      )
      AND work.available_at <= clock_timestamp()
      AND work.attempt_count < work.max_attempts
      AND namespace_row.status = 'active'
      AND work.authorization_snapshot_json ->> 'authorization_epoch' =
        epoch_row.authorization_epoch::text
      AND work.authorization_snapshot_json ->> 'privacy_epoch' =
        epoch_row.privacy_epoch::text
      AND work.authorization_snapshot_json ->> 'retrieval_policy_epoch' =
        epoch_row.retrieval_policy_epoch::text
      AND EXISTS (
        SELECT 1
        FROM public.backend_principal_grants AS grant_row
        WHERE grant_row.principal_id = principal
          AND grant_row.namespace_id = work.namespace_id
          AND grant_row.data_mode = work.data_mode
          AND grant_row.purpose = request_purpose
          AND grant_row.authorization_epoch =
            epoch_row.authorization_epoch
          AND grant_row.status = 'active'
          AND grant_row.valid_from <= clock_timestamp()
          AND (
            grant_row.valid_until IS NULL
            OR grant_row.valid_until > clock_timestamp()
          )
          AND grant_row.allowed_handlers_json ? 'normalization'
      )
    ORDER BY work.available_at, work.created_at, work.work_id
    FOR UPDATE OF namespace_row, work SKIP LOCKED
    LIMIT 1
  ), claimed AS (
    UPDATE public.sleep_domain_normalization_work AS work
    SET status = 'running',
        attempt_count = work.attempt_count + 1,
        lease_generation = work.lease_generation + 1,
        fencing_token = gen_random_uuid()::text,
        worker_instance = claimant_worker_instance,
        lease_owner = claimant_worker_instance,
        heartbeat_at = clock_timestamp(),
        lease_expires_at = clock_timestamp()
          + make_interval(secs => requested_lease_seconds),
        updated_at = clock_timestamp()
    FROM candidate
    WHERE work.work_id = candidate.work_id
    RETURNING work.work_id, work.namespace_id, work.data_mode,
      work.namespace_generation, work.run_id, work.arm_id,
      work.subject_id, work.raw_ingress_record_id,
      candidate.claim_authorization_epoch,
      candidate.claim_privacy_epoch,
      candidate.claim_retrieval_policy_epoch,
      work.lease_generation, work.fencing_token
  )
  SELECT claimed.work_id, claimed.namespace_id, claimed.data_mode,
    claimed.namespace_generation, claimed.run_id, claimed.arm_id,
    claimed.subject_id, claimed.raw_ingress_record_id,
    claimed.claim_authorization_epoch, claimed.claim_privacy_epoch,
    claimed.claim_retrieval_policy_epoch, claimed.lease_generation,
    claimed.fencing_token
  FROM claimed;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_claim_normalization_work(TEXT, INTEGER)
  FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_heartbeat_normalization_work(
  target_work_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_lease_seconds INTEGER
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_lease_seconds < 1 OR requested_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'invalid normalization heartbeat duration';
  END IF;
  UPDATE public.sleep_domain_normalization_work
  SET heartbeat_at = clock_timestamp(),
      lease_expires_at = clock_timestamp()
        + make_interval(secs => requested_lease_seconds),
      updated_at = clock_timestamp()
  WHERE work_id = target_work_id
    AND status = 'running'
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation,
      run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_heartbeat_normalization_work(
  TEXT, BIGINT, TEXT, INTEGER
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_finalize_normalization_work(
  target_work_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_final_status TEXT,
  requested_next_available_at TIMESTAMPTZ,
  requested_error_code TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_final_status NOT IN (
    'succeeded', 'retry', 'dead_letter', 'quarantined'
  ) OR (requested_final_status = 'retry'
    AND requested_next_available_at IS NULL) THEN
    RAISE EXCEPTION 'invalid normalization final status';
  END IF;
  UPDATE public.sleep_domain_normalization_work
  SET status = requested_final_status,
      available_at = COALESCE(requested_next_available_at, available_at),
      last_error_code = requested_error_code,
      lease_expires_at = NULL,
      lease_owner = NULL,
      worker_instance = NULL,
      updated_at = clock_timestamp()
  WHERE work_id = target_work_id
    AND status = 'running'
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation,
      run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_finalize_normalization_work(
  TEXT, BIGINT, TEXT, TEXT, TIMESTAMPTZ, TEXT
) FROM PUBLIC;

-- Cross-namespace claim is the only SECURITY DEFINER work scan.  It derives
-- principal and deployment data mode from trusted connection context, checks
-- the principal grant/handler allowlist, uses PostgreSQL server time, and
-- returns only the ID plus the exact fence needed by a subsequent scoped UoW.
CREATE OR REPLACE FUNCTION sleepagent_claim_operation(
  requested_queue TEXT,
  claimant_worker_instance TEXT,
  requested_lease_seconds INTEGER
)
RETURNS TABLE (
  operation_id TEXT,
  namespace_id TEXT,
  data_mode TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT,
  operation_type TEXT,
  authorization_epoch BIGINT,
  privacy_epoch BIGINT,
  retrieval_policy_epoch BIGINT,
  lease_generation BIGINT,
  fencing_token TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  principal TEXT := NULLIF(
    current_setting('sleepagent.service_principal_id', TRUE), ''
  );
  deployment_mode TEXT := NULLIF(
    current_setting('sleepagent.data_mode', TRUE), ''
  );
  request_purpose TEXT := NULLIF(
    current_setting('sleepagent.purpose', TRUE), ''
  );
  caller_process_role TEXT := NULLIF(
    current_setting('sleepagent.process_role', TRUE), ''
  );
BEGIN
  IF principal IS NULL OR deployment_mode NOT IN ('live', 'replay')
     OR request_purpose IS NULL OR caller_process_role <> 'worker'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION
      'trusted worker principal/data mode/purpose context is required';
  END IF;
  IF requested_queue IS NULL OR requested_queue = ''
     OR claimant_worker_instance IS NULL OR claimant_worker_instance = ''
     OR requested_lease_seconds < 1 OR requested_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'invalid operation claim parameters';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT op.operation_id,
      epoch_row.authorization_epoch AS claim_authorization_epoch,
      epoch_row.privacy_epoch AS claim_privacy_epoch,
      epoch_row.retrieval_policy_epoch AS claim_retrieval_policy_epoch
    FROM public.sleep_domain_operations AS op
    JOIN public.backend_namespaces AS ns
      ON ns.namespace_id = op.namespace_id
     AND ns.data_mode = op.data_mode
    JOIN public.backend_subject_epochs AS epoch_row
      ON epoch_row.namespace_id = op.namespace_id
     AND epoch_row.data_mode = op.data_mode
     AND epoch_row.subject_id = op.subject_id
    WHERE op.protocol_version >= 2
      AND op.data_mode = deployment_mode
      AND op.queue_name = requested_queue
      AND (
        op.status IN ('pending', 'retry')
        OR (op.status = 'running'
          AND op.lease_expires_at <= clock_timestamp())
      )
      AND op.available_at <= clock_timestamp()
      AND op.attempt_count < op.max_attempts
      AND ns.status = 'active'
      AND COALESCE(
        op.authorization_snapshot_json,
        op.workload_authorization_snapshot_json
      ) ->> 'authorization_epoch' = epoch_row.authorization_epoch::text
      AND COALESCE(
        op.authorization_snapshot_json,
        op.workload_authorization_snapshot_json
      ) ->> 'privacy_epoch' = epoch_row.privacy_epoch::text
      AND COALESCE(
        op.authorization_snapshot_json,
        op.workload_authorization_snapshot_json
      ) ->> 'retrieval_policy_epoch' =
        epoch_row.retrieval_policy_epoch::text
      AND (
        SELECT COUNT(*)
        FROM public.sleep_domain_operations AS active
        WHERE active.namespace_id = op.namespace_id
          AND active.data_mode = op.data_mode
          AND active.protocol_version >= 2
          AND active.status = 'running'
          AND active.lease_expires_at > clock_timestamp()
      ) < ns.max_worker_concurrency
      AND EXISTS (
        SELECT 1
        FROM public.backend_principal_grants AS grant_row
        WHERE grant_row.principal_id = principal
          AND grant_row.namespace_id = op.namespace_id
          AND grant_row.data_mode = op.data_mode
          AND grant_row.purpose = request_purpose
          AND grant_row.authorization_epoch =
            epoch_row.authorization_epoch
          AND grant_row.status = 'active'
          AND grant_row.valid_from <= clock_timestamp()
          AND (
            grant_row.valid_until IS NULL
            OR grant_row.valid_until > clock_timestamp()
          )
          AND grant_row.allowed_handlers_json ? op.operation_type
          AND EXISTS (
            SELECT 1
            FROM public.backend_service_principals AS service_principal
            WHERE service_principal.principal_id = principal
              AND service_principal.status = 'active'
          )
      )
    ORDER BY op.priority DESC, op.available_at, op.created_at, op.operation_id
    FOR UPDATE OF ns, op SKIP LOCKED
    LIMIT 1
  ), claimed AS (
    UPDATE public.sleep_domain_operations AS op
    SET status = 'running',
        attempt_count = op.attempt_count + 1,
        lease_generation = op.lease_generation + 1,
        fencing_token = gen_random_uuid()::text,
        worker_instance = claimant_worker_instance,
        lease_owner = claimant_worker_instance,
        heartbeat_at = clock_timestamp(),
        lease_expires_at = clock_timestamp()
          + make_interval(secs => requested_lease_seconds),
        updated_at = clock_timestamp()
    FROM candidate
    WHERE op.operation_id = candidate.operation_id
    RETURNING op.operation_id, op.namespace_id, op.data_mode,
      op.namespace_generation, op.run_id, op.arm_id, op.subject_id,
      op.operation_type, candidate.claim_authorization_epoch,
      candidate.claim_privacy_epoch, candidate.claim_retrieval_policy_epoch,
      op.lease_generation, op.fencing_token
  )
  SELECT claimed.operation_id, claimed.namespace_id, claimed.data_mode,
    claimed.namespace_generation, claimed.run_id, claimed.arm_id,
    claimed.subject_id, claimed.operation_type,
    claimed.claim_authorization_epoch, claimed.claim_privacy_epoch,
    claimed.claim_retrieval_policy_epoch,
    claimed.lease_generation, claimed.fencing_token
  FROM claimed;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_claim_operation(TEXT, TEXT, INTEGER)
  FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_heartbeat_operation(
  target_operation_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_lease_seconds INTEGER
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_lease_seconds < 1 OR requested_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'invalid heartbeat lease duration';
  END IF;
  UPDATE public.sleep_domain_operations
  SET heartbeat_at = clock_timestamp(),
      lease_expires_at = clock_timestamp()
        + make_interval(secs => requested_lease_seconds),
      updated_at = clock_timestamp()
  WHERE operation_id = target_operation_id
    AND status = 'running'
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_heartbeat_operation(
  TEXT, BIGINT, TEXT, INTEGER
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_finalize_operation(
  target_operation_id TEXT,
  expected_cas_version BIGINT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_final_status TEXT,
  requested_outcome_class TEXT,
  requested_next_available_at TIMESTAMPTZ
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_final_status NOT IN (
    'succeeded', 'retry', 'failed', 'dead_letter',
    'reconciliation_required', 'outcome_unknown'
  ) OR (requested_final_status = 'retry'
    AND requested_next_available_at IS NULL) THEN
    RAISE EXCEPTION 'invalid operation final status';
  END IF;
  UPDATE public.sleep_domain_operations
  SET status = requested_final_status,
      outcome_class = requested_outcome_class,
      available_at = COALESCE(requested_next_available_at, available_at),
      cas_version = cas_version + 1,
      lease_expires_at = NULL,
      lease_owner = NULL,
      worker_instance = NULL,
      updated_at = clock_timestamp(),
      dead_lettered_at = CASE WHEN requested_final_status = 'dead_letter'
        THEN clock_timestamp() ELSE dead_lettered_at END
  WHERE operation_id = target_operation_id
    AND status = 'running'
    AND cas_version = expected_cas_version
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp()
    AND COALESCE(
      authorization_snapshot_json,
      workload_authorization_snapshot_json
    ) ->> 'authorization_epoch' = NULLIF(
      current_setting('sleepagent.authorization_epoch', TRUE), ''
    )
    AND COALESCE(
      authorization_snapshot_json,
      workload_authorization_snapshot_json
    ) ->> 'privacy_epoch' = NULLIF(
      current_setting('sleepagent.privacy_epoch', TRUE), ''
    )
    AND COALESCE(
      authorization_snapshot_json,
      workload_authorization_snapshot_json
    ) ->> 'retrieval_policy_epoch' = NULLIF(
      current_setting('sleepagent.retrieval_policy_epoch', TRUE), ''
    );
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_finalize_operation(
  TEXT, BIGINT, BIGINT, TEXT, TEXT, TEXT, TIMESTAMPTZ
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_operation_fence_allows(
  target_operation_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT EXISTS (
    SELECT 1
    FROM public.sleep_domain_operations AS op
    WHERE op.operation_id = target_operation_id
      AND op.status = 'running'
      AND op.lease_generation = expected_lease_generation
      AND op.fencing_token = expected_fencing_token
      AND op.worker_instance = NULLIF(
        current_setting('sleepagent.worker_instance', TRUE), ''
      )
      AND public.sleepagent_principal_context_allows()
      AND public.sleepagent_subject_generation_scope_allows(
        op.namespace_id, op.data_mode, op.subject_id,
        op.namespace_generation, op.run_id, op.arm_id
      )
      AND op.lease_expires_at > clock_timestamp()
      AND COALESCE(
        op.authorization_snapshot_json,
        op.workload_authorization_snapshot_json
      ) ->> 'authorization_epoch' = NULLIF(
        current_setting('sleepagent.authorization_epoch', TRUE), ''
      )
      AND COALESCE(
        op.authorization_snapshot_json,
        op.workload_authorization_snapshot_json
      ) ->> 'privacy_epoch' = NULLIF(
        current_setting('sleepagent.privacy_epoch', TRUE), ''
      )
      AND COALESCE(
        op.authorization_snapshot_json,
        op.workload_authorization_snapshot_json
      ) ->> 'retrieval_policy_epoch' = NULLIF(
        current_setting('sleepagent.retrieval_policy_epoch', TRUE), ''
      )
  )
$$;

REVOKE ALL ON FUNCTION sleepagent_operation_fence_allows(
  TEXT, BIGINT, TEXT
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_enforce_scenario_clock_fence()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF NOT public.sleepagent_operation_fence_allows(
    NEW.last_command_operation_id,
    NEW.command_lease_generation,
    NEW.command_fencing_token
  ) THEN
    RAISE EXCEPTION 'ScenarioClock mutation requires a live operation fence';
  END IF;
  IF TG_OP = 'UPDATE'
     AND NEW.clock_version <> OLD.clock_version + 1 THEN
    RAISE EXCEPTION 'ScenarioClock version must advance by one';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS backend_replay_scenario_clock_fence
  ON backend_replay_scenario_clocks;
CREATE TRIGGER backend_replay_scenario_clock_fence
BEFORE INSERT OR UPDATE ON backend_replay_scenario_clocks
FOR EACH ROW EXECUTE FUNCTION sleepagent_enforce_scenario_clock_fence();

CREATE OR REPLACE FUNCTION sleepagent_claim_delivery(
  requested_destination TEXT,
  claimant_worker_instance TEXT,
  requested_lease_seconds INTEGER
)
RETURNS TABLE (
  delivery_intent_id TEXT,
  namespace_id TEXT,
  data_mode TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT,
  handler_name TEXT,
  authorization_epoch BIGINT,
  privacy_epoch BIGINT,
  retrieval_policy_epoch BIGINT,
  lease_generation BIGINT,
  fencing_token TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  principal TEXT := NULLIF(
    current_setting('sleepagent.service_principal_id', TRUE), ''
  );
  deployment_mode TEXT := NULLIF(
    current_setting('sleepagent.data_mode', TRUE), ''
  );
  request_purpose TEXT := NULLIF(
    current_setting('sleepagent.purpose', TRUE), ''
  );
BEGIN
  IF requested_destination IS NULL OR requested_destination = ''
     OR claimant_worker_instance IS NULL OR claimant_worker_instance = ''
     OR requested_lease_seconds < 1 OR requested_lease_seconds > 3600
     OR principal IS NULL OR deployment_mode NOT IN ('live', 'replay')
     OR request_purpose IS NULL
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <>
       'worker'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid or untrusted delivery claim context';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT intent.delivery_intent_id,
      epoch_row.authorization_epoch AS claim_authorization_epoch,
      epoch_row.privacy_epoch AS claim_privacy_epoch,
      epoch_row.retrieval_policy_epoch AS claim_retrieval_policy_epoch
    FROM public.backend_delivery_intents AS intent
    JOIN public.backend_namespaces AS namespace_row
      ON namespace_row.namespace_id = intent.namespace_id
     AND namespace_row.data_mode = intent.data_mode
    JOIN public.backend_subject_epochs AS epoch_row
      ON epoch_row.namespace_id = intent.namespace_id
     AND epoch_row.data_mode = intent.data_mode
     AND epoch_row.subject_id = intent.subject_id
    WHERE intent.data_mode = deployment_mode
      AND intent.destination = requested_destination
      AND (
        intent.status IN ('pending', 'retry')
        OR (intent.status IN ('running', 'dispatching')
          AND intent.lease_expires_at <= clock_timestamp())
      )
      AND intent.available_at <= clock_timestamp()
      AND intent.attempt_count < intent.max_attempts
      AND namespace_row.status = 'active'
      AND intent.authorization_snapshot_json ->> 'authorization_epoch' =
        epoch_row.authorization_epoch::text
      AND intent.authorization_snapshot_json ->> 'privacy_epoch' =
        epoch_row.privacy_epoch::text
      AND intent.authorization_snapshot_json ->>
        'retrieval_policy_epoch' = epoch_row.retrieval_policy_epoch::text
      AND (
        intent.predecessor_sequence IS NULL
        OR EXISTS (
          SELECT 1
          FROM public.backend_delivery_intents AS predecessor
          WHERE predecessor.destination = intent.destination
            AND predecessor.aggregate_type = intent.aggregate_type
            AND predecessor.aggregate_id = intent.aggregate_id
            AND predecessor.aggregate_sequence =
              intent.predecessor_sequence
            AND predecessor.status = 'delivered'
        )
      )
      AND EXISTS (
        SELECT 1
        FROM public.backend_principal_grants AS grant_row
        WHERE grant_row.principal_id = principal
          AND grant_row.namespace_id = intent.namespace_id
          AND grant_row.data_mode = intent.data_mode
          AND grant_row.purpose = request_purpose
          AND grant_row.authorization_epoch =
            epoch_row.authorization_epoch
          AND grant_row.status = 'active'
          AND grant_row.valid_from <= clock_timestamp()
          AND (
            grant_row.valid_until IS NULL
            OR grant_row.valid_until > clock_timestamp()
          )
          AND grant_row.allowed_handlers_json ? intent.handler_name
      )
    ORDER BY intent.priority DESC, intent.available_at,
      intent.created_at, intent.delivery_intent_id
    FOR UPDATE OF namespace_row, intent SKIP LOCKED
    LIMIT 1
  ), claimed AS (
    UPDATE public.backend_delivery_intents AS intent
    SET status = 'running',
        attempt_count = intent.attempt_count + 1,
        lease_generation = intent.lease_generation + 1,
        fencing_token = gen_random_uuid()::text,
        worker_instance = claimant_worker_instance,
        lease_expires_at = clock_timestamp()
          + make_interval(secs => requested_lease_seconds),
        dispatch_permit_at = NULL,
        updated_at = clock_timestamp()
    FROM candidate
    WHERE intent.delivery_intent_id = candidate.delivery_intent_id
    RETURNING intent.delivery_intent_id, intent.namespace_id,
      intent.data_mode, intent.namespace_generation, intent.run_id,
      intent.arm_id, intent.subject_id, intent.handler_name,
      candidate.claim_authorization_epoch,
      candidate.claim_privacy_epoch,
      candidate.claim_retrieval_policy_epoch,
      intent.lease_generation, intent.fencing_token
  )
  SELECT claimed.delivery_intent_id, claimed.namespace_id,
    claimed.data_mode, claimed.namespace_generation, claimed.run_id,
    claimed.arm_id, claimed.subject_id, claimed.handler_name,
    claimed.claim_authorization_epoch, claimed.claim_privacy_epoch,
    claimed.claim_retrieval_policy_epoch, claimed.lease_generation,
    claimed.fencing_token
  FROM claimed;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_claim_delivery(TEXT, TEXT, INTEGER)
  FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_heartbeat_delivery(
  target_delivery_intent_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_lease_seconds INTEGER
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_lease_seconds < 1 OR requested_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'invalid delivery heartbeat duration';
  END IF;
  UPDATE public.backend_delivery_intents
  SET lease_expires_at = clock_timestamp()
        + make_interval(secs => requested_lease_seconds),
      updated_at = clock_timestamp()
  WHERE delivery_intent_id = target_delivery_intent_id
    AND status IN ('running', 'dispatching')
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_heartbeat_delivery(
  TEXT, BIGINT, TEXT, INTEGER
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_mark_delivery_dispatching(
  target_delivery_intent_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  UPDATE public.backend_delivery_intents
  SET status = 'dispatching',
      dispatch_permit_at = clock_timestamp(),
      updated_at = clock_timestamp()
  WHERE delivery_intent_id = target_delivery_intent_id
    AND status = 'running'
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_mark_delivery_dispatching(
  TEXT, BIGINT, TEXT
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_finalize_delivery(
  target_delivery_intent_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_final_status TEXT,
  next_available_at TIMESTAMPTZ
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_final_status NOT IN (
    'delivered', 'retry', 'dead_letter', 'outcome_unknown'
  ) OR (requested_final_status = 'retry' AND next_available_at IS NULL) THEN
    RAISE EXCEPTION 'invalid delivery final status';
  END IF;
  UPDATE public.backend_delivery_intents
  SET status = requested_final_status,
      available_at = COALESCE(next_available_at, available_at),
      delivered_at = CASE WHEN requested_final_status = 'delivered'
        THEN clock_timestamp() ELSE delivered_at END,
      lease_expires_at = NULL,
      worker_instance = NULL,
      updated_at = clock_timestamp()
  WHERE delivery_intent_id = target_delivery_intent_id
    AND status IN ('running', 'dispatching')
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_finalize_delivery(
  TEXT, BIGINT, TEXT, TEXT, TIMESTAMPTZ
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_consume_pending_handle(
  target_handle_id TEXT,
  expected_cas_version BIGINT,
  expected_target_state_version BIGINT,
  expected_target_sha256 TEXT,
  expected_policy_sha256 TEXT,
  command_receipt_id TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  UPDATE public.backend_pending_handles
  SET status = 'consumed',
      cas_version = cas_version + 1,
      consumed_at = clock_timestamp(),
      consumed_by_command_receipt_id = command_receipt_id
  WHERE handle_id = target_handle_id
    AND status = 'pending'
    AND cas_version = expected_cas_version
    AND target_state_version = expected_target_state_version
    AND target_sha256 = expected_target_sha256
    AND policy_sha256 = expected_policy_sha256
    AND actor_id = NULLIF(
      current_setting('sleepagent.actor_id', TRUE), ''
    )
    AND role = NULLIF(current_setting('sleepagent.actor_role', TRUE), '')
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND authorization_epoch::text = NULLIF(
      current_setting('sleepagent.authorization_epoch', TRUE), ''
    )
    AND privacy_epoch::text = NULLIF(
      current_setting('sleepagent.privacy_epoch', TRUE), ''
    )
    AND retrieval_policy_epoch::text = NULLIF(
      current_setting('sleepagent.retrieval_policy_epoch', TRUE), ''
    )
    AND expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_consume_pending_handle(
  TEXT, BIGINT, BIGINT, TEXT, TEXT, TEXT
) FROM PUBLIC;

-- New work-protocol state is subject scoped after claim.
DROP POLICY IF EXISTS sleep_domain_raw_inbox_scope ON sleep_domain_raw_inbox;
CREATE POLICY sleep_domain_raw_inbox_scope ON sleep_domain_raw_inbox
  USING (
    (scope_protocol_version < 2
      AND sleepagent_namespace_scope_allows(namespace_id, data_mode))
    OR (scope_protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  )
  WITH CHECK (
    (scope_protocol_version < 2
      AND sleepagent_namespace_scope_allows(namespace_id, data_mode))
    OR (scope_protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  );

ALTER TABLE sleep_domain_normalization_work ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_normalization_work FORCE ROW LEVEL SECURITY;
CREATE POLICY sleep_domain_normalization_work_scope
  ON sleep_domain_normalization_work
  USING (
    (protocol_version < 2
      AND sleepagent_namespace_scope_allows(namespace_id, data_mode))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  )
  WITH CHECK (
    (protocol_version < 2
      AND sleepagent_namespace_scope_allows(namespace_id, data_mode))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  );

DROP POLICY IF EXISTS sleep_domain_operation_scope ON sleep_domain_operations;
CREATE POLICY sleep_domain_operation_scope ON sleep_domain_operations
  USING (
    (protocol_version < 2
      AND sleepagent_subject_scope_allows(
        namespace_id, data_mode, subject_id
      ))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  )
  WITH CHECK (
    (protocol_version < 2
      AND sleepagent_subject_scope_allows(
        namespace_id, data_mode, subject_id
      ))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  );

DROP POLICY IF EXISTS sleep_domain_domain_outbox_scope
  ON sleep_domain_domain_outbox;
CREATE POLICY sleep_domain_domain_outbox_scope ON sleep_domain_domain_outbox
  USING (
    (protocol_version < 2
      AND sleepagent_subject_scope_allows(
        namespace_id, data_mode, subject_id
      ))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  )
  WITH CHECK (
    (protocol_version < 2
      AND sleepagent_subject_scope_allows(
        namespace_id, data_mode, subject_id
      ))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  );

ALTER TABLE backend_command_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_command_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_command_receipt_scope ON backend_command_receipts
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_invocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_invocations FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_invocation_scope ON backend_invocations
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_invocation_journal ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_invocation_journal FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_invocation_journal_scope ON backend_invocation_journal
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_operation_heartbeats ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_operation_heartbeats FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_operation_heartbeat_scope
  ON backend_operation_heartbeats
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_operation_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_operation_checkpoints FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_operation_checkpoint_scope
  ON backend_operation_checkpoints
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_delivery_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_delivery_intents FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_delivery_intent_scope ON backend_delivery_intents
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_delivery_journal ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_delivery_journal FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_delivery_journal_scope ON backend_delivery_journal
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_consumer_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_consumer_inbox FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_consumer_inbox_scope ON backend_consumer_inbox
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_consumer_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_consumer_checkpoints FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_consumer_checkpoint_scope ON backend_consumer_checkpoints
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

DROP POLICY IF EXISTS sleep_domain_analysis_role_view_scope
  ON sleep_domain_analysis_role_views;
CREATE POLICY sleep_domain_analysis_role_view_scope
  ON sleep_domain_analysis_role_views
  USING (
    (
      (protocol_version < 2 AND sleepagent_subject_scope_allows(
        namespace_id, data_mode, subject_id
      ))
      OR (protocol_version >= 2
        AND sleepagent_subject_generation_scope_allows(
          namespace_id, data_mode, subject_id, namespace_generation,
          run_id, arm_id
        ))
    )
    AND (
      sleepagent_scope_setting('sleepagent.process_role') = 'worker'
      OR role = sleepagent_scope_setting('sleepagent.actor_role')
    )
  )
  WITH CHECK (
    (
      (protocol_version < 2 AND sleepagent_subject_scope_allows(
        namespace_id, data_mode, subject_id
      ))
      OR (protocol_version >= 2
        AND sleepagent_subject_generation_scope_allows(
          namespace_id, data_mode, subject_id, namespace_generation,
          run_id, arm_id
        ))
    )
    AND sleepagent_scope_setting('sleepagent.process_role') = 'worker'
  );

ALTER TABLE backend_product_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_product_attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_product_attempt_scope ON backend_product_attempts
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_pending_handles ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_pending_handles FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_pending_handle_scope ON backend_pending_handles
  USING (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND (
      sleepagent_scope_setting('sleepagent.process_role') = 'worker'
      OR (
        actor_id = sleepagent_scope_setting('sleepagent.actor_id')
        AND role = sleepagent_scope_setting('sleepagent.actor_role')
      )
    )
  )
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND (
      sleepagent_scope_setting('sleepagent.process_role') = 'worker'
      OR (
        actor_id = sleepagent_scope_setting('sleepagent.actor_id')
        AND role = sleepagent_scope_setting('sleepagent.actor_role')
      )
    )
  );

ALTER TABLE backend_replay_scenario_clocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_replay_scenario_clocks FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_replay_scenario_clock_scope
  ON backend_replay_scenario_clocks
  USING (sleepagent_namespace_generation_scope_allows(
    namespace_id, data_mode, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_namespace_generation_scope_allows(
    namespace_id, data_mode, namespace_generation, run_id, arm_id
  ));

CREATE OR REPLACE FUNCTION sleepagent_demo_seed_access_allows()
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT EXISTS (
    SELECT 1
    FROM public.backend_service_principals AS principal
    WHERE principal.principal_id = sleepagent_scope_setting(
        'sleepagent.service_principal_id'
      )
      AND principal.database_role_name::text = session_user::text
      AND principal.status = 'active'
      AND principal.principal_kind IN ('demo_controller', 'worker')
  )
$$;

ALTER TABLE backend_demo_seed_allowlist ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_demo_seed_allowlist FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_demo_seed_allowlist_access
  ON backend_demo_seed_allowlist
  USING (sleepagent_demo_seed_access_allows())
  WITH CHECK (sleepagent_demo_seed_access_allows());

ALTER TABLE backend_demo_traces ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_demo_traces FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_demo_trace_scope ON backend_demo_traces
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

-- sleepagent:transactional=true
-- Stable UUIDv7 NightEpisode identity and explicit wake-date assignment.

ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN id_scheme TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN namespace_generation BIGINT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN run_id TEXT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN arm_id TEXT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN episode_anchor_key TEXT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN opening_source_idempotency_identity TEXT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN timezone_name TEXT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN boundary_policy_version TEXT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN collection_start_at TIMESTAMPTZ;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN deterministic_close_deadline_at TIMESTAMPTZ;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN bed_at TIMESTAMPTZ;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN wake_at TIMESTAMPTZ;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN bed_local_date DATE;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN wake_local_date DATE;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN vendor_wake_local_date DATE;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN episode_local_date DATE;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN assignment_basis TEXT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN date_confidence TEXT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN assignment_estimated BOOLEAN;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN date_state TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN date_finalized_at TIMESTAMPTZ;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN bed_utc_offset_seconds INTEGER;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN wake_utc_offset_seconds INTEGER;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN bed_fold SMALLINT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN wake_fold SMALLINT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN date_conflict BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN reconciliation_status TEXT;

ALTER TABLE sleep_domain_night_episodes
  ADD CONSTRAINT sleep_domain_night_episode_v2_contract CHECK (
    protocol_version < 2 OR (
      id_scheme = 'uuidv7'
      AND night_episode_id ~
        '^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
      AND namespace_generation >= 1
      AND (
        (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
        OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
      )
      AND episode_anchor_key IS NOT NULL
      AND episode_anchor_key <> ''
      AND opening_source_idempotency_identity IS NOT NULL
      AND opening_source_idempotency_identity <> ''
      AND timezone_name IS NOT NULL
      AND timezone_name <> ''
      AND boundary_policy_version IS NOT NULL
      AND boundary_policy_version <> ''
      AND collection_start_at IS NOT NULL
      AND deterministic_close_deadline_at > collection_start_at
      AND date_state IN ('provisional', 'finalized', 'conflict')
      AND (bed_fold IS NULL OR bed_fold IN (0, 1))
      AND (wake_fold IS NULL OR wake_fold IN (0, 1))
      AND (
        bed_utc_offset_seconds IS NULL
        OR bed_utc_offset_seconds BETWEEN -64800 AND 64800
      )
      AND (
        wake_utc_offset_seconds IS NULL
        OR wake_utc_offset_seconds BETWEEN -64800 AND 64800
      )
      AND (
        (date_state = 'provisional'
          AND episode_local_date IS NULL
          AND assignment_basis = 'provisional'
          AND date_confidence = 'unknown'
          AND assignment_estimated IS NULL
          AND date_finalized_at IS NULL
          AND date_conflict = FALSE
          AND reconciliation_status IS NULL)
        OR
        (date_state = 'finalized'
          AND episode_local_date IS NOT NULL
          AND assignment_basis IN (
            'observed_wake', 'vendor_wake_date', 'deadline_fallback'
          )
          AND date_confidence IN (
            'observed', 'vendor_asserted', 'estimated'
          )
          AND assignment_estimated IS NOT NULL
          AND date_finalized_at IS NOT NULL
          AND date_conflict = FALSE
          AND reconciliation_status IS NULL
          AND (
            (assignment_basis = 'observed_wake'
              AND wake_local_date = episode_local_date
              AND wake_at IS NOT NULL
              AND date_confidence = 'observed'
              AND assignment_estimated = FALSE)
            OR
            (assignment_basis = 'vendor_wake_date'
              AND vendor_wake_local_date = episode_local_date
              AND date_confidence = 'vendor_asserted'
              AND assignment_estimated = FALSE)
            OR
            (assignment_basis = 'deadline_fallback'
              AND assignment_estimated = TRUE
              AND date_confidence = 'estimated')
          ))
        OR
        (date_state = 'conflict'
          AND episode_local_date IS NOT NULL
          AND date_conflict = TRUE
          AND reconciliation_status = 'reconciliation_required')
      )
      AND (
        (bed_at IS NULL AND bed_local_date IS NULL
          AND bed_utc_offset_seconds IS NULL AND bed_fold IS NULL)
        OR (bed_at IS NOT NULL AND bed_local_date IS NOT NULL
          AND bed_utc_offset_seconds IS NOT NULL AND bed_fold IS NOT NULL)
      )
      AND (
        (wake_at IS NULL AND wake_local_date IS NULL
          AND wake_utc_offset_seconds IS NULL AND wake_fold IS NULL)
        OR (wake_at IS NOT NULL AND wake_local_date IS NOT NULL
          AND wake_utc_offset_seconds IS NOT NULL AND wake_fold IS NOT NULL)
      )
    )
  ) NOT VALID;

ALTER TABLE sleep_domain_night_episodes
  ADD CONSTRAINT sleep_domain_night_episode_v2_namespace_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_night_episodes
  ADD CONSTRAINT sleep_domain_night_episode_v2_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT NOT VALID;

CREATE UNIQUE INDEX IF NOT EXISTS ux_sleep_domain_episode_anchor_v2
  ON sleep_domain_night_episodes (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''), episode_anchor_key
  )
  WHERE protocol_version >= 2;

CREATE UNIQUE INDEX IF NOT EXISTS ux_sleep_domain_main_episode_date_v2
  ON sleep_domain_night_episodes (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''),
    subject_id, episode_local_date
  )
  WHERE protocol_version >= 2
    AND date_state = 'finalized'
    AND date_conflict = FALSE;

CREATE INDEX IF NOT EXISTS idx_sleep_domain_episode_wake_date_v2
  ON sleep_domain_night_episodes (
    namespace_id, data_mode, subject_id, episode_local_date DESC,
    updated_at DESC
  )
  WHERE protocol_version >= 2;

ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN id_scheme TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN namespace_generation BIGINT;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN run_id TEXT;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN arm_id TEXT;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN episode_anchor_key TEXT;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN timezone_name TEXT;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN boundary_policy_version TEXT;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN bed_local_date DATE;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN wake_local_date DATE;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN vendor_wake_local_date DATE;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN episode_local_date DATE;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN assignment_basis TEXT;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN date_confidence TEXT;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN assignment_estimated BOOLEAN;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN date_state TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN date_conflict BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN episode_schema_version TEXT;

ALTER TABLE sleep_domain_night_episode_revisions
  ADD CONSTRAINT sleep_domain_night_revision_v2_contract CHECK (
    protocol_version < 2 OR (
      id_scheme = 'uuidv7'
      AND night_episode_revision_id ~
        '^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
      AND namespace_generation >= 1
      AND (
        (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
        OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
      )
      AND episode_anchor_key IS NOT NULL
      AND timezone_name IS NOT NULL
      AND boundary_policy_version IS NOT NULL
      AND episode_schema_version = 'night_episode.v2'
      AND date_state IN ('provisional', 'finalized', 'conflict')
      AND (
        (date_state = 'provisional' AND episode_local_date IS NULL
          AND assignment_basis = 'provisional'
          AND date_confidence = 'unknown')
        OR
        (date_state = 'finalized' AND episode_local_date IS NOT NULL
          AND assignment_basis IN (
            'observed_wake', 'vendor_wake_date', 'deadline_fallback'
          ) AND date_confidence IN (
            'observed', 'vendor_asserted', 'estimated'
          ) AND assignment_estimated IS NOT NULL)
        OR
        (date_state = 'conflict' AND episode_local_date IS NOT NULL
          AND date_conflict = TRUE)
      )
    )
  ) NOT VALID;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD CONSTRAINT sleep_domain_night_revision_v2_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD CONSTRAINT sleep_domain_night_revision_v2_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT NOT VALID;

ALTER TABLE sleep_domain_monitoring_snapshots
  ADD COLUMN active_episode_anchor_key TEXT;
ALTER TABLE sleep_domain_monitoring_snapshots
  ADD COLUMN active_namespace_generation BIGINT;
ALTER TABLE sleep_domain_monitoring_snapshots
  ADD COLUMN active_run_id TEXT;
ALTER TABLE sleep_domain_monitoring_snapshots
  ADD COLUMN active_arm_id TEXT;

CREATE TABLE IF NOT EXISTS backend_episode_date_reconciliation (
  reconciliation_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  candidate_night_episode_id TEXT NOT NULL,
  conflicting_night_episode_id TEXT NOT NULL,
  candidate_revision_id TEXT NOT NULL,
  proposed_episode_local_date DATE NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN ('reconciliation_required', 'resolved', 'rejected')
  ),
  reason_code TEXT NOT NULL,
  resolution_json JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  resolved_at TIMESTAMPTZ,
  UNIQUE (
    namespace_id, data_mode, candidate_night_episode_id,
    candidate_revision_id, proposed_episode_local_date
  ),
  FOREIGN KEY (candidate_night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (conflicting_night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (candidate_revision_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episode_revisions (
      night_episode_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (candidate_night_episode_id <> conflicting_night_episode_id),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  ),
  CHECK (
    (status = 'reconciliation_required' AND resolved_at IS NULL)
    OR (status IN ('resolved', 'rejected') AND resolved_at IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS backend_monitoring_snapshots_v2 (
  monitoring_snapshot_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('dormant', 'active')),
  active_night_episode_id TEXT,
  active_episode_anchor_key TEXT,
  cas_version BIGINT NOT NULL CHECK (cas_version >= 1),
  snapshot_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  ),
  CHECK (
    (state = 'dormant' AND active_night_episode_id IS NULL
      AND active_episode_anchor_key IS NULL)
    OR (state = 'active' AND active_night_episode_id IS NOT NULL
      AND active_episode_anchor_key IS NOT NULL)
  ),
  FOREIGN KEY (active_night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_backend_monitoring_snapshot_v2_scope
  ON backend_monitoring_snapshots_v2 (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''), subject_id
  );

ALTER TABLE backend_episode_date_reconciliation ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_episode_date_reconciliation FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_episode_date_reconciliation_scope
  ON backend_episode_date_reconciliation
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

DROP POLICY IF EXISTS sleep_domain_night_episode_scope
  ON sleep_domain_night_episodes;
CREATE POLICY sleep_domain_night_episode_scope ON sleep_domain_night_episodes
  USING (
    (protocol_version < 2 AND sleepagent_subject_scope_allows(
      namespace_id, data_mode, subject_id
    ))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  )
  WITH CHECK (
    (protocol_version < 2 AND sleepagent_subject_scope_allows(
      namespace_id, data_mode, subject_id
    ))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  );

DROP POLICY IF EXISTS sleep_domain_night_revision_scope
  ON sleep_domain_night_episode_revisions;
CREATE POLICY sleep_domain_night_revision_scope
  ON sleep_domain_night_episode_revisions
  USING (
    (protocol_version < 2 AND sleepagent_subject_scope_allows(
      namespace_id, data_mode, subject_id
    ))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  )
  WITH CHECK (
    (protocol_version < 2 AND sleepagent_subject_scope_allows(
      namespace_id, data_mode, subject_id
    ))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  );

ALTER TABLE backend_monitoring_snapshots_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_monitoring_snapshots_v2 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_monitoring_snapshot_v2_scope
  ON backend_monitoring_snapshots_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

-- sleepagent:transactional=true
-- Envelope-key metadata, retention work and auditable crypto-shred receipts.

CREATE TABLE IF NOT EXISTS backend_retention_classes (
  retention_class TEXT PRIMARY KEY,
  policy_version TEXT NOT NULL,
  retention_seconds BIGINT NOT NULL CHECK (retention_seconds > 0),
  expiry_action TEXT NOT NULL CHECK (
    expiry_action IN ('expire', 'crypto_shred', 'delete_projection', 'retain_audit')
  ),
  legal_hold_supported BOOLEAN NOT NULL DEFAULT FALSE,
  configuration_sha256 TEXT NOT NULL CHECK (
    configuration_sha256 ~ '^[0-9a-f]{64}$'
  ),
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS backend_retention_deks (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  retention_domain TEXT NOT NULL CHECK (
    retention_domain IN (
      'raw', 'conversation_checkpoint', 'personalized_memory_profile',
      'export_cache', 'audit_pseudonym'
    )
  ),
  generation BIGINT NOT NULL CHECK (generation >= 1),
  kek_key_id TEXT NOT NULL,
  wrapping_algorithm TEXT NOT NULL,
  wrapped_dek BYTEA,
  dek_sha256 TEXT NOT NULL CHECK (dek_sha256 ~ '^[0-9a-f]{64}$'),
  status TEXT NOT NULL CHECK (status IN ('active', 'retired', 'shredded')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  retired_at TIMESTAMPTZ,
  shredded_at TIMESTAMPTZ,
  PRIMARY KEY (
    namespace_id, data_mode, subject_id, retention_domain, generation
  ),
  FOREIGN KEY (namespace_id, data_mode, subject_id)
    REFERENCES backend_subjects (namespace_id, data_mode, subject_id)
    ON DELETE RESTRICT,
  CHECK (
    (status IN ('active', 'retired') AND wrapped_dek IS NOT NULL
      AND shredded_at IS NULL)
    OR (status = 'shredded' AND wrapped_dek IS NULL
      AND shredded_at IS NOT NULL)
  ),
  CHECK (retired_at IS NULL OR retired_at >= created_at),
  CHECK (shredded_at IS NULL OR shredded_at >= created_at),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_backend_retention_dek_active
  ON backend_retention_deks (
    namespace_id, data_mode, subject_id, retention_domain
  )
  WHERE status = 'active';

CREATE TABLE IF NOT EXISTS backend_retention_bindings (
  retention_binding_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  retention_domain TEXT NOT NULL,
  dek_generation BIGINT NOT NULL CHECK (dek_generation >= 1),
  retention_class TEXT NOT NULL
    REFERENCES backend_retention_classes(retention_class) ON DELETE RESTRICT,
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  legal_hold BOOLEAN NOT NULL DEFAULT FALSE,
  legal_hold_reason TEXT,
  binding_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (namespace_id, data_mode, resource_type, resource_id),
  FOREIGN KEY (
    namespace_id, data_mode, subject_id, retention_domain, dek_generation
  ) REFERENCES backend_retention_deks (
    namespace_id, data_mode, subject_id, retention_domain, generation
  ) ON DELETE RESTRICT,
  CHECK (
    (legal_hold = FALSE AND legal_hold_reason IS NULL)
    OR (legal_hold = TRUE AND legal_hold_reason IS NOT NULL)
  ),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_backend_retention_due
  ON backend_retention_bindings (
    namespace_id, data_mode, legal_hold, expires_at, retention_binding_id
  );

CREATE TABLE IF NOT EXISTS backend_retention_jobs (
  retention_job_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  retention_domain TEXT NOT NULL,
  dek_generation BIGINT NOT NULL CHECK (dek_generation >= 1),
  semantic_key TEXT NOT NULL,
  job_kind TEXT NOT NULL CHECK (
    job_kind IN ('scheduled_expiry', 'subject_forget', 'key_rotation')
  ),
  status TEXT NOT NULL CHECK (
    status IN (
      'pending', 'running', 'retry', 'succeeded', 'failed',
      'dead_letter', 'reconciliation_required'
    )
  ),
  priority INTEGER NOT NULL DEFAULT 0,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts INTEGER NOT NULL DEFAULT 8 CHECK (max_attempts >= 1),
  available_at TIMESTAMPTZ NOT NULL,
  lease_generation BIGINT NOT NULL DEFAULT 0,
  fencing_token TEXT,
  worker_instance TEXT,
  heartbeat_at TIMESTAMPTZ,
  lease_expires_at TIMESTAMPTZ,
  authorization_snapshot_json JSONB NOT NULL CHECK (
    jsonb_typeof(authorization_snapshot_json) = 'object'
    AND authorization_snapshot_json ?& ARRAY[
      'authorization_epoch', 'privacy_epoch', 'retrieval_policy_epoch'
    ]
  ),
  job_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (namespace_id, data_mode, semantic_key),
  UNIQUE (
    retention_job_id, namespace_id, data_mode, subject_id,
    retention_domain, dek_generation
  ),
  FOREIGN KEY (
    namespace_id, data_mode, subject_id, retention_domain, dek_generation
  ) REFERENCES backend_retention_deks (
    namespace_id, data_mode, subject_id, retention_domain, generation
  ) ON DELETE RESTRICT,
  CHECK (
    status <> 'running'
    OR (lease_generation >= 1 AND fencing_token IS NOT NULL
      AND worker_instance IS NOT NULL AND lease_expires_at IS NOT NULL)
  ),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_backend_retention_job_claim
  ON backend_retention_jobs (
    data_mode, status, priority DESC, available_at, created_at
  );

CREATE TABLE IF NOT EXISTS backend_shred_receipts (
  shred_receipt_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  subject_pseudonym TEXT NOT NULL,
  retention_job_id TEXT NOT NULL,
  retention_domain TEXT NOT NULL,
  dek_generation BIGINT NOT NULL CHECK (dek_generation >= 1),
  lease_generation BIGINT NOT NULL CHECK (lease_generation >= 1),
  fencing_token TEXT NOT NULL,
  policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  destroyed_json JSONB NOT NULL,
  expired_json JSONB NOT NULL,
  retained_with_reason_json JSONB NOT NULL,
  receipt_json JSONB NOT NULL,
  completed_at TIMESTAMPTZ NOT NULL,
  UNIQUE (
    namespace_id, data_mode, subject_id, retention_domain, dek_generation
  ),
  FOREIGN KEY (
    retention_job_id, namespace_id, data_mode, subject_id,
    retention_domain, dek_generation
  ) REFERENCES backend_retention_jobs (
    retention_job_id, namespace_id, data_mode, subject_id,
    retention_domain, dek_generation
  ) ON DELETE RESTRICT,
  CHECK (jsonb_typeof(destroyed_json) = 'array'),
  CHECK (jsonb_typeof(expired_json) = 'array'),
  CHECK (jsonb_typeof(retained_with_reason_json) = 'array'),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS backend_retention_events (
  retention_event_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  retention_job_id TEXT NOT NULL,
  retention_domain TEXT NOT NULL,
  dek_generation BIGINT NOT NULL CHECK (dek_generation >= 1),
  sequence BIGINT NOT NULL CHECK (sequence >= 1),
  event_type TEXT NOT NULL,
  lease_generation BIGINT NOT NULL CHECK (lease_generation >= 1),
  fencing_token TEXT NOT NULL,
  event_json JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  UNIQUE (retention_job_id, sequence),
  FOREIGN KEY (
    retention_job_id, namespace_id, data_mode, subject_id,
    retention_domain, dek_generation
  ) REFERENCES backend_retention_jobs (
    retention_job_id, namespace_id, data_mode, subject_id,
    retention_domain, dek_generation
  ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

ALTER TABLE backend_retention_deks
  ADD CONSTRAINT backend_retention_dek_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT;
ALTER TABLE backend_retention_deks
  ADD CONSTRAINT backend_retention_dek_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT;
ALTER TABLE backend_retention_jobs
  ADD CONSTRAINT backend_retention_job_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT;
ALTER TABLE backend_retention_jobs
  ADD CONSTRAINT backend_retention_job_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION sleepagent_retention_fence_allows(
  target_retention_job_id TEXT,
  target_namespace_id TEXT,
  target_data_mode TEXT,
  target_namespace_generation BIGINT,
  target_run_id TEXT,
  target_arm_id TEXT,
  target_subject_id TEXT,
  target_retention_domain TEXT,
  target_dek_generation BIGINT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT
    public.sleepagent_principal_context_allows()
    AND NULLIF(current_setting('sleepagent.process_role', TRUE), '') =
      'worker'
    AND public.sleepagent_subject_generation_scope_allows(
      target_namespace_id, target_data_mode, target_subject_id,
      target_namespace_generation, target_run_id, target_arm_id
    )
    AND EXISTS (
      SELECT 1
      FROM public.backend_retention_jobs AS job
      WHERE job.retention_job_id = target_retention_job_id
        AND job.namespace_id = target_namespace_id
        AND job.data_mode = target_data_mode
        AND job.namespace_generation = target_namespace_generation
        AND job.run_id IS NOT DISTINCT FROM target_run_id
        AND job.arm_id IS NOT DISTINCT FROM target_arm_id
        AND job.subject_id = target_subject_id
        AND job.retention_domain = target_retention_domain
        AND job.dek_generation = target_dek_generation
        AND job.status = 'running'
        AND job.lease_generation = expected_lease_generation
        AND job.fencing_token = expected_fencing_token
        AND job.worker_instance = NULLIF(
          current_setting('sleepagent.worker_instance', TRUE), ''
        )
        AND job.lease_expires_at > clock_timestamp()
        AND job.authorization_snapshot_json ->> 'authorization_epoch' =
          NULLIF(current_setting(
            'sleepagent.authorization_epoch', TRUE
          ), '')
        AND job.authorization_snapshot_json ->> 'privacy_epoch' =
          NULLIF(current_setting('sleepagent.privacy_epoch', TRUE), '')
        AND job.authorization_snapshot_json ->> 'retrieval_policy_epoch' =
          NULLIF(current_setting(
            'sleepagent.retrieval_policy_epoch', TRUE
          ), '')
    )
$$;

REVOKE ALL ON FUNCTION sleepagent_retention_fence_allows(
  TEXT, TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT, BIGINT, BIGINT, TEXT
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_protect_retention_dek()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'retention DEK metadata cannot be deleted';
  END IF;
  IF NEW.namespace_id <> OLD.namespace_id
     OR NEW.data_mode <> OLD.data_mode
     OR NEW.subject_id <> OLD.subject_id
     OR NEW.retention_domain <> OLD.retention_domain
     OR NEW.generation <> OLD.generation
     OR NEW.dek_sha256 <> OLD.dek_sha256
     OR NEW.created_at <> OLD.created_at THEN
    RAISE EXCEPTION 'retention DEK identity is immutable';
  END IF;
  IF OLD.status = 'shredded' THEN
    RAISE EXCEPTION 'shredded retention DEK metadata is terminal';
  END IF;
  IF OLD.status = 'retired' AND NEW.status NOT IN ('retired', 'shredded') THEN
    RAISE EXCEPTION 'retired retention DEK may only transition to shredded';
  END IF;
  IF OLD.status = 'active'
     AND NEW.status NOT IN ('active', 'retired', 'shredded') THEN
    RAISE EXCEPTION 'invalid retention DEK state transition';
  END IF;
  IF NEW.status = 'shredded' AND OLD.status <> 'shredded'
     AND NOT EXISTS (
       SELECT 1
       FROM public.backend_shred_receipts AS receipt
       WHERE receipt.namespace_id = NEW.namespace_id
         AND receipt.data_mode = NEW.data_mode
         AND receipt.namespace_generation = NEW.namespace_generation
         AND receipt.run_id IS NOT DISTINCT FROM NEW.run_id
         AND receipt.arm_id IS NOT DISTINCT FROM NEW.arm_id
         AND receipt.subject_id = NEW.subject_id
         AND receipt.retention_domain = NEW.retention_domain
         AND receipt.dek_generation = NEW.generation
         AND public.sleepagent_retention_fence_allows(
           receipt.retention_job_id, receipt.namespace_id,
           receipt.data_mode, receipt.namespace_generation,
           receipt.run_id, receipt.arm_id, receipt.subject_id,
           receipt.retention_domain, receipt.dek_generation,
           receipt.lease_generation, receipt.fencing_token
         )
     ) THEN
    RAISE EXCEPTION
      'retention DEK shred requires a current fenced job and receipt';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS backend_retention_dek_state_guard
  ON backend_retention_deks;
CREATE TRIGGER backend_retention_dek_state_guard
BEFORE UPDATE OR DELETE ON backend_retention_deks
FOR EACH ROW EXECUTE FUNCTION sleepagent_protect_retention_dek();

CREATE OR REPLACE FUNCTION sleepagent_enforce_shred_receipt_fence()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF NOT public.sleepagent_retention_fence_allows(
    NEW.retention_job_id, NEW.namespace_id, NEW.data_mode,
    NEW.namespace_generation, NEW.run_id, NEW.arm_id, NEW.subject_id,
    NEW.retention_domain, NEW.dek_generation, NEW.lease_generation,
    NEW.fencing_token
  ) THEN
    RAISE EXCEPTION 'stale or unauthorized retention work fence';
  END IF;
  IF TG_TABLE_NAME = 'backend_shred_receipts' THEN
    NEW.completed_at := clock_timestamp();
  ELSIF TG_TABLE_NAME = 'backend_retention_events' THEN
    NEW.occurred_at := clock_timestamp();
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS backend_shred_receipt_fence
  ON backend_shred_receipts;
CREATE TRIGGER backend_shred_receipt_fence
BEFORE INSERT ON backend_shred_receipts
FOR EACH ROW EXECUTE FUNCTION sleepagent_enforce_shred_receipt_fence();

DROP TRIGGER IF EXISTS backend_retention_event_fence
  ON backend_retention_events;
CREATE TRIGGER backend_retention_event_fence
BEFORE INSERT ON backend_retention_events
FOR EACH ROW EXECUTE FUNCTION sleepagent_enforce_shred_receipt_fence();

DROP TRIGGER IF EXISTS backend_shred_receipt_immutable
  ON backend_shred_receipts;
CREATE TRIGGER backend_shred_receipt_immutable
BEFORE UPDATE OR DELETE ON backend_shred_receipts
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

DROP TRIGGER IF EXISTS backend_retention_event_immutable
  ON backend_retention_events;
CREATE TRIGGER backend_retention_event_immutable
BEFORE UPDATE OR DELETE ON backend_retention_events
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

-- Existing encrypted payloads/manifests remain protocol v1.  New production
-- encryption records exact retention domain and DEK generation.
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN encryption_protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN retention_subject_id TEXT;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN retention_domain TEXT;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN dek_generation BIGINT;
ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_encryption_v2_contract CHECK (
    encryption_protocol_version < 2 OR (
      retention_subject_id IS NOT NULL
      AND retention_domain = 'raw'
      AND dek_generation >= 1
    )
  ) NOT VALID;

ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_encryption_v2_dek_fk
  FOREIGN KEY (
    namespace_id, data_mode, retention_subject_id,
    retention_domain, dek_generation
  ) REFERENCES backend_retention_deks (
    namespace_id, data_mode, subject_id, retention_domain, generation
  ) ON DELETE RESTRICT NOT VALID;

ALTER TABLE product_induction_manifests
  ADD COLUMN IF NOT EXISTS encryption_protocol_version INTEGER
    NOT NULL DEFAULT 1;
ALTER TABLE product_induction_manifests
  ADD COLUMN IF NOT EXISTS retention_domain TEXT;
ALTER TABLE product_induction_manifests
  ADD COLUMN IF NOT EXISTS dek_generation BIGINT;
ALTER TABLE product_induction_manifests
  ADD CONSTRAINT product_induction_manifest_encryption_v2_contract CHECK (
    encryption_protocol_version < 2 OR (
      namespace_id IS NOT NULL
      AND data_mode IS NOT NULL
      AND retention_domain = 'personalized_memory_profile'
      AND dek_generation >= 1
      AND wrapped_data_key IS NOT NULL
      AND key_id IS NOT NULL
    )
  ) NOT VALID;

ALTER TABLE product_induction_manifests
  ADD CONSTRAINT product_induction_manifest_encryption_v2_dek_fk
  FOREIGN KEY (
    namespace_id, data_mode, subject_id, retention_domain, dek_generation
  ) REFERENCES backend_retention_deks (
    namespace_id, data_mode, subject_id, retention_domain, generation
  ) ON DELETE RESTRICT NOT VALID;

CREATE OR REPLACE FUNCTION sleepagent_claim_retention_job(
  claimant_worker_instance TEXT,
  requested_lease_seconds INTEGER
)
RETURNS TABLE (
  retention_job_id TEXT,
  namespace_id TEXT,
  data_mode TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT,
  retention_domain TEXT,
  dek_generation BIGINT,
  authorization_epoch BIGINT,
  privacy_epoch BIGINT,
  retrieval_policy_epoch BIGINT,
  lease_generation BIGINT,
  fencing_token TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  principal TEXT := NULLIF(
    current_setting('sleepagent.service_principal_id', TRUE), ''
  );
  deployment_mode TEXT := NULLIF(
    current_setting('sleepagent.data_mode', TRUE), ''
  );
  request_purpose TEXT := NULLIF(
    current_setting('sleepagent.purpose', TRUE), ''
  );
  caller_process_role TEXT := NULLIF(
    current_setting('sleepagent.process_role', TRUE), ''
  );
BEGIN
  IF principal IS NULL OR deployment_mode NOT IN ('live', 'replay')
     OR request_purpose IS NULL OR caller_process_role <> 'worker'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION
      'trusted retention principal/data mode/purpose context is required';
  END IF;
  IF claimant_worker_instance IS NULL OR claimant_worker_instance = ''
     OR requested_lease_seconds < 1 OR requested_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'invalid retention claim parameters';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT job.retention_job_id,
      epoch_row.authorization_epoch AS claim_authorization_epoch,
      epoch_row.privacy_epoch AS claim_privacy_epoch,
      epoch_row.retrieval_policy_epoch AS claim_retrieval_policy_epoch
    FROM public.backend_retention_jobs AS job
    JOIN public.backend_namespaces AS ns
     ON ns.namespace_id = job.namespace_id
     AND ns.data_mode = job.data_mode
    JOIN public.backend_subject_epochs AS epoch_row
      ON epoch_row.namespace_id = job.namespace_id
     AND epoch_row.data_mode = job.data_mode
     AND epoch_row.subject_id = job.subject_id
    WHERE job.data_mode = deployment_mode
      AND (
        job.status IN ('pending', 'retry')
        OR (job.status = 'running'
          AND job.lease_expires_at <= clock_timestamp())
      )
      AND job.available_at <= clock_timestamp()
      AND job.attempt_count < job.max_attempts
      AND ns.status = 'active'
      AND job.authorization_snapshot_json ->> 'authorization_epoch' =
        epoch_row.authorization_epoch::text
      AND job.authorization_snapshot_json ->> 'privacy_epoch' =
        epoch_row.privacy_epoch::text
      AND job.authorization_snapshot_json ->> 'retrieval_policy_epoch' =
        epoch_row.retrieval_policy_epoch::text
      AND EXISTS (
        SELECT 1
        FROM public.backend_principal_grants AS grant_row
        WHERE grant_row.principal_id = principal
          AND grant_row.namespace_id = job.namespace_id
          AND grant_row.data_mode = job.data_mode
          AND grant_row.purpose = request_purpose
          AND grant_row.authorization_epoch =
            epoch_row.authorization_epoch
          AND grant_row.status = 'active'
          AND grant_row.valid_from <= clock_timestamp()
          AND (
            grant_row.valid_until IS NULL
            OR grant_row.valid_until > clock_timestamp()
          )
          AND grant_row.allowed_handlers_json ? 'retention'
          AND EXISTS (
            SELECT 1
            FROM public.backend_service_principals AS service_principal
            WHERE service_principal.principal_id = principal
              AND service_principal.status = 'active'
          )
      )
    ORDER BY job.priority DESC, job.available_at, job.created_at,
      job.retention_job_id
    FOR UPDATE OF ns, job SKIP LOCKED
    LIMIT 1
  ), claimed AS (
    UPDATE public.backend_retention_jobs AS job
    SET status = 'running',
        attempt_count = job.attempt_count + 1,
        lease_generation = job.lease_generation + 1,
        fencing_token = gen_random_uuid()::text,
        worker_instance = claimant_worker_instance,
        heartbeat_at = clock_timestamp(),
        lease_expires_at = clock_timestamp()
          + make_interval(secs => requested_lease_seconds),
        updated_at = clock_timestamp()
    FROM candidate
    WHERE job.retention_job_id = candidate.retention_job_id
    RETURNING job.retention_job_id, job.namespace_id, job.data_mode,
      job.namespace_generation, job.run_id, job.arm_id, job.subject_id,
      job.retention_domain, job.dek_generation,
      candidate.claim_authorization_epoch, candidate.claim_privacy_epoch,
      candidate.claim_retrieval_policy_epoch, job.lease_generation,
      job.fencing_token
  )
  SELECT claimed.retention_job_id, claimed.namespace_id, claimed.data_mode,
    claimed.namespace_generation, claimed.run_id, claimed.arm_id,
    claimed.subject_id, claimed.retention_domain, claimed.dek_generation,
    claimed.claim_authorization_epoch, claimed.claim_privacy_epoch,
    claimed.claim_retrieval_policy_epoch,
    claimed.lease_generation, claimed.fencing_token
  FROM claimed;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_claim_retention_job(TEXT, INTEGER)
  FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_heartbeat_retention_job(
  target_retention_job_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_lease_seconds INTEGER
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_lease_seconds < 1 OR requested_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'invalid retention heartbeat lease duration';
  END IF;
  UPDATE public.backend_retention_jobs
  SET heartbeat_at = clock_timestamp(),
      lease_expires_at = clock_timestamp()
        + make_interval(secs => requested_lease_seconds),
      updated_at = clock_timestamp()
  WHERE retention_job_id = target_retention_job_id
    AND status = 'running'
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_heartbeat_retention_job(
  TEXT, BIGINT, TEXT, INTEGER
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_finalize_retention_job(
  target_retention_job_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_final_status TEXT,
  requested_next_available_at TIMESTAMPTZ
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_final_status NOT IN (
    'succeeded', 'retry', 'failed', 'dead_letter',
    'reconciliation_required'
  ) OR (requested_final_status = 'retry'
    AND requested_next_available_at IS NULL) THEN
    RAISE EXCEPTION 'invalid retention final status';
  END IF;
  UPDATE public.backend_retention_jobs
  SET status = requested_final_status,
      available_at = COALESCE(requested_next_available_at, available_at),
      lease_expires_at = NULL,
      worker_instance = NULL,
      updated_at = clock_timestamp()
  WHERE retention_job_id = target_retention_job_id
    AND status = 'running'
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp()
    AND authorization_snapshot_json ->> 'authorization_epoch' = NULLIF(
      current_setting('sleepagent.authorization_epoch', TRUE), ''
    )
    AND authorization_snapshot_json ->> 'privacy_epoch' = NULLIF(
      current_setting('sleepagent.privacy_epoch', TRUE), ''
    )
    AND authorization_snapshot_json ->> 'retrieval_policy_epoch' = NULLIF(
      current_setting('sleepagent.retrieval_policy_epoch', TRUE), ''
    );
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_finalize_retention_job(
  TEXT, BIGINT, TEXT, TEXT, TIMESTAMPTZ
) FROM PUBLIC;

ALTER TABLE backend_retention_deks ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_retention_deks FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_retention_dek_scope ON backend_retention_deks
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_retention_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_retention_bindings FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_retention_binding_scope ON backend_retention_bindings
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_retention_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_retention_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_retention_job_scope ON backend_retention_jobs
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_shred_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_shred_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_shred_receipt_scope ON backend_shred_receipts
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_retention_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_retention_events FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_retention_event_scope ON backend_retention_events
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

-- Canonical cleanup: development-only fixed/dynamic runtime storage is not
-- part of the pre-production baseline. Current user-input state remains on
-- radar_user_input_requests because ProductEpisodeRuntime owns that workflow.

ALTER TABLE radar_tasks
  ALTER COLUMN runtime_kind SET DEFAULT 'product_episode';
ALTER TABLE radar_tasks
  ALTER COLUMN runtime_contract_version SET DEFAULT 'product-episode.v1';
ALTER TABLE radar_tasks
  ADD CONSTRAINT radar_tasks_canonical_runtime_kind
  CHECK (runtime_kind = 'product_episode');
