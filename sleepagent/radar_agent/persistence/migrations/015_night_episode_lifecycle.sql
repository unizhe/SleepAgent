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

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('015_night_episode_lifecycle', CURRENT_TIMESTAMP)
ON CONFLICT (version) DO NOTHING;
