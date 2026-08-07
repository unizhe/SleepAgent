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

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('013_perceptor_push_ingestion', CURRENT_TIMESTAMP)
ON CONFLICT (version) DO NOTHING;
