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

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('011_adapter_registry_control', CURRENT_TIMESTAMP)
ON CONFLICT (version) DO NOTHING;
