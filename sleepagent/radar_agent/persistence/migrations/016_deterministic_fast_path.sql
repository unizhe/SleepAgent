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

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('016_deterministic_fast_path', CURRENT_TIMESTAMP)
ON CONFLICT (version) DO NOTHING;
