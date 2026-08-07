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

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('018_sleep_api_v1', CURRENT_TIMESTAMP)
ON CONFLICT (version) DO NOTHING;
