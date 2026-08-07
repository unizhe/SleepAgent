ALTER TABLE sleep_domain_analysis_revisions
  ADD COLUMN analysis_run_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_sleep_domain_analysis_run
  ON sleep_domain_analysis_revisions (
    namespace_id, data_mode, analysis_run_id
  )
  WHERE analysis_run_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS sleep_domain_analysis_role_views (
  role_view_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  analysis_revision_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  night_episode_revision_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('elder', 'family', 'doctor')),
  status TEXT NOT NULL CHECK (
    status IN ('ready', 'degraded', 'pending', 'blocked')
  ),
  product_agent_episode_id TEXT NOT NULL,
  view_json JSONB NOT NULL,
  generated_at TIMESTAMPTZ NOT NULL,
  UNIQUE (role_view_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, analysis_revision_id, role),
  FOREIGN KEY (analysis_revision_id, namespace_id, data_mode)
    REFERENCES sleep_domain_analysis_revisions (
      analysis_revision_id, namespace_id, data_mode
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

CREATE INDEX IF NOT EXISTS idx_sleep_domain_role_view_subject
  ON sleep_domain_analysis_role_views (
    namespace_id, data_mode, subject_id, role, generated_at
  );

CREATE INDEX IF NOT EXISTS idx_sleep_domain_agent_operation_fairness
  ON sleep_domain_operations (
    namespace_id, data_mode, operation_type, subject_id,
    status, lease_expires_at, updated_at, created_at
  );

INSERT INTO radar_agent_schema_migrations (version, applied_at)
VALUES ('017_product_agent_bridge', CURRENT_TIMESTAMP)
ON CONFLICT (version) DO NOTHING;
