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

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('012_device_binding_promotion', CURRENT_TIMESTAMP)
ON CONFLICT (version) DO NOTHING;
