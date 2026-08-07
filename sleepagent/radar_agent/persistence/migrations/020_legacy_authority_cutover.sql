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

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('020_legacy_authority_cutover', CURRENT_TIMESTAMP)
ON CONFLICT (version) DO NOTHING;
