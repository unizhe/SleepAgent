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

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('010_sleep_domain_foundation', CURRENT_TIMESTAMP)
ON CONFLICT(version) DO NOTHING;
