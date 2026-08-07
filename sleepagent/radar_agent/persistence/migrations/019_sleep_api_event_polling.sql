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

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('019_sleep_api_event_polling', CURRENT_TIMESTAMP)
ON CONFLICT (version) DO NOTHING;
