-- sleepagent:transactional=true
-- Stable UUIDv7 NightEpisode identity and explicit wake-date assignment.

ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN id_scheme TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN namespace_generation BIGINT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN run_id TEXT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN arm_id TEXT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN episode_anchor_key TEXT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN opening_source_idempotency_identity TEXT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN timezone_name TEXT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN boundary_policy_version TEXT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN collection_start_at TIMESTAMPTZ;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN deterministic_close_deadline_at TIMESTAMPTZ;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN bed_at TIMESTAMPTZ;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN wake_at TIMESTAMPTZ;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN bed_local_date DATE;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN wake_local_date DATE;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN vendor_wake_local_date DATE;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN episode_local_date DATE;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN assignment_basis TEXT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN date_confidence TEXT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN assignment_estimated BOOLEAN;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN date_state TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN date_finalized_at TIMESTAMPTZ;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN bed_utc_offset_seconds INTEGER;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN wake_utc_offset_seconds INTEGER;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN bed_fold SMALLINT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN wake_fold SMALLINT;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN date_conflict BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE sleep_domain_night_episodes
  ADD COLUMN reconciliation_status TEXT;

ALTER TABLE sleep_domain_night_episodes
  ADD CONSTRAINT sleep_domain_night_episode_v2_contract CHECK (
    protocol_version < 2 OR (
      id_scheme = 'uuidv7'
      AND night_episode_id ~
        '^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
      AND namespace_generation >= 1
      AND (
        (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
        OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
      )
      AND episode_anchor_key IS NOT NULL
      AND episode_anchor_key <> ''
      AND opening_source_idempotency_identity IS NOT NULL
      AND opening_source_idempotency_identity <> ''
      AND timezone_name IS NOT NULL
      AND timezone_name <> ''
      AND boundary_policy_version IS NOT NULL
      AND boundary_policy_version <> ''
      AND collection_start_at IS NOT NULL
      AND deterministic_close_deadline_at > collection_start_at
      AND date_state IN ('provisional', 'finalized', 'conflict')
      AND (bed_fold IS NULL OR bed_fold IN (0, 1))
      AND (wake_fold IS NULL OR wake_fold IN (0, 1))
      AND (
        bed_utc_offset_seconds IS NULL
        OR bed_utc_offset_seconds BETWEEN -64800 AND 64800
      )
      AND (
        wake_utc_offset_seconds IS NULL
        OR wake_utc_offset_seconds BETWEEN -64800 AND 64800
      )
      AND (
        (date_state = 'provisional'
          AND episode_local_date IS NULL
          AND assignment_basis = 'provisional'
          AND date_confidence = 'unknown'
          AND assignment_estimated IS NULL
          AND date_finalized_at IS NULL
          AND date_conflict = FALSE
          AND reconciliation_status IS NULL)
        OR
        (date_state = 'finalized'
          AND episode_local_date IS NOT NULL
          AND assignment_basis IN (
            'observed_wake', 'vendor_wake_date', 'deadline_fallback'
          )
          AND date_confidence IN (
            'observed', 'vendor_asserted', 'estimated'
          )
          AND assignment_estimated IS NOT NULL
          AND date_finalized_at IS NOT NULL
          AND date_conflict = FALSE
          AND reconciliation_status IS NULL
          AND (
            (assignment_basis = 'observed_wake'
              AND wake_local_date = episode_local_date
              AND wake_at IS NOT NULL
              AND date_confidence = 'observed'
              AND assignment_estimated = FALSE)
            OR
            (assignment_basis = 'vendor_wake_date'
              AND vendor_wake_local_date = episode_local_date
              AND date_confidence = 'vendor_asserted'
              AND assignment_estimated = FALSE)
            OR
            (assignment_basis = 'deadline_fallback'
              AND assignment_estimated = TRUE
              AND date_confidence = 'estimated')
          ))
        OR
        (date_state = 'conflict'
          AND episode_local_date IS NOT NULL
          AND date_conflict = TRUE
          AND reconciliation_status = 'reconciliation_required')
      )
      AND (
        (bed_at IS NULL AND bed_local_date IS NULL
          AND bed_utc_offset_seconds IS NULL AND bed_fold IS NULL)
        OR (bed_at IS NOT NULL AND bed_local_date IS NOT NULL
          AND bed_utc_offset_seconds IS NOT NULL AND bed_fold IS NOT NULL)
      )
      AND (
        (wake_at IS NULL AND wake_local_date IS NULL
          AND wake_utc_offset_seconds IS NULL AND wake_fold IS NULL)
        OR (wake_at IS NOT NULL AND wake_local_date IS NOT NULL
          AND wake_utc_offset_seconds IS NOT NULL AND wake_fold IS NOT NULL)
      )
    )
  ) NOT VALID;

ALTER TABLE sleep_domain_night_episodes
  ADD CONSTRAINT sleep_domain_night_episode_v2_namespace_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_night_episodes
  ADD CONSTRAINT sleep_domain_night_episode_v2_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT NOT VALID;

CREATE UNIQUE INDEX IF NOT EXISTS ux_sleep_domain_episode_anchor_v2
  ON sleep_domain_night_episodes (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''), episode_anchor_key
  )
  WHERE protocol_version >= 2;

CREATE UNIQUE INDEX IF NOT EXISTS ux_sleep_domain_main_episode_date_v2
  ON sleep_domain_night_episodes (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''),
    subject_id, episode_local_date
  )
  WHERE protocol_version >= 2
    AND date_state = 'finalized'
    AND date_conflict = FALSE;

CREATE INDEX IF NOT EXISTS idx_sleep_domain_episode_wake_date_v2
  ON sleep_domain_night_episodes (
    namespace_id, data_mode, subject_id, episode_local_date DESC,
    updated_at DESC
  )
  WHERE protocol_version >= 2;

ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN id_scheme TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN namespace_generation BIGINT;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN run_id TEXT;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN arm_id TEXT;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN episode_anchor_key TEXT;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN timezone_name TEXT;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN boundary_policy_version TEXT;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN bed_local_date DATE;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN wake_local_date DATE;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN vendor_wake_local_date DATE;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN episode_local_date DATE;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN assignment_basis TEXT;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN date_confidence TEXT;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN assignment_estimated BOOLEAN;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN date_state TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN date_conflict BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD COLUMN episode_schema_version TEXT;

ALTER TABLE sleep_domain_night_episode_revisions
  ADD CONSTRAINT sleep_domain_night_revision_v2_contract CHECK (
    protocol_version < 2 OR (
      id_scheme = 'uuidv7'
      AND night_episode_revision_id ~
        '^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
      AND namespace_generation >= 1
      AND (
        (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
        OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
      )
      AND episode_anchor_key IS NOT NULL
      AND timezone_name IS NOT NULL
      AND boundary_policy_version IS NOT NULL
      AND episode_schema_version = 'night_episode.v2'
      AND date_state IN ('provisional', 'finalized', 'conflict')
      AND (
        (date_state = 'provisional' AND episode_local_date IS NULL
          AND assignment_basis = 'provisional'
          AND date_confidence = 'unknown')
        OR
        (date_state = 'finalized' AND episode_local_date IS NOT NULL
          AND assignment_basis IN (
            'observed_wake', 'vendor_wake_date', 'deadline_fallback'
          ) AND date_confidence IN (
            'observed', 'vendor_asserted', 'estimated'
          ) AND assignment_estimated IS NOT NULL)
        OR
        (date_state = 'conflict' AND episode_local_date IS NOT NULL
          AND date_conflict = TRUE)
      )
    )
  ) NOT VALID;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD CONSTRAINT sleep_domain_night_revision_v2_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_night_episode_revisions
  ADD CONSTRAINT sleep_domain_night_revision_v2_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT NOT VALID;

ALTER TABLE sleep_domain_monitoring_snapshots
  ADD COLUMN active_episode_anchor_key TEXT;
ALTER TABLE sleep_domain_monitoring_snapshots
  ADD COLUMN active_namespace_generation BIGINT;
ALTER TABLE sleep_domain_monitoring_snapshots
  ADD COLUMN active_run_id TEXT;
ALTER TABLE sleep_domain_monitoring_snapshots
  ADD COLUMN active_arm_id TEXT;

CREATE TABLE IF NOT EXISTS backend_episode_date_reconciliation (
  reconciliation_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  candidate_night_episode_id TEXT NOT NULL,
  conflicting_night_episode_id TEXT NOT NULL,
  candidate_revision_id TEXT NOT NULL,
  proposed_episode_local_date DATE NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN ('reconciliation_required', 'resolved', 'rejected')
  ),
  reason_code TEXT NOT NULL,
  resolution_json JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  resolved_at TIMESTAMPTZ,
  UNIQUE (
    namespace_id, data_mode, candidate_night_episode_id,
    candidate_revision_id, proposed_episode_local_date
  ),
  FOREIGN KEY (candidate_night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (conflicting_night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (candidate_revision_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episode_revisions (
      night_episode_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (candidate_night_episode_id <> conflicting_night_episode_id),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  ),
  CHECK (
    (status = 'reconciliation_required' AND resolved_at IS NULL)
    OR (status IN ('resolved', 'rejected') AND resolved_at IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS backend_monitoring_snapshots_v2 (
  monitoring_snapshot_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('dormant', 'active')),
  active_night_episode_id TEXT,
  active_episode_anchor_key TEXT,
  cas_version BIGINT NOT NULL CHECK (cas_version >= 1),
  snapshot_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  ),
  CHECK (
    (state = 'dormant' AND active_night_episode_id IS NULL
      AND active_episode_anchor_key IS NULL)
    OR (state = 'active' AND active_night_episode_id IS NOT NULL
      AND active_episode_anchor_key IS NOT NULL)
  ),
  FOREIGN KEY (active_night_episode_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_backend_monitoring_snapshot_v2_scope
  ON backend_monitoring_snapshots_v2 (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''), subject_id
  );

ALTER TABLE backend_episode_date_reconciliation ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_episode_date_reconciliation FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_episode_date_reconciliation_scope
  ON backend_episode_date_reconciliation
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

DROP POLICY IF EXISTS sleep_domain_night_episode_scope
  ON sleep_domain_night_episodes;
CREATE POLICY sleep_domain_night_episode_scope ON sleep_domain_night_episodes
  USING (
    (protocol_version < 2 AND sleepagent_subject_scope_allows(
      namespace_id, data_mode, subject_id
    ))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  )
  WITH CHECK (
    (protocol_version < 2 AND sleepagent_subject_scope_allows(
      namespace_id, data_mode, subject_id
    ))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  );

DROP POLICY IF EXISTS sleep_domain_night_revision_scope
  ON sleep_domain_night_episode_revisions;
CREATE POLICY sleep_domain_night_revision_scope
  ON sleep_domain_night_episode_revisions
  USING (
    (protocol_version < 2 AND sleepagent_subject_scope_allows(
      namespace_id, data_mode, subject_id
    ))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  )
  WITH CHECK (
    (protocol_version < 2 AND sleepagent_subject_scope_allows(
      namespace_id, data_mode, subject_id
    ))
    OR (protocol_version >= 2
      AND sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation,
        run_id, arm_id
      ))
  );

ALTER TABLE backend_monitoring_snapshots_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_monitoring_snapshots_v2 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_monitoring_snapshot_v2_scope
  ON backend_monitoring_snapshots_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));
