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

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('014_perceptor_pull_reconciliation', CURRENT_TIMESTAMP)
ON CONFLICT (version) DO NOTHING;
