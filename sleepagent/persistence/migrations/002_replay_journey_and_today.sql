-- sleepagent:transactional=true
-- Replay journey orchestration, ordered replay ingress, and typed public
-- Product /today projections.  The immutable 001 baseline remains untouched.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE backend_demo_seed_allowlist
  ADD COLUMN artifact_family TEXT;
ALTER TABLE backend_demo_seed_allowlist
  ADD COLUMN scenario_id TEXT;
ALTER TABLE backend_demo_seed_allowlist
  ADD COLUMN adapter_version TEXT;
ALTER TABLE backend_demo_seed_allowlist
  ADD COLUMN manifest_schema_version TEXT;
ALTER TABLE backend_demo_seed_allowlist
  ADD COLUMN expected_observation_count INTEGER;
ALTER TABLE backend_demo_seed_allowlist
  ADD COLUMN component_pins_sha256 TEXT;
ALTER TABLE backend_demo_seed_allowlist
  ADD COLUMN canonical_sequence_sha256 TEXT;
ALTER TABLE backend_demo_seed_allowlist
  ADD COLUMN manifest_sha256 TEXT;
ALTER TABLE backend_demo_seed_allowlist
  ADD COLUMN first_received_at TIMESTAMPTZ;
ALTER TABLE backend_demo_seed_allowlist
  ADD COLUMN last_received_at TIMESTAMPTZ;
ALTER TABLE backend_demo_seed_allowlist
  ADD COLUMN night_count INTEGER;

ALTER TABLE backend_demo_seed_allowlist
  ADD CONSTRAINT backend_demo_seed_allowlist_v2_contract CHECK (
    artifact_family IS NULL OR (
      artifact_family <> ''
      AND scenario_id IS NOT NULL AND scenario_id <> ''
      AND adapter_version IS NOT NULL AND adapter_version <> ''
      AND manifest_schema_version = 'replay_ingress_manifest.v1'
      AND expected_observation_count >= 1
      AND component_pins_sha256 ~ '^[0-9a-f]{64}$'
      AND canonical_sequence_sha256 ~ '^[0-9a-f]{64}$'
      AND manifest_sha256 ~ '^[0-9a-f]{64}$'
      AND first_received_at IS NOT NULL
      AND last_received_at >= first_received_at
      AND night_count = 1
    )
  ) NOT VALID;

CREATE UNIQUE INDEX IF NOT EXISTS ux_backend_demo_seed_scenario_v2
  ON backend_demo_seed_allowlist (artifact_family, scenario_id)
  WHERE artifact_family IS NOT NULL;

ALTER TABLE backend_command_receipts
  ALTER COLUMN actor_id DROP NOT NULL;
ALTER TABLE backend_command_receipts
  ALTER COLUMN authorization_snapshot_json DROP NOT NULL;
ALTER TABLE backend_command_receipts
  ADD COLUMN origin_kind TEXT NOT NULL DEFAULT 'user';
ALTER TABLE backend_command_receipts
  ADD COLUMN workload_authorization_snapshot_json JSONB;
ALTER TABLE backend_command_receipts
  ADD CONSTRAINT backend_command_receipt_origin_v2 CHECK (
    (
      origin_kind = 'user'
      AND actor_id IS NOT NULL
      AND authorization_snapshot_json IS NOT NULL
      AND workload_authorization_snapshot_json IS NULL
    )
    OR (
      origin_kind = 'system'
      AND actor_id IS NULL
      AND authorization_snapshot_json IS NULL
      AND workload_authorization_snapshot_json IS NOT NULL
      AND jsonb_typeof(workload_authorization_snapshot_json) = 'object'
      AND workload_authorization_snapshot_json ?& ARRAY[
        'authorization_epoch', 'privacy_epoch', 'retrieval_policy_epoch'
      ]
    )
  ) NOT VALID;

CREATE UNIQUE INDEX ux_backend_command_receipt_caller_v2
  ON backend_command_receipts (
    service_principal_id, COALESCE(actor_id, ''), route_template,
    caller_idempotency_key
  );

CREATE TABLE backend_demo_journeys (
  journey_id TEXT PRIMARY KEY,
  root_operation_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT NOT NULL,
  arm_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  seed_id TEXT NOT NULL
    REFERENCES backend_demo_seed_allowlist(seed_id) ON DELETE RESTRICT,
  semantic_key TEXT NOT NULL,
  scenario_sha256 TEXT NOT NULL CHECK (
    scenario_sha256 ~ '^[0-9a-f]{64}$'
  ),
  manifest_sha256 TEXT NOT NULL CHECK (
    manifest_sha256 ~ '^[0-9a-f]{64}$'
  ),
  generator_version TEXT NOT NULL,
  adapter_version TEXT NOT NULL,
  model_version TEXT NOT NULL,
  policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  schema_manifest_sha256 TEXT NOT NULL CHECK (
    schema_manifest_sha256 ~ '^[0-9a-f]{64}$'
  ),
  phase TEXT NOT NULL CHECK (
    phase IN (
      'accepted', 'staging_input', 'waiting_normalization',
      'waiting_episode', 'waiting_fast_path', 'waiting_product',
      'verifying_views', 'succeeded', 'blocked',
      'reconciliation_required', 'failed'
    )
  ),
  version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
  resume_at TIMESTAMPTZ NOT NULL,
  deadline_at TIMESTAMPTZ NOT NULL,
  wait_count BIGINT NOT NULL DEFAULT 0 CHECK (wait_count >= 0),
  failure_attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (
    failure_attempt_count >= 0
  ),
  max_failure_attempts INTEGER NOT NULL DEFAULT 5 CHECK (
    max_failure_attempts >= 1
  ),
  lease_generation BIGINT NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
  fencing_token TEXT,
  worker_instance TEXT,
  heartbeat_at TIMESTAMPTZ,
  lease_expires_at TIMESTAMPTZ,
  result_json JSONB,
  error_code TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  terminal_at TIMESTAMPTZ,
  UNIQUE (root_operation_id),
  UNIQUE (
    namespace_id, data_mode, namespace_generation, semantic_key
  ),
  UNIQUE (
    journey_id, namespace_id, data_mode, subject_id
  ),
  UNIQUE (
    journey_id, namespace_id, data_mode, namespace_generation,
    run_id, arm_id, subject_id
  ),
  FOREIGN KEY (
    root_operation_id, namespace_id, data_mode, subject_id
  ) REFERENCES sleep_domain_operations (
    operation_id, namespace_id, data_mode, subject_id
  ) ON DELETE RESTRICT,
  FOREIGN KEY (
    namespace_id, namespace_generation, run_id, arm_id
  ) REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT,
  CHECK (deadline_at > created_at),
  CHECK (
    (phase IN ('succeeded', 'blocked', 'reconciliation_required', 'failed')
      AND terminal_at IS NOT NULL)
    OR
    (phase NOT IN ('succeeded', 'blocked', 'reconciliation_required', 'failed')
      AND terminal_at IS NULL)
  ),
  CHECK (
    (phase = 'succeeded' AND result_json IS NOT NULL AND error_code IS NULL)
    OR (phase IN ('blocked', 'reconciliation_required', 'failed')
      AND error_code IS NOT NULL)
    OR (phase NOT IN (
      'succeeded', 'blocked', 'reconciliation_required', 'failed'
    ) AND result_json IS NULL AND error_code IS NULL)
  ),
  CHECK (
    (worker_instance IS NULL AND fencing_token IS NULL
      AND lease_expires_at IS NULL)
    OR (worker_instance IS NOT NULL AND fencing_token IS NOT NULL
      AND lease_generation >= 1 AND lease_expires_at IS NOT NULL)
  ),
  CHECK (jsonb_typeof(COALESCE(result_json, '{}'::jsonb)) = 'object')
);

CREATE INDEX idx_backend_demo_journey_claim
  ON backend_demo_journeys (
    phase, resume_at, created_at, journey_id
  )
  WHERE phase NOT IN (
    'succeeded', 'blocked', 'reconciliation_required', 'failed'
  );

CREATE TABLE backend_demo_journey_checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  journey_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT NOT NULL,
  arm_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  phase TEXT NOT NULL,
  semantic_checkpoint_key TEXT NOT NULL,
  checkpoint_sha256 TEXT NOT NULL CHECK (
    checkpoint_sha256 ~ '^[0-9a-f]{64}$'
  ),
  checkpoint_json JSONB NOT NULL,
  lease_generation BIGINT NOT NULL CHECK (lease_generation >= 1),
  fencing_token TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (journey_id, phase, semantic_checkpoint_key),
  FOREIGN KEY (
    journey_id, namespace_id, data_mode, namespace_generation,
    run_id, arm_id, subject_id
  )
    REFERENCES backend_demo_journeys (
      journey_id, namespace_id, data_mode, namespace_generation,
      run_id, arm_id, subject_id
    ) ON DELETE RESTRICT,
  CHECK (jsonb_typeof(checkpoint_json) = 'object'),
  CHECK (NOT (checkpoint_json ?| ARRAY[
    'oracle', 'oracle_expected', 'expected_outcome', 'gold_answer'
  ]))
);

CREATE TABLE backend_demo_journey_events (
  event_id TEXT PRIMARY KEY,
  journey_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT NOT NULL,
  arm_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  sequence BIGINT GENERATED ALWAYS AS IDENTITY,
  event_type TEXT NOT NULL,
  state TEXT NOT NULL,
  correlation_id TEXT,
  event_json JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (journey_id, sequence),
  FOREIGN KEY (
    journey_id, namespace_id, data_mode, namespace_generation,
    run_id, arm_id, subject_id
  )
    REFERENCES backend_demo_journeys (
      journey_id, namespace_id, data_mode, namespace_generation,
      run_id, arm_id, subject_id
    ) ON DELETE RESTRICT,
  CHECK (jsonb_typeof(event_json) = 'object'),
  CHECK (NOT (event_json ?| ARRAY[
    'oracle', 'oracle_expected', 'expected_outcome', 'gold_answer'
  ]))
);

CREATE TABLE backend_demo_journey_receipts (
  receipt_id TEXT PRIMARY KEY,
  journey_id TEXT NOT NULL UNIQUE,
  root_operation_id TEXT NOT NULL UNIQUE,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT NOT NULL,
  arm_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (
    outcome IN ('succeeded', 'blocked', 'reconciliation_required', 'failed')
  ),
  receipt_sha256 TEXT NOT NULL CHECK (
    receipt_sha256 ~ '^[0-9a-f]{64}$'
  ),
  receipt_json JSONB NOT NULL,
  error_code TEXT,
  committed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (
    journey_id, namespace_id, data_mode, namespace_generation,
    run_id, arm_id, subject_id
  )
    REFERENCES backend_demo_journeys (
      journey_id, namespace_id, data_mode, namespace_generation,
      run_id, arm_id, subject_id
    ) ON DELETE RESTRICT,
  CHECK (jsonb_typeof(receipt_json) = 'object'),
  CHECK (
    (outcome = 'succeeded' AND error_code IS NULL)
    OR (outcome <> 'succeeded' AND error_code IS NOT NULL)
  ),
  CHECK (NOT (receipt_json ?| ARRAY[
    'oracle', 'oracle_expected', 'expected_outcome', 'gold_answer'
  ]))
);

CREATE TABLE backend_replay_ingress_manifests (
  manifest_id TEXT PRIMARY KEY,
  journey_id TEXT NOT NULL UNIQUE,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT NOT NULL,
  arm_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  seed_id TEXT NOT NULL
    REFERENCES backend_demo_seed_allowlist(seed_id) ON DELETE RESTRICT,
  schema_version TEXT NOT NULL CHECK (
    schema_version = 'replay_ingress_manifest.v1'
  ),
  scenario_sha256 TEXT NOT NULL CHECK (
    scenario_sha256 ~ '^[0-9a-f]{64}$'
  ),
  generator_version TEXT NOT NULL,
  adapter_version TEXT NOT NULL,
  component_pins_sha256 TEXT NOT NULL CHECK (
    component_pins_sha256 ~ '^[0-9a-f]{64}$'
  ),
  canonical_sequence_sha256 TEXT NOT NULL CHECK (
    canonical_sequence_sha256 ~ '^[0-9a-f]{64}$'
  ),
  observation_count INTEGER NOT NULL CHECK (observation_count >= 1),
  first_received_at TIMESTAMPTZ NOT NULL,
  last_received_at TIMESTAMPTZ NOT NULL,
  night_count INTEGER NOT NULL CHECK (night_count = 1),
  manifest_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (
    journey_id, namespace_id, data_mode, namespace_generation,
    run_id, arm_id, subject_id
  )
    REFERENCES backend_demo_journeys (
      journey_id, namespace_id, data_mode, namespace_generation,
      run_id, arm_id, subject_id
    ) ON DELETE RESTRICT,
  CHECK (last_received_at >= first_received_at),
  CHECK (jsonb_typeof(manifest_json) = 'object'),
  CHECK (NOT (manifest_json ?| ARRAY[
    'oracle', 'oracle_expected', 'expected_outcome', 'gold_answer'
  ]))
);

CREATE TABLE backend_replay_ingress_batches (
  batch_id TEXT PRIMARY KEY,
  journey_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT NOT NULL,
  arm_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  batch_sequence BIGINT NOT NULL CHECK (batch_sequence >= 1),
  predecessor_batch_id TEXT,
  observation_count INTEGER NOT NULL CHECK (
    observation_count BETWEEN 1 AND 100
  ),
  canonical_payload_sha256 TEXT NOT NULL CHECK (
    canonical_payload_sha256 ~ '^[0-9a-f]{64}$'
  ),
  status TEXT NOT NULL CHECK (
    status IN ('reserved', 'committed', 'failed')
  ),
  receipt_json JSONB,
  committed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (journey_id, batch_sequence),
  UNIQUE (journey_id, canonical_payload_sha256),
  FOREIGN KEY (
    journey_id, namespace_id, data_mode, namespace_generation,
    run_id, arm_id, subject_id
  )
    REFERENCES backend_demo_journeys (
      journey_id, namespace_id, data_mode, namespace_generation,
      run_id, arm_id, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (predecessor_batch_id)
    REFERENCES backend_replay_ingress_batches(batch_id) ON DELETE RESTRICT,
  CHECK (
    (batch_sequence = 1 AND predecessor_batch_id IS NULL)
    OR (batch_sequence > 1 AND predecessor_batch_id IS NOT NULL)
  ),
  CHECK (
    (status = 'committed' AND committed_at IS NOT NULL
      AND receipt_json IS NOT NULL)
    OR (status <> 'committed' AND committed_at IS NULL)
  )
);

ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN replay_journey_id TEXT;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN replay_manifest_sha256 TEXT;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN ingress_stream_key TEXT;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN stream_sequence BIGINT;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN predecessor_raw_ingress_record_id TEXT;

ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_replay_order_v1 CHECK (
    replay_journey_id IS NULL OR (
      data_mode = 'replay'
      AND replay_manifest_sha256 ~ '^[0-9a-f]{64}$'
      AND ingress_stream_key IS NOT NULL AND ingress_stream_key <> ''
      AND stream_sequence >= 1
      AND (
        (stream_sequence = 1
          AND predecessor_raw_ingress_record_id IS NULL)
        OR (stream_sequence > 1
          AND predecessor_raw_ingress_record_id IS NOT NULL)
      )
    )
  ) NOT VALID;
ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_replay_journey_fk
  FOREIGN KEY (
    replay_journey_id, namespace_id, data_mode, subject_id
  ) REFERENCES backend_demo_journeys (
    journey_id, namespace_id, data_mode, subject_id
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_replay_predecessor_fk
  FOREIGN KEY (predecessor_raw_ingress_record_id)
  REFERENCES sleep_domain_raw_inbox(raw_ingress_record_id)
  ON DELETE RESTRICT NOT VALID;

CREATE UNIQUE INDEX ux_sleep_domain_raw_replay_stream_sequence
  ON sleep_domain_raw_inbox (
    namespace_id, data_mode, namespace_generation,
    ingress_stream_key, stream_sequence
  )
  WHERE replay_journey_id IS NOT NULL;

ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN replay_journey_id TEXT;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN ingress_stream_key TEXT;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN stream_sequence BIGINT;
ALTER TABLE sleep_domain_normalization_work
  ADD COLUMN predecessor_work_id TEXT;

ALTER TABLE sleep_domain_normalization_work
  ADD CONSTRAINT sleep_domain_normalization_replay_order_v1 CHECK (
    replay_journey_id IS NULL OR (
      data_mode = 'replay'
      AND ingress_stream_key IS NOT NULL AND ingress_stream_key <> ''
      AND stream_sequence >= 1
      AND (
        (stream_sequence = 1 AND predecessor_work_id IS NULL)
        OR (stream_sequence > 1 AND predecessor_work_id IS NOT NULL)
      )
    )
  ) NOT VALID;
ALTER TABLE sleep_domain_normalization_work
  ADD CONSTRAINT sleep_domain_normalization_replay_journey_fk
  FOREIGN KEY (
    replay_journey_id, namespace_id, data_mode, subject_id
  ) REFERENCES backend_demo_journeys (
    journey_id, namespace_id, data_mode, subject_id
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_normalization_work
  ADD CONSTRAINT sleep_domain_normalization_predecessor_fk
  FOREIGN KEY (predecessor_work_id)
  REFERENCES sleep_domain_normalization_work(work_id)
  ON DELETE RESTRICT NOT VALID;

CREATE UNIQUE INDEX ux_sleep_domain_normalization_replay_stream_sequence
  ON sleep_domain_normalization_work (
    namespace_id, data_mode, namespace_generation,
    ingress_stream_key, stream_sequence
  )
  WHERE replay_journey_id IS NOT NULL;

ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN public_schema_version TEXT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN public_today_json JSONB;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN public_projection_sha256 TEXT;
ALTER TABLE sleep_domain_analysis_role_views
  ADD COLUMN public_committed_at TIMESTAMPTZ;

ALTER TABLE sleep_domain_analysis_role_views
  ADD CONSTRAINT sleep_domain_public_today_v1_contract CHECK (
    (
      public_schema_version IS NULL
      AND public_today_json IS NULL
      AND public_projection_sha256 IS NULL
      AND public_committed_at IS NULL
    )
    OR (
      public_schema_version = 'product_sleep_today.v1'
      AND jsonb_typeof(public_today_json) = 'object'
      AND public_today_json ->> 'role' = role
      AND public_today_json ->> 'analysis_revision_id' =
        analysis_revision_id
      AND public_projection_sha256 ~ '^[0-9a-f]{64}$'
      AND public_committed_at IS NOT NULL
    )
  ) NOT VALID;

CREATE INDEX idx_sleep_domain_public_today_current
  ON sleep_domain_analysis_role_views (
    namespace_id, data_mode, namespace_generation, subject_id, role,
    public_committed_at DESC, role_view_id DESC
  )
  WHERE public_schema_version = 'product_sleep_today.v1';

CREATE OR REPLACE FUNCTION sleepagent_reject_demo_append_only_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER backend_demo_journey_checkpoint_immutable
BEFORE UPDATE OR DELETE ON backend_demo_journey_checkpoints
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_demo_append_only_mutation();

CREATE TRIGGER backend_demo_journey_event_immutable
BEFORE UPDATE OR DELETE ON backend_demo_journey_events
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_demo_append_only_mutation();

CREATE TRIGGER backend_demo_journey_receipt_immutable
BEFORE UPDATE OR DELETE ON backend_demo_journey_receipts
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_demo_append_only_mutation();

CREATE TRIGGER backend_replay_ingress_manifest_immutable
BEFORE UPDATE OR DELETE ON backend_replay_ingress_manifests
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_demo_append_only_mutation();

ALTER TABLE sleep_domain_analysis_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_analysis_revisions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_analysis_revision_scope
  ON sleep_domain_analysis_revisions;
CREATE POLICY sleep_domain_analysis_revision_scope
  ON sleep_domain_analysis_revisions
  USING (sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ))
  WITH CHECK (
    sleepagent_scope_setting('sleepagent.process_role') = 'worker'
    AND sleepagent_subject_scope_allows(
      namespace_id, data_mode, subject_id
    )
  );

ALTER TABLE sleep_domain_current_quality ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_current_quality FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_current_quality_scope
  ON sleep_domain_current_quality;
CREATE POLICY sleep_domain_current_quality_scope
  ON sleep_domain_current_quality
  USING (sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ))
  WITH CHECK (
    sleepagent_scope_setting('sleepagent.process_role') = 'worker'
    AND sleepagent_subject_scope_allows(
      namespace_id, data_mode, subject_id
    )
  );

ALTER TABLE sleep_domain_current_risk ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_current_risk FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_current_risk_scope
  ON sleep_domain_current_risk;
CREATE POLICY sleep_domain_current_risk_scope
  ON sleep_domain_current_risk
  USING (sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ))
  WITH CHECK (
    sleepagent_scope_setting('sleepagent.process_role') = 'worker'
    AND sleepagent_subject_scope_allows(
      namespace_id, data_mode, subject_id
    )
  );

ALTER TABLE sleep_domain_episode_observation_memberships
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_episode_observation_memberships
  FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_episode_observation_membership_scope
  ON sleep_domain_episode_observation_memberships;
CREATE POLICY sleep_domain_episode_observation_membership_scope
  ON sleep_domain_episode_observation_memberships
  USING (sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ))
  WITH CHECK (
    sleepagent_scope_setting('sleepagent.process_role') = 'worker'
    AND sleepagent_subject_scope_allows(
      namespace_id, data_mode, subject_id
    )
  );

ALTER TABLE backend_demo_journeys ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_demo_journeys FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_demo_journey_scope ON backend_demo_journeys
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_demo_journey_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_demo_journey_checkpoints FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_demo_journey_checkpoint_scope
  ON backend_demo_journey_checkpoints
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_demo_journey_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_demo_journey_events FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_demo_journey_event_scope
  ON backend_demo_journey_events
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_demo_journey_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_demo_journey_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_demo_journey_receipt_scope
  ON backend_demo_journey_receipts
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_replay_ingress_manifests ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_replay_ingress_manifests FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_replay_ingress_manifest_scope
  ON backend_replay_ingress_manifests
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_replay_ingress_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_replay_ingress_batches FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_replay_ingress_batch_scope
  ON backend_replay_ingress_batches
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

CREATE OR REPLACE FUNCTION sleepagent_demo_workload_allows()
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT
    NULLIF(current_setting('sleepagent.process_role', TRUE), '') = 'api'
    AND NULLIF(current_setting('sleepagent.data_mode', TRUE), '') = 'replay'
    AND EXISTS (
      SELECT 1
      FROM public.backend_service_principals AS principal
      WHERE principal.principal_id = NULLIF(
          current_setting('sleepagent.service_principal_id', TRUE), ''
        )
        AND principal.database_role_name::text = session_user::text
        AND principal.principal_kind = 'demo_controller'
        AND principal.status = 'active'
    )
$$;

REVOKE ALL ON FUNCTION sleepagent_demo_workload_allows() FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_reserve_demo_journey(
  requested_seed_id TEXT,
  requested_caller_idempotency_key TEXT,
  requested_sha256 TEXT,
  requested_batch_size INTEGER,
  new_root_operation_id TEXT,
  new_journey_id TEXT,
  new_command_receipt_id TEXT,
  new_event_id TEXT
)
RETURNS TABLE (
  root_operation_id TEXT,
  journey_id TEXT,
  namespace_id TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT,
  phase TEXT,
  reused BOOLEAN
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  seed_row public.backend_demo_seed_allowlist%ROWTYPE;
  receipt_row public.backend_command_receipts%ROWTYPE;
  journey_row public.backend_demo_journeys%ROWTYPE;
  principal TEXT := NULLIF(
    current_setting('sleepagent.service_principal_id', TRUE), ''
  );
  metadata JSONB;
  namespace_value TEXT;
  subject_value TEXT;
  run_value TEXT;
  arm_value TEXT;
  generation_value BIGINT;
  semantic_value TEXT;
  workload_snapshot JSONB;
BEGIN
  IF NOT public.sleepagent_demo_workload_allows()
     OR requested_seed_id IS NULL OR requested_seed_id = ''
     OR requested_caller_idempotency_key IS NULL
     OR requested_caller_idempotency_key = ''
     OR octet_length(requested_caller_idempotency_key) > 200
     OR requested_sha256 !~ '^[0-9a-f]{64}$'
     OR requested_batch_size < 1 OR requested_batch_size > 100
     OR new_root_operation_id !~
       '^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
     OR new_journey_id IS NULL OR new_journey_id = ''
     OR new_command_receipt_id IS NULL OR new_command_receipt_id = ''
     OR new_event_id IS NULL OR new_event_id = '' THEN
    RAISE EXCEPTION 'invalid_request';
  END IF;

  SELECT seed.*
  INTO seed_row
  FROM public.backend_demo_seed_allowlist AS seed
  WHERE seed.seed_id = requested_seed_id
    AND seed.active
    AND seed.artifact_family IS NOT NULL
  FOR SHARE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'scenario_not_found';
  END IF;

  metadata := seed_row.metadata_json;
  IF seed_row.schema_version <> 'canonical_replay_scenario.v1'
     OR NOT (
       (seed_row.adapter_version = 'replay_external_fact_adapter.v1'
         AND seed_row.manifest_schema_version = 'replay_ingress_manifest.v1'
         AND seed_row.night_count = 1)
       OR
       (seed_row.adapter_version = 'replay_external_fact_adapter.v2'
         AND seed_row.manifest_schema_version = 'replay_ingress_manifest.v2'
         AND seed_row.night_count BETWEEN 1 AND 15)
     )
     OR seed_row.expected_observation_count < 1
     OR metadata IS NULL
     OR NOT (metadata ?& ARRAY[
       'namespace_id', 'subject_id', 'timezone_name',
       'scenario_clock_start', 'model_version', 'policy_sha256',
       'schema_manifest_sha256'
     ]) THEN
    RAISE EXCEPTION 'scenario_contract_invalid';
  END IF;

  namespace_value := metadata ->> 'namespace_id';
  subject_value := metadata ->> 'subject_id';
  IF namespace_value !~ '^replay:[a-z0-9][a-z0-9._:-]{0,127}$'
     OR subject_value IS NULL OR subject_value = ''
     OR metadata ->> 'schema_manifest_sha256' !~ '^[0-9a-f]{64}$'
     OR metadata ->> 'policy_sha256' !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'scenario_contract_invalid';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(namespace_value, 0));

  INSERT INTO public.backend_namespaces (
    namespace_id, data_mode, current_generation, status,
    synthetic_non_release, max_worker_concurrency
  ) VALUES (
    namespace_value, 'replay', 1, 'active', TRUE, 1
  )
  ON CONFLICT ON CONSTRAINT backend_namespaces_pkey DO NOTHING;

  SELECT namespace.current_generation
  INTO generation_value
  FROM public.backend_namespaces AS namespace
  WHERE namespace.namespace_id = namespace_value
    AND namespace.data_mode = 'replay'
    AND namespace.status = 'active'
    AND namespace.synthetic_non_release
  FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'generation_fenced';
  END IF;

  INSERT INTO public.backend_namespace_generations (
    namespace_id, data_mode, generation, status,
    configuration_sha256
  ) VALUES (
    namespace_value, 'replay', generation_value, 'active',
    seed_row.seed_sha256
  )
  ON CONFLICT ON CONSTRAINT backend_namespace_generations_pkey DO NOTHING;

  IF NOT EXISTS (
    SELECT 1
    FROM public.backend_namespace_generations AS generation
    WHERE generation.namespace_id = namespace_value
      AND generation.data_mode = 'replay'
      AND generation.generation = generation_value
      AND generation.status = 'active'
      AND generation.configuration_sha256 = seed_row.seed_sha256
  ) THEN
    RAISE EXCEPTION 'generation_fenced';
  END IF;

  run_value := 'replay-run:' || substring(seed_row.seed_sha256, 1, 24)
    || ':g' || generation_value::text;
  arm_value := 'replay-arm:' || substring(seed_row.seed_sha256, 1, 24)
    || ':g' || generation_value::text;

  INSERT INTO public.backend_replay_runs (
    run_id, namespace_id, data_mode, namespace_generation,
    scenario_id, scenario_sha256, generation, status,
    synthetic_non_release
  ) VALUES (
    run_value, namespace_value, 'replay', generation_value,
    seed_row.scenario_id, seed_row.seed_sha256,
    generation_value::INTEGER, 'active', TRUE
  )
  ON CONFLICT ON CONSTRAINT backend_replay_runs_pkey DO NOTHING;

  IF NOT EXISTS (
    SELECT 1
    FROM public.backend_replay_runs AS run
    WHERE run.run_id = run_value
      AND run.namespace_id = namespace_value
      AND run.data_mode = 'replay'
      AND run.namespace_generation = generation_value
      AND run.scenario_id = seed_row.scenario_id
      AND run.scenario_sha256 = seed_row.seed_sha256
      AND run.generation = generation_value::INTEGER
      AND run.status = 'active'
      AND run.synthetic_non_release
  ) THEN
    RAISE EXCEPTION 'generation_fenced';
  END IF;

  INSERT INTO public.backend_replay_arms (
    arm_id, run_id, namespace_id, data_mode, namespace_generation,
    arm_name, configuration_sha256
  ) VALUES (
    arm_value, run_value, namespace_value, 'replay', generation_value,
    'control', seed_row.component_pins_sha256
  )
  ON CONFLICT ON CONSTRAINT backend_replay_arms_pkey DO NOTHING;

  IF NOT EXISTS (
    SELECT 1
    FROM public.backend_replay_arms AS arm
    WHERE arm.arm_id = arm_value
      AND arm.run_id = run_value
      AND arm.namespace_id = namespace_value
      AND arm.data_mode = 'replay'
      AND arm.namespace_generation = generation_value
      AND arm.arm_name = 'control'
      AND arm.configuration_sha256 = seed_row.component_pins_sha256
  ) THEN
    RAISE EXCEPTION 'generation_fenced';
  END IF;

  INSERT INTO public.backend_subjects (
    namespace_id, data_mode, subject_id, timezone_name, status
  ) VALUES (
    namespace_value, 'replay', subject_value,
    metadata ->> 'timezone_name', 'active'
  )
  ON CONFLICT ON CONSTRAINT backend_subjects_pkey DO NOTHING;

  IF NOT EXISTS (
    SELECT 1
    FROM public.backend_subjects AS subject
    WHERE subject.namespace_id = namespace_value
      AND subject.data_mode = 'replay'
      AND subject.subject_id = subject_value
      AND subject.timezone_name = metadata ->> 'timezone_name'
      AND subject.status = 'active'
  ) THEN
    RAISE EXCEPTION 'generation_fenced';
  END IF;

  INSERT INTO public.backend_subject_epochs (
    namespace_id, data_mode, subject_id, authorization_epoch,
    privacy_epoch, retrieval_policy_epoch
  ) VALUES (
    namespace_value, 'replay', subject_value, 1, 1, 1
  )
  ON CONFLICT ON CONSTRAINT backend_subject_epochs_pkey DO NOTHING;

  IF NOT EXISTS (
    SELECT 1
    FROM public.backend_subject_epochs AS epoch
    WHERE epoch.namespace_id = namespace_value
      AND epoch.data_mode = 'replay'
      AND epoch.subject_id = subject_value
      AND epoch.authorization_epoch = 1
      AND epoch.privacy_epoch = 1
      AND epoch.retrieval_policy_epoch = 1
  ) THEN
    RAISE EXCEPTION 'generation_fenced';
  END IF;

  INSERT INTO public.backend_principal_grants (
    grant_id, principal_id, namespace_id, data_mode, purpose,
    scopes_json, allowed_handlers_json, authorization_epoch,
    status, valid_from
  ) VALUES (
    'grant:' || encode(digest(
      principal || E'\x1f' || namespace_value || E'\x1fdemo_control',
      'sha256'
    ), 'hex'),
    principal, namespace_value, 'replay', 'demo_control',
    '["demo:seed","demo:advance","demo:operation:read","demo:trace:read",'
      '"demo:clock:read"]'::jsonb,
    '[]'::jsonb, 1, 'active', clock_timestamp()
  )
  ON CONFLICT ON CONSTRAINT
    backend_principal_grants_principal_id_namespace_id_data_mod_key
  DO UPDATE SET status = 'active', authorization_epoch = 1,
    valid_until = NULL, updated_at = clock_timestamp();

  INSERT INTO public.backend_principal_grants (
    grant_id, principal_id, namespace_id, data_mode, purpose,
    scopes_json, allowed_handlers_json, authorization_epoch,
    status, valid_from
  )
  SELECT
    'grant:' || encode(digest(
      worker.principal_id || E'\x1f' || namespace_value
        || E'\x1fworker',
      'sha256'
    ), 'hex'),
    worker.principal_id, namespace_value, 'replay', 'worker',
    '["replay:process"]'::jsonb,
    '["replay_journey","normalization","fast_path","product_agent",'
      '"sleep_command","product_interaction","demo_advance",'
      '"induction","reconciliation","retention",'
      '"deterministic_replay_sink"]'::jsonb,
    1, 'active', clock_timestamp()
  FROM public.backend_service_principals AS worker
  WHERE worker.principal_kind = 'worker'
    AND worker.status = 'active'
  ON CONFLICT ON CONSTRAINT
    backend_principal_grants_principal_id_namespace_id_data_mod_key
  DO UPDATE SET status = 'active', authorization_epoch = 1,
    valid_until = NULL,
    allowed_handlers_json =
      backend_principal_grants.allowed_handlers_json
      || EXCLUDED.allowed_handlers_json,
    updated_at = clock_timestamp();

  workload_snapshot := jsonb_build_object(
    'authorization_epoch', 1,
    'privacy_epoch', 1,
    'retrieval_policy_epoch', 1,
    'principal_id', principal,
    'purpose', 'demo_control'
  );
  semantic_value := encode(digest(concat_ws(
    E'\x1f', generation_value::text, seed_row.seed_sha256,
    seed_row.manifest_sha256, seed_row.generator_version,
    seed_row.adapter_version, metadata ->> 'model_version',
    metadata ->> 'policy_sha256',
    metadata ->> 'schema_manifest_sha256'
  ), 'sha256'), 'hex');

  SELECT receipt.*
  INTO receipt_row
  FROM public.backend_command_receipts AS receipt
  WHERE receipt.service_principal_id = principal
    AND receipt.actor_id IS NULL
    AND receipt.route_template = '/demo/v1/seed'
    AND receipt.caller_idempotency_key = requested_caller_idempotency_key
  FOR UPDATE;
  IF FOUND THEN
    IF receipt_row.request_sha256 <> requested_sha256 THEN
      RAISE EXCEPTION 'idempotency_conflict';
    END IF;
    SELECT journey.*
    INTO STRICT journey_row
    FROM public.backend_demo_journeys AS journey
    WHERE journey.root_operation_id = receipt_row.operation_id;
    RETURN QUERY SELECT
      journey_row.root_operation_id, journey_row.journey_id,
      journey_row.namespace_id, journey_row.namespace_generation,
      journey_row.run_id, journey_row.arm_id, journey_row.subject_id,
      journey_row.phase, TRUE;
    RETURN;
  END IF;

  SELECT journey.*
  INTO journey_row
  FROM public.backend_demo_journeys AS journey
  WHERE journey.namespace_id = namespace_value
    AND journey.data_mode = 'replay'
    AND journey.namespace_generation = generation_value
    AND journey.semantic_key = semantic_value
  FOR UPDATE;

  IF NOT FOUND THEN
    INSERT INTO public.sleep_domain_operations (
      operation_id, namespace_id, data_mode, operation_type,
      subject_id, service_principal_id, actor_id,
      target_resource_id, target_resource_key, idempotency_key,
      request_sha256, status, operation_json, created_at, updated_at,
      protocol_version, namespace_generation, run_id, arm_id,
      id_scheme, origin_kind, semantic_key, queue_name, priority,
      available_at, max_attempts, workload_authorization_snapshot_json,
      policy_sha256
    ) VALUES (
      new_root_operation_id, namespace_value, 'replay', 'replay_journey',
      subject_value, principal, NULL,
      seed_row.seed_id, seed_row.seed_id, semantic_value,
      requested_sha256, 'pending', jsonb_build_object(
        'schema_version', 'demo_root_operation.v1',
        'seed_id', seed_row.seed_id,
        'scenario_id', seed_row.scenario_id,
        'manifest_sha256', seed_row.manifest_sha256,
        'batch_size', requested_batch_size,
        'data_mode', 'replay',
        'synthetic_non_release', TRUE
      ), clock_timestamp(), clock_timestamp(),
      2, generation_value, run_value, arm_value,
      'uuidv7', 'system', semantic_value, 'replay_journey', 100,
      clock_timestamp(), 5, workload_snapshot,
      metadata ->> 'policy_sha256'
    );

    INSERT INTO public.backend_demo_journeys (
      journey_id, root_operation_id, namespace_id, data_mode,
      namespace_generation, run_id, arm_id, subject_id, seed_id,
      semantic_key, scenario_sha256, manifest_sha256,
      generator_version, adapter_version, model_version,
      policy_sha256, schema_manifest_sha256, phase,
      resume_at, deadline_at
    ) VALUES (
      new_journey_id, new_root_operation_id, namespace_value, 'replay',
      generation_value, run_value, arm_value, subject_value,
      seed_row.seed_id, semantic_value, seed_row.seed_sha256,
      seed_row.manifest_sha256, seed_row.generator_version,
      seed_row.adapter_version, metadata ->> 'model_version',
      metadata ->> 'policy_sha256',
      metadata ->> 'schema_manifest_sha256', 'accepted',
      clock_timestamp(), clock_timestamp() + interval '30 minutes'
    )
    RETURNING * INTO journey_row;

    INSERT INTO public.backend_demo_journey_events (
      event_id, journey_id, namespace_id, data_mode,
      namespace_generation, run_id, arm_id, subject_id,
      event_type, state, event_json
    ) VALUES (
      new_event_id, journey_row.journey_id, namespace_value, 'replay',
      generation_value, run_value, arm_value, subject_value,
      'journey.accepted', 'accepted', jsonb_build_object(
        'schema_version', 'demo_journey_event.v1',
        'root_operation_id', journey_row.root_operation_id,
        'manifest_sha256', seed_row.manifest_sha256
      )
    );

    INSERT INTO public.sleep_domain_domain_outbox (
      event_id, namespace_id, data_mode, event_type,
      aggregate_type, aggregate_id, aggregate_version,
      per_aggregate_sequence, subject_id, operation_id,
      status, available_at, event_json, created_at,
      protocol_version, namespace_generation, run_id, arm_id
    ) VALUES (
      'outbox:' || encode(digest(
        new_event_id || E'\x1fjourney.accepted', 'sha256'
      ), 'hex'),
      namespace_value, 'replay', 'DEMO_JOURNEY_ACCEPTED',
      'ReplayJourney', journey_row.journey_id, 1, 1,
      subject_value, journey_row.root_operation_id,
      'committed', clock_timestamp(), jsonb_build_object(
        'schema_version', 'committed_event.v2',
        'event_type', 'DEMO_JOURNEY_ACCEPTED',
        'root_operation_id', journey_row.root_operation_id,
        'journey_id', journey_row.journey_id,
        'seed_id', seed_row.seed_id,
        'data_mode', 'replay',
        'synthetic_non_release', TRUE
      ), clock_timestamp(), 2, generation_value, run_value, arm_value
    );
  END IF;

  INSERT INTO public.backend_command_receipts (
    command_receipt_id, namespace_id, data_mode, namespace_generation,
    run_id, arm_id, service_principal_id, actor_id, subject_id,
    route_template, caller_idempotency_key, request_sha256,
    operation_id, authorization_snapshot_json, receipt_json,
    origin_kind, workload_authorization_snapshot_json
  ) VALUES (
    new_command_receipt_id, namespace_value, 'replay', generation_value,
    run_value, arm_value, principal, NULL, subject_value,
    '/demo/v1/seed', requested_caller_idempotency_key,
    requested_sha256, journey_row.root_operation_id, NULL,
    jsonb_build_object(
      'schema_version', 'demo_seed_receipt.v1',
      'root_operation_id', journey_row.root_operation_id,
      'journey_id', journey_row.journey_id,
      'generation', generation_value,
      'data_mode', 'replay',
      'synthetic_non_release', TRUE
    ), 'system', workload_snapshot
  );

  INSERT INTO public.backend_authorization_audit (
    audit_id, namespace_id, data_mode, subject_id,
    principal_id, actor_id, binding_id, decision,
    reason_code, policy_sha256, authorization_epoch,
    privacy_epoch, retrieval_policy_epoch, audit_json, occurred_at
  ) VALUES (
    'audit:' || encode(digest(
      new_command_receipt_id || E'\x1fdemo_seed_reserved', 'sha256'
    ), 'hex'),
    namespace_value, 'replay', subject_value,
    principal, NULL, NULL, 'allow', 'demo_seed_reserved',
    metadata ->> 'policy_sha256', 1, 1, 1,
    jsonb_build_object(
      'schema_version', 'demo_authorization_audit.v1',
      'route', '/demo/v1/seed',
      'root_operation_id', journey_row.root_operation_id,
      'journey_id', journey_row.journey_id,
      'command_receipt_id', new_command_receipt_id,
      'reused', journey_row.root_operation_id <> new_root_operation_id,
      'data_mode', 'replay',
      'synthetic_non_release', TRUE
    ), clock_timestamp()
  );

  RETURN QUERY SELECT
    journey_row.root_operation_id, journey_row.journey_id,
    journey_row.namespace_id, journey_row.namespace_generation,
    journey_row.run_id, journey_row.arm_id, journey_row.subject_id,
    journey_row.phase,
    journey_row.root_operation_id <> new_root_operation_id;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_reserve_demo_journey(
  TEXT, TEXT, TEXT, INTEGER, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_get_demo_operation(
  target_root_operation_id TEXT
)
RETURNS TABLE (
  operation_id TEXT,
  namespace_generation BIGINT,
  phase TEXT,
  result_json JSONB,
  error_code TEXT,
  updated_at TIMESTAMPTZ
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  match_count INTEGER;
BEGIN
  IF NOT public.sleepagent_demo_workload_allows()
     OR target_root_operation_id IS NULL
     OR target_root_operation_id = '' THEN
    RAISE EXCEPTION 'authorization_denied';
  END IF;
  SELECT count(*)
  INTO match_count
  FROM public.backend_demo_journeys AS journey
  JOIN public.sleep_domain_operations AS operation
    ON operation.operation_id = journey.root_operation_id
  WHERE journey.root_operation_id = target_root_operation_id
    AND operation.service_principal_id = NULLIF(
      current_setting('sleepagent.service_principal_id', TRUE), ''
    )
    AND operation.origin_kind = 'system';
  IF match_count = 0 THEN
    RAISE EXCEPTION 'operation_not_found';
  ELSIF match_count <> 1 THEN
    RAISE EXCEPTION 'operation_authority_ambiguous';
  END IF;
  RETURN QUERY
  SELECT journey.root_operation_id, journey.namespace_generation,
    journey.phase, journey.result_json, journey.error_code,
    journey.updated_at
  FROM public.backend_demo_journeys AS journey
  WHERE journey.root_operation_id = target_root_operation_id;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_get_demo_operation(TEXT) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_read_demo_clock()
RETURNS TABLE (
  scenario_now TIMESTAMPTZ,
  namespace_generation BIGINT
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  match_count INTEGER;
BEGIN
  IF NOT public.sleepagent_demo_workload_allows() THEN
    RAISE EXCEPTION 'authorization_denied';
  END IF;
  SELECT count(*)
  INTO match_count
  FROM (
    SELECT DISTINCT journey.namespace_id, journey.namespace_generation
    FROM public.backend_demo_journeys AS journey
    JOIN public.sleep_domain_operations AS operation
      ON operation.operation_id = journey.root_operation_id
    JOIN public.backend_namespaces AS namespace
      ON namespace.namespace_id = journey.namespace_id
     AND namespace.data_mode = journey.data_mode
     AND namespace.current_generation = journey.namespace_generation
    WHERE operation.service_principal_id = NULLIF(
        current_setting('sleepagent.service_principal_id', TRUE), ''
      )
      AND namespace.status = 'active'
  ) AS visible;
  IF match_count = 0 THEN
    RAISE EXCEPTION 'operation_not_found';
  ELSIF match_count <> 1 THEN
    RAISE EXCEPTION 'operation_authority_ambiguous';
  END IF;
  RETURN QUERY
  SELECT COALESCE(
      clock.scenario_now,
      (seed.metadata_json ->> 'scenario_clock_start')::timestamptz
    ),
    journey.namespace_generation
  FROM public.backend_demo_journeys AS journey
  JOIN public.sleep_domain_operations AS operation
    ON operation.operation_id = journey.root_operation_id
  JOIN public.backend_namespaces AS namespace
    ON namespace.namespace_id = journey.namespace_id
   AND namespace.data_mode = journey.data_mode
   AND namespace.current_generation = journey.namespace_generation
  JOIN public.backend_demo_seed_allowlist AS seed
    ON seed.seed_id = journey.seed_id
  LEFT JOIN public.backend_replay_scenario_clocks AS clock
    ON clock.namespace_id = journey.namespace_id
   AND clock.data_mode = journey.data_mode
   AND clock.namespace_generation = journey.namespace_generation
   AND clock.run_id = journey.run_id
   AND clock.arm_id = journey.arm_id
  WHERE operation.service_principal_id = NULLIF(
      current_setting('sleepagent.service_principal_id', TRUE), ''
    )
    AND namespace.status = 'active'
  ORDER BY journey.created_at DESC, journey.journey_id DESC
  LIMIT 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_read_demo_clock() FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_read_demo_trace(
  target_root_operation_id TEXT,
  after_sequence BIGINT,
  requested_limit INTEGER
)
RETURNS TABLE (
  sequence BIGINT,
  event_type TEXT,
  operation_id TEXT,
  state TEXT,
  correlation_id TEXT,
  occurred_at TIMESTAMPTZ,
  root_operation_id TEXT,
  night_episode_revision_id TEXT,
  fast_path_operation_id TEXT,
  product_operation_id TEXT,
  analysis_revision_id TEXT
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF NOT public.sleepagent_demo_workload_allows()
     OR target_root_operation_id IS NULL
     OR target_root_operation_id = ''
     OR after_sequence < 0
     OR requested_limit < 1 OR requested_limit > 201 THEN
    RAISE EXCEPTION 'authorization_denied';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM public.backend_demo_journeys AS journey
    JOIN public.sleep_domain_operations AS operation
      ON operation.operation_id = journey.root_operation_id
    WHERE journey.root_operation_id = target_root_operation_id
      AND operation.service_principal_id = NULLIF(
        current_setting('sleepagent.service_principal_id', TRUE), ''
      )
  ) THEN
    RAISE EXCEPTION 'operation_not_found';
  END IF;
  RETURN QUERY
  SELECT event.sequence, event.event_type,
    COALESCE(event.event_json ->> 'operation_id', target_root_operation_id),
    event.state,
    COALESCE(event.correlation_id, target_root_operation_id),
    event.occurred_at, target_root_operation_id,
    event.event_json ->> 'night_episode_revision_id',
    event.event_json ->> 'fast_path_operation_id',
    event.event_json ->> 'product_operation_id',
    event.event_json ->> 'analysis_revision_id'
  FROM public.backend_demo_journey_events AS event
  JOIN public.backend_demo_journeys AS journey
    ON journey.journey_id = event.journey_id
  WHERE journey.root_operation_id = target_root_operation_id
    AND event.sequence > after_sequence
  ORDER BY event.sequence
  LIMIT requested_limit;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_read_demo_trace(
  TEXT, BIGINT, INTEGER
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_claim_demo_journey(
  claimant_worker_instance TEXT,
  requested_lease_seconds INTEGER
)
RETURNS TABLE (
  journey_id TEXT,
  root_operation_id TEXT,
  namespace_id TEXT,
  data_mode TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT,
  phase TEXT,
  version BIGINT,
  authorization_epoch BIGINT,
  privacy_epoch BIGINT,
  retrieval_policy_epoch BIGINT,
  lease_generation BIGINT,
  fencing_token TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  principal TEXT := NULLIF(
    current_setting('sleepagent.service_principal_id', TRUE), ''
  );
BEGIN
  IF claimant_worker_instance IS NULL OR claimant_worker_instance = ''
     OR requested_lease_seconds < 1 OR requested_lease_seconds > 3600
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <>
       'worker'
     OR NULLIF(current_setting('sleepagent.data_mode', TRUE), '') <> 'replay'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid or untrusted demo journey claim context';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT journey.journey_id
    FROM public.backend_demo_journeys AS journey
    JOIN public.backend_subject_epochs AS epoch_row
      ON epoch_row.namespace_id = journey.namespace_id
     AND epoch_row.data_mode = journey.data_mode
     AND epoch_row.subject_id = journey.subject_id
    WHERE journey.phase NOT IN (
        'succeeded', 'blocked', 'reconciliation_required', 'failed'
      )
      AND journey.resume_at <= clock_timestamp()
      AND (
        journey.worker_instance IS NULL
        OR journey.lease_expires_at <= clock_timestamp()
      )
      AND journey.failure_attempt_count < journey.max_failure_attempts
      AND EXISTS (
        SELECT 1
        FROM public.backend_principal_grants AS grant_row
        WHERE grant_row.principal_id = principal
          AND grant_row.namespace_id = journey.namespace_id
          AND grant_row.data_mode = journey.data_mode
          AND grant_row.status = 'active'
          AND grant_row.authorization_epoch =
            epoch_row.authorization_epoch
          AND grant_row.valid_from <= clock_timestamp()
          AND (
            grant_row.valid_until IS NULL
            OR grant_row.valid_until > clock_timestamp()
          )
          AND grant_row.allowed_handlers_json ? 'replay_journey'
      )
    ORDER BY journey.resume_at, journey.created_at, journey.journey_id
    FOR UPDATE OF journey SKIP LOCKED
    LIMIT 1
  ), claimed AS (
    UPDATE public.backend_demo_journeys AS journey
    SET lease_generation = journey.lease_generation + 1,
        fencing_token = gen_random_uuid()::text,
        worker_instance = claimant_worker_instance,
        heartbeat_at = clock_timestamp(),
        lease_expires_at = clock_timestamp()
          + make_interval(secs => requested_lease_seconds),
        version = journey.version + 1,
        updated_at = clock_timestamp()
    FROM candidate
    WHERE journey.journey_id = candidate.journey_id
    RETURNING journey.*
  ), claim_event AS (
    INSERT INTO public.backend_demo_journey_events (
      event_id, journey_id, namespace_id, data_mode,
      namespace_generation, run_id, arm_id, subject_id,
      event_type, state, correlation_id, event_json
    )
    SELECT
      'event:' || encode(digest(concat_ws(
        E'\x1f', claimed.journey_id, 'claim',
        claimed.lease_generation::text
      ), 'sha256'), 'hex'),
      claimed.journey_id, claimed.namespace_id, claimed.data_mode,
      claimed.namespace_generation, claimed.run_id, claimed.arm_id,
      claimed.subject_id, 'journey.claimed', claimed.phase,
      claimed.root_operation_id, jsonb_build_object(
        'schema_version', 'demo_journey_claim.v1',
        'root_operation_id', claimed.root_operation_id,
        'operation_id', claimed.root_operation_id,
        'lease_generation', claimed.lease_generation,
        'worker_instance', claimed.worker_instance
      )
    FROM claimed
    RETURNING event_id
  )
  SELECT claimed.journey_id, claimed.root_operation_id,
    claimed.namespace_id, claimed.data_mode,
    claimed.namespace_generation, claimed.run_id, claimed.arm_id,
    claimed.subject_id, claimed.phase, claimed.version,
    epoch_row.authorization_epoch, epoch_row.privacy_epoch,
    epoch_row.retrieval_policy_epoch,
    claimed.lease_generation, claimed.fencing_token
  FROM claimed
  JOIN claim_event ON TRUE
  JOIN public.backend_subject_epochs AS epoch_row
    ON epoch_row.namespace_id = claimed.namespace_id
   AND epoch_row.data_mode = claimed.data_mode
   AND epoch_row.subject_id = claimed.subject_id;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_claim_demo_journey(TEXT, INTEGER)
  FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_heartbeat_demo_journey(
  target_journey_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_lease_seconds INTEGER
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_lease_seconds < 1 OR requested_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'invalid demo journey heartbeat duration';
  END IF;
  UPDATE public.backend_demo_journeys
  SET heartbeat_at = clock_timestamp(),
      lease_expires_at = clock_timestamp()
        + make_interval(secs => requested_lease_seconds),
      updated_at = clock_timestamp()
  WHERE journey_id = target_journey_id
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation,
      run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_heartbeat_demo_journey(
  TEXT, BIGINT, TEXT, INTEGER
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_wait_demo_journey(
  target_journey_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_phase TEXT,
  requested_resume_at TIMESTAMPTZ
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  journey_row public.backend_demo_journeys%ROWTYPE;
  checkpoint_payload JSONB;
  checkpoint_hash TEXT;
  changed INTEGER;
BEGIN
  IF requested_phase NOT IN (
      'staging_input', 'waiting_normalization', 'waiting_episode',
      'waiting_fast_path', 'waiting_product', 'verifying_views'
    )
     OR requested_resume_at IS NULL
     OR requested_resume_at <= clock_timestamp() THEN
    RAISE EXCEPTION 'invalid demo journey wait transition';
  END IF;
  SELECT journey.* INTO journey_row
  FROM public.backend_demo_journeys AS journey
  WHERE journey.journey_id = target_journey_id
    AND journey.lease_generation = expected_lease_generation
    AND journey.fencing_token = expected_fencing_token
    AND journey.worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      journey.namespace_id, journey.data_mode, journey.subject_id,
      journey.namespace_generation, journey.run_id, journey.arm_id
    )
    AND journey.lease_expires_at > clock_timestamp()
    AND journey.deadline_at > clock_timestamp()
    AND journey.phase NOT IN (
      'succeeded', 'blocked', 'reconciliation_required', 'failed'
    )
  FOR UPDATE;
  IF NOT FOUND THEN
    RETURN FALSE;
  END IF;

  checkpoint_payload := jsonb_build_object(
    'schema_version', 'demo_journey_checkpoint.v1',
    'checkpoint', 'wait',
    'phase', requested_phase,
    'wait_count', journey_row.wait_count + 1,
    'resume_at', requested_resume_at
  );
  checkpoint_hash := encode(digest(concat_ws(
    E'\x1f', journey_row.journey_id, requested_phase,
    (journey_row.wait_count + 1)::text, checkpoint_payload::text
  ), 'sha256'), 'hex');
  INSERT INTO public.backend_demo_journey_checkpoints (
    checkpoint_id, journey_id, namespace_id, data_mode,
    namespace_generation, run_id, arm_id, subject_id, phase,
    semantic_checkpoint_key, checkpoint_sha256, checkpoint_json,
    lease_generation, fencing_token
  ) VALUES (
    'checkpoint:' || checkpoint_hash, journey_row.journey_id,
    journey_row.namespace_id, journey_row.data_mode,
    journey_row.namespace_generation, journey_row.run_id,
    journey_row.arm_id, journey_row.subject_id, journey_row.phase,
    'wait:' || requested_phase || ':' ||
      (journey_row.wait_count + 1)::text,
    checkpoint_hash, checkpoint_payload,
    journey_row.lease_generation, journey_row.fencing_token
  );
  INSERT INTO public.backend_demo_journey_events (
    event_id, journey_id, namespace_id, data_mode,
    namespace_generation, run_id, arm_id, subject_id,
    event_type, state, event_json
  ) VALUES (
    'event:' || checkpoint_hash, journey_row.journey_id,
    journey_row.namespace_id, journey_row.data_mode,
    journey_row.namespace_generation, journey_row.run_id,
    journey_row.arm_id, journey_row.subject_id,
    'journey.wait', requested_phase, checkpoint_payload
  );

  UPDATE public.backend_demo_journeys
  SET phase = requested_phase,
      resume_at = requested_resume_at,
      wait_count = wait_count + 1,
      worker_instance = NULL,
      fencing_token = NULL,
      lease_expires_at = NULL,
      heartbeat_at = NULL,
      version = version + 1,
      updated_at = clock_timestamp()
  WHERE journey_id = journey_row.journey_id
    AND lease_generation = journey_row.lease_generation
    AND fencing_token = journey_row.fencing_token;
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_wait_demo_journey(
  TEXT, BIGINT, TEXT, TEXT, TIMESTAMPTZ
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_finalize_demo_journey_attempt(
  target_journey_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_status TEXT,
  requested_error_code TEXT,
  requested_resume_at TIMESTAMPTZ
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  journey_row public.backend_demo_journeys%ROWTYPE;
  receipt_payload JSONB;
  checkpoint_payload JSONB;
  checkpoint_hash TEXT;
BEGIN
  IF requested_status NOT IN ('retry', 'failed', 'reconciliation_required')
     OR requested_error_code IS NULL OR requested_error_code = ''
     OR (requested_status = 'retry' AND requested_resume_at IS NULL)
     OR (requested_status <> 'retry' AND requested_resume_at IS NOT NULL) THEN
    RAISE EXCEPTION 'invalid demo journey attempt finalization';
  END IF;
  SELECT journey.*
  INTO journey_row
  FROM public.backend_demo_journeys AS journey
  WHERE journey.journey_id = target_journey_id
    AND journey.lease_generation = expected_lease_generation
    AND journey.fencing_token = expected_fencing_token
    AND journey.worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND journey.lease_expires_at > clock_timestamp()
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      journey.namespace_id, journey.data_mode, journey.subject_id,
      journey.namespace_generation, journey.run_id, journey.arm_id
    )
  FOR UPDATE;
  IF NOT FOUND THEN
    RETURN FALSE;
  END IF;

  IF requested_status = 'retry' THEN
    checkpoint_payload := jsonb_build_object(
      'schema_version', 'demo_journey_checkpoint.v1',
      'checkpoint', 'retry',
      'phase', journey_row.phase,
      'failure_attempt_count', journey_row.failure_attempt_count + 1,
      'error_code', requested_error_code,
      'resume_at', requested_resume_at,
      'root_operation_id', journey_row.root_operation_id,
      'operation_id', journey_row.root_operation_id
    );
    checkpoint_hash := encode(digest(concat_ws(
      E'\x1f', journey_row.journey_id, 'retry',
      (journey_row.failure_attempt_count + 1)::text,
      checkpoint_payload::text
    ), 'sha256'), 'hex');
    INSERT INTO public.backend_demo_journey_checkpoints (
      checkpoint_id, journey_id, namespace_id, data_mode,
      namespace_generation, run_id, arm_id, subject_id, phase,
      semantic_checkpoint_key, checkpoint_sha256, checkpoint_json,
      lease_generation, fencing_token
    ) VALUES (
      'checkpoint:' || checkpoint_hash, journey_row.journey_id,
      journey_row.namespace_id, journey_row.data_mode,
      journey_row.namespace_generation, journey_row.run_id,
      journey_row.arm_id, journey_row.subject_id, journey_row.phase,
      'retry:' || (journey_row.failure_attempt_count + 1)::text,
      checkpoint_hash, checkpoint_payload,
      journey_row.lease_generation, journey_row.fencing_token
    );
    INSERT INTO public.backend_demo_journey_events (
      event_id, journey_id, namespace_id, data_mode,
      namespace_generation, run_id, arm_id, subject_id,
      event_type, state, correlation_id, event_json
    ) VALUES (
      'event:' || checkpoint_hash, journey_row.journey_id,
      journey_row.namespace_id, journey_row.data_mode,
      journey_row.namespace_generation, journey_row.run_id,
      journey_row.arm_id, journey_row.subject_id,
      'journey.retry', journey_row.phase, journey_row.root_operation_id,
      checkpoint_payload
    );
    UPDATE public.backend_demo_journeys
    SET failure_attempt_count = failure_attempt_count + 1,
        resume_at = GREATEST(requested_resume_at, clock_timestamp()),
        worker_instance = NULL, fencing_token = NULL,
        lease_expires_at = NULL, heartbeat_at = NULL,
        version = version + 1, updated_at = clock_timestamp()
    WHERE journey_id = target_journey_id
      AND lease_generation = journey_row.lease_generation
      AND fencing_token = journey_row.fencing_token;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'journey_fence_lost';
    END IF;
    RETURN TRUE;
  END IF;

  receipt_payload := jsonb_build_object(
    'schema_version', 'demo_journey_terminal_receipt.v1',
    'journey_id', journey_row.journey_id,
    'root_operation_id', journey_row.root_operation_id,
    'outcome', requested_status,
    'error_code', requested_error_code,
    'manifest_sha256', journey_row.manifest_sha256,
    'data_mode', 'replay',
    'synthetic_non_release', TRUE
  );
  INSERT INTO public.backend_demo_journey_receipts (
    receipt_id, journey_id, root_operation_id, namespace_id, data_mode,
    namespace_generation, run_id, arm_id, subject_id, outcome,
    receipt_sha256, receipt_json, error_code
  ) VALUES (
    gen_random_uuid()::text, journey_row.journey_id,
    journey_row.root_operation_id, journey_row.namespace_id, 'replay',
    journey_row.namespace_generation, journey_row.run_id,
    journey_row.arm_id, journey_row.subject_id, requested_status,
    encode(digest(receipt_payload::text, 'sha256'), 'hex'),
    receipt_payload, requested_error_code
  );

  UPDATE public.backend_demo_journeys
  SET phase = requested_status,
      failure_attempt_count = failure_attempt_count + 1,
      error_code = requested_error_code,
      terminal_at = clock_timestamp(),
      worker_instance = NULL, fencing_token = NULL,
      lease_expires_at = NULL, heartbeat_at = NULL,
      version = version + 1, updated_at = clock_timestamp()
  WHERE journey_id = target_journey_id
    AND lease_generation = journey_row.lease_generation
    AND fencing_token = journey_row.fencing_token;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'journey_fence_lost';
  END IF;

  UPDATE public.sleep_domain_operations
  SET status = CASE
        WHEN requested_status = 'reconciliation_required'
          THEN 'outcome_unknown'
        ELSE 'failed'
      END,
      outcome_class = requested_status,
      operation_json = operation_json || jsonb_build_object(
        'error_code', requested_error_code,
        'terminal_at', clock_timestamp()
      ),
      cas_version = cas_version + 1,
      lease_owner = NULL, lease_expires_at = NULL,
      fencing_token = NULL, worker_instance = NULL,
      updated_at = clock_timestamp()
  WHERE operation_id = journey_row.root_operation_id
    AND status NOT IN ('succeeded', 'failed', 'outcome_unknown');
  IF NOT FOUND THEN
    RAISE EXCEPTION 'root_operation_terminal_conflict';
  END IF;

  INSERT INTO public.backend_demo_journey_events (
    event_id, journey_id, namespace_id, data_mode,
    namespace_generation, run_id, arm_id, subject_id,
    event_type, state, correlation_id, event_json
  ) VALUES (
    gen_random_uuid()::text, journey_row.journey_id,
    journey_row.namespace_id, 'replay', journey_row.namespace_generation,
    journey_row.run_id, journey_row.arm_id, journey_row.subject_id,
    'journey.terminal', requested_status, journey_row.root_operation_id,
    receipt_payload
  );

  INSERT INTO public.sleep_domain_domain_outbox (
    event_id, namespace_id, data_mode, event_type,
    aggregate_type, aggregate_id, aggregate_version,
    per_aggregate_sequence, subject_id, operation_id,
    status, available_at, event_json, created_at,
    protocol_version, namespace_generation, run_id, arm_id
  ) VALUES (
    'outbox:' || encode(digest(
      journey_row.journey_id || E'\x1fjourney.terminal', 'sha256'
    ), 'hex'),
    journey_row.namespace_id, 'replay', 'DEMO_JOURNEY_TERMINAL',
    'ReplayJourney', journey_row.journey_id, journey_row.version + 1, 2,
    journey_row.subject_id, journey_row.root_operation_id,
    'committed', clock_timestamp(), receipt_payload, clock_timestamp(),
    2, journey_row.namespace_generation, journey_row.run_id,
    journey_row.arm_id
  );
  INSERT INTO public.backend_authorization_audit (
    audit_id, namespace_id, data_mode, subject_id,
    principal_id, actor_id, binding_id, decision,
    reason_code, policy_sha256, authorization_epoch,
    privacy_epoch, retrieval_policy_epoch, audit_json, occurred_at
  ) VALUES (
    'audit:' || encode(digest(
      journey_row.root_operation_id || E'\x1fdemo_journey_terminal',
      'sha256'
    ), 'hex'),
    journey_row.namespace_id, 'replay', journey_row.subject_id,
    NULLIF(current_setting('sleepagent.service_principal_id', TRUE), ''),
    NULL, NULL, 'allow', 'demo_journey_terminal',
    journey_row.policy_sha256,
    current_setting('sleepagent.authorization_epoch')::bigint,
    current_setting('sleepagent.privacy_epoch')::bigint,
    current_setting('sleepagent.retrieval_policy_epoch')::bigint,
    receipt_payload, clock_timestamp()
  );
  RETURN TRUE;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_finalize_demo_journey_attempt(
  TEXT, BIGINT, TEXT, TEXT, TEXT, TIMESTAMPTZ
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_succeed_demo_journey(
  target_journey_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_result JSONB
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  journey_row public.backend_demo_journeys%ROWTYPE;
  receipt_payload JSONB;
  match_count INTEGER;
  product_policy_sha256 TEXT;
BEGIN
  IF requested_result IS NULL
     OR jsonb_typeof(requested_result) <> 'object'
     OR NOT (requested_result ?& ARRAY[
       'night_episode_id', 'night_episode_revision_id',
       'fast_path_operation_id', 'product_operation_id',
       'analysis_revision_id', 'role_projection_ids', 'manifest_sha256'
     ])
     OR jsonb_typeof(requested_result -> 'role_projection_ids') <> 'object'
     OR (SELECT COALESCE(
           array_agg(role_name ORDER BY role_name), ARRAY[]::text[]
         )
         FROM jsonb_object_keys(
           requested_result -> 'role_projection_ids'
         ) AS roles(role_name))
       <> ARRAY['doctor', 'elder', 'family']::text[] THEN
    RAISE EXCEPTION 'role_projection_incomplete';
  END IF;

  SELECT journey.*
  INTO journey_row
  FROM public.backend_demo_journeys AS journey
  WHERE journey.journey_id = target_journey_id
    AND journey.phase = 'verifying_views'
    AND journey.lease_generation = expected_lease_generation
    AND journey.fencing_token = expected_fencing_token
    AND journey.worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND journey.lease_expires_at > clock_timestamp()
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      journey.namespace_id, journey.data_mode, journey.subject_id,
      journey.namespace_generation, journey.run_id, journey.arm_id
    )
  FOR UPDATE;
  IF NOT FOUND THEN
    RETURN FALSE;
  END IF;
  IF requested_result ->> 'manifest_sha256' <>
       journey_row.manifest_sha256 THEN
    RAISE EXCEPTION 'scenario_contract_invalid';
  END IF;

  SELECT count(*) INTO match_count
  FROM public.sleep_domain_night_episodes AS episode
  JOIN public.sleep_domain_night_episode_revisions AS revision
    ON revision.night_episode_revision_id = episode.current_revision_id
   AND revision.namespace_id = episode.namespace_id
   AND revision.data_mode = episode.data_mode
  WHERE episode.night_episode_id = requested_result ->> 'night_episode_id'
    AND episode.current_revision_id =
      requested_result ->> 'night_episode_revision_id'
    AND episode.namespace_id = journey_row.namespace_id
    AND episode.data_mode = journey_row.data_mode
    AND episode.namespace_generation = journey_row.namespace_generation
    AND episode.run_id = journey_row.run_id
    AND episode.arm_id = journey_row.arm_id
    AND episode.subject_id = journey_row.subject_id
    AND episode.protocol_version >= 2
    AND episode.date_state = 'finalized'
    AND episode.date_conflict = FALSE
    AND episode.assignment_basis = 'observed_wake'
    AND EXISTS (
      SELECT 1
      FROM public.sleep_domain_episode_observation_memberships AS member
      WHERE member.namespace_id = episode.namespace_id
        AND member.data_mode = episode.data_mode
        AND member.night_episode_id = episode.night_episode_id
    )
    AND (
      SELECT COALESCE(array_agg(member.observation_id
        ORDER BY member.observation_id), ARRAY[]::text[])
      FROM public.sleep_domain_episode_observation_memberships AS member
      WHERE member.namespace_id = episode.namespace_id
        AND member.data_mode = episode.data_mode
        AND member.night_episode_id = episode.night_episode_id
    ) = (
      SELECT COALESCE(array_agg(value ORDER BY value), ARRAY[]::text[])
      FROM jsonb_array_elements_text(
        revision.revision_json -> 'observation_ids'
      ) AS ids(value)
    );
  IF match_count <> 1 THEN
    RAISE EXCEPTION 'episode_not_committed';
  END IF;

  SELECT count(*) INTO match_count
  FROM public.sleep_domain_operations AS operation
  WHERE operation.operation_id =
      requested_result ->> 'fast_path_operation_id'
    AND operation.namespace_id = journey_row.namespace_id
    AND operation.data_mode = journey_row.data_mode
    AND operation.namespace_generation = journey_row.namespace_generation
    AND operation.run_id = journey_row.run_id
    AND operation.arm_id = journey_row.arm_id
    AND operation.subject_id = journey_row.subject_id
    AND operation.operation_type = 'fast_path'
    AND operation.target_resource_key =
      requested_result ->> 'night_episode_revision_id'
    AND operation.status = 'succeeded';
  IF match_count <> 1 THEN
    RAISE EXCEPTION 'episode_not_committed';
  END IF;

  SELECT count(*), max(operation.policy_sha256)
  INTO match_count, product_policy_sha256
  FROM public.sleep_domain_operations AS operation
  WHERE operation.operation_id =
      requested_result ->> 'product_operation_id'
    AND operation.namespace_id = journey_row.namespace_id
    AND operation.data_mode = journey_row.data_mode
    AND operation.namespace_generation = journey_row.namespace_generation
    AND operation.run_id = journey_row.run_id
    AND operation.arm_id = journey_row.arm_id
    AND operation.subject_id = journey_row.subject_id
    AND operation.operation_type = 'product_agent'
    AND operation.target_resource_key =
      requested_result ->> 'night_episode_revision_id'
    AND operation.status = 'succeeded'
    AND operation.operation_json #>> '{result,analysis_revision_id}' =
      requested_result ->> 'analysis_revision_id'
    AND operation.operation_json #>> '{result,night_episode_revision_id}' =
      requested_result ->> 'night_episode_revision_id'
    AND jsonb_array_length(
      operation.operation_json #> '{result,role_view_ids}'
    ) = 3;
  IF match_count <> 1 THEN
    RAISE EXCEPTION 'product_failed';
  END IF;

  SELECT count(*) INTO match_count
  FROM public.sleep_domain_analysis_revisions AS analysis
  WHERE analysis.namespace_id = journey_row.namespace_id
    AND analysis.data_mode = journey_row.data_mode
    AND analysis.subject_id = journey_row.subject_id
    AND analysis.night_episode_id =
      requested_result ->> 'night_episode_id'
    AND analysis.night_episode_revision_id =
      requested_result ->> 'night_episode_revision_id';
  IF match_count <> 1 THEN
    RAISE EXCEPTION 'product_failed';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM public.sleep_domain_analysis_revisions AS analysis
    WHERE analysis.analysis_revision_id =
        requested_result ->> 'analysis_revision_id'
      AND analysis.namespace_id = journey_row.namespace_id
      AND analysis.data_mode = journey_row.data_mode
      AND analysis.subject_id = journey_row.subject_id
      AND analysis.night_episode_id =
        requested_result ->> 'night_episode_id'
      AND analysis.night_episode_revision_id =
        requested_result ->> 'night_episode_revision_id'
  ) THEN
    RAISE EXCEPTION 'product_failed';
  END IF;

  SELECT count(*) INTO match_count
  FROM public.sleep_domain_analysis_role_views AS view
  JOIN public.backend_subject_epochs AS epoch_row
    ON epoch_row.namespace_id = view.namespace_id
   AND epoch_row.data_mode = view.data_mode
   AND epoch_row.subject_id = view.subject_id
  WHERE view.analysis_revision_id =
      requested_result ->> 'analysis_revision_id'
    AND view.namespace_id = journey_row.namespace_id
    AND view.data_mode = journey_row.data_mode
    AND view.namespace_generation = journey_row.namespace_generation
    AND view.run_id = journey_row.run_id
    AND view.arm_id = journey_row.arm_id
    AND view.subject_id = journey_row.subject_id
    AND view.night_episode_id =
      requested_result ->> 'night_episode_id'
    AND view.night_episode_revision_id =
      requested_result ->> 'night_episode_revision_id'
    AND view.role IN ('elder', 'family', 'doctor')
    AND view.role_view_id =
      requested_result -> 'role_projection_ids' ->> view.role
    AND view.status = 'ready'
    AND view.protocol_version >= 2
    AND view.policy_sha256 = product_policy_sha256
    AND view.authorization_epoch = epoch_row.authorization_epoch
    AND view.privacy_epoch = epoch_row.privacy_epoch
    AND view.retrieval_policy_epoch = epoch_row.retrieval_policy_epoch
    AND view.public_schema_version = 'product_sleep_today.v1'
    AND view.public_today_json ->> 'role' = view.role
    AND view.public_today_json ->> 'analysis_revision_id' =
      view.analysis_revision_id
    AND view.public_projection_sha256 ~ '^[0-9a-f]{64}$';
  IF match_count <> 3 THEN
    RAISE EXCEPTION 'role_projection_incomplete';
  END IF;

  receipt_payload := jsonb_build_object(
    'schema_version', 'demo_journey_terminal_receipt.v1',
    'journey_id', journey_row.journey_id,
    'root_operation_id', journey_row.root_operation_id,
    'outcome', 'succeeded',
    'result', requested_result,
    'manifest_sha256', journey_row.manifest_sha256,
    'data_mode', 'replay',
    'synthetic_non_release', TRUE
  );
  INSERT INTO public.backend_demo_journey_receipts (
    receipt_id, journey_id, root_operation_id, namespace_id, data_mode,
    namespace_generation, run_id, arm_id, subject_id, outcome,
    receipt_sha256, receipt_json, error_code
  ) VALUES (
    gen_random_uuid()::text, journey_row.journey_id,
    journey_row.root_operation_id, journey_row.namespace_id, 'replay',
    journey_row.namespace_generation, journey_row.run_id,
    journey_row.arm_id, journey_row.subject_id, 'succeeded',
    encode(digest(receipt_payload::text, 'sha256'), 'hex'),
    receipt_payload, NULL
  );

  UPDATE public.backend_demo_journeys
  SET phase = 'succeeded', result_json = requested_result,
      terminal_at = clock_timestamp(), worker_instance = NULL,
      fencing_token = NULL, lease_expires_at = NULL,
      heartbeat_at = NULL, version = version + 1,
      updated_at = clock_timestamp()
  WHERE journey_id = journey_row.journey_id
    AND lease_generation = journey_row.lease_generation
    AND fencing_token = journey_row.fencing_token;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'journey_fence_lost';
  END IF;

  UPDATE public.sleep_domain_operations
  SET status = 'succeeded', outcome_class = 'succeeded',
      operation_json = operation_json || jsonb_build_object(
        'result', requested_result,
        'terminal_at', clock_timestamp()
      ),
      cas_version = cas_version + 1,
      lease_owner = NULL, lease_expires_at = NULL,
      fencing_token = NULL, worker_instance = NULL,
      heartbeat_at = NULL, updated_at = clock_timestamp()
  WHERE operation_id = journey_row.root_operation_id
    AND status NOT IN ('succeeded', 'failed', 'outcome_unknown');
  IF NOT FOUND THEN
    RAISE EXCEPTION 'root_operation_terminal_conflict';
  END IF;

  INSERT INTO public.backend_demo_journey_events (
    event_id, journey_id, namespace_id, data_mode,
    namespace_generation, run_id, arm_id, subject_id,
    event_type, state, correlation_id, event_json
  ) VALUES (
    gen_random_uuid()::text, journey_row.journey_id,
    journey_row.namespace_id, 'replay', journey_row.namespace_generation,
    journey_row.run_id, journey_row.arm_id, journey_row.subject_id,
    'journey.succeeded', 'succeeded', journey_row.root_operation_id,
    receipt_payload
  );
  INSERT INTO public.sleep_domain_domain_outbox (
    event_id, namespace_id, data_mode, event_type,
    aggregate_type, aggregate_id, aggregate_version,
    per_aggregate_sequence, subject_id, operation_id,
    status, available_at, event_json, created_at,
    protocol_version, namespace_generation, run_id, arm_id
  ) VALUES (
    'outbox:' || encode(digest(
      journey_row.journey_id || E'\x1fjourney.terminal', 'sha256'
    ), 'hex'),
    journey_row.namespace_id, 'replay', 'DEMO_JOURNEY_TERMINAL',
    'ReplayJourney', journey_row.journey_id, journey_row.version + 1, 2,
    journey_row.subject_id, journey_row.root_operation_id,
    'committed', clock_timestamp(), receipt_payload, clock_timestamp(),
    2, journey_row.namespace_generation, journey_row.run_id,
    journey_row.arm_id
  );
  INSERT INTO public.backend_authorization_audit (
    audit_id, namespace_id, data_mode, subject_id,
    principal_id, actor_id, binding_id, decision,
    reason_code, policy_sha256, authorization_epoch,
    privacy_epoch, retrieval_policy_epoch, audit_json, occurred_at
  ) VALUES (
    'audit:' || encode(digest(
      journey_row.root_operation_id || E'\x1fdemo_journey_terminal',
      'sha256'
    ), 'hex'),
    journey_row.namespace_id, 'replay', journey_row.subject_id,
    NULLIF(current_setting('sleepagent.service_principal_id', TRUE), ''),
    NULL, NULL, 'allow', 'demo_journey_terminal',
    journey_row.policy_sha256,
    current_setting('sleepagent.authorization_epoch')::bigint,
    current_setting('sleepagent.privacy_epoch')::bigint,
    current_setting('sleepagent.retrieval_policy_epoch')::bigint,
    receipt_payload, clock_timestamp()
  );
  RETURN TRUE;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_succeed_demo_journey(
  TEXT, BIGINT, TEXT, JSONB
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_bootstrap_demo_journey(
  target_root_operation_id TEXT,
  target_journey_id TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  journey_row public.backend_demo_journeys%ROWTYPE;
  seed_row public.backend_demo_seed_allowlist%ROWTYPE;
  metadata JSONB;
  actor_entry RECORD;
  checkpoint_payload JSONB;
BEGIN
  SELECT journey.*
  INTO journey_row
  FROM public.backend_demo_journeys AS journey
  WHERE journey.journey_id = target_journey_id
    AND journey.root_operation_id = target_root_operation_id
    AND journey.phase = 'accepted'
    AND journey.worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND journey.lease_expires_at > clock_timestamp()
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      journey.namespace_id, journey.data_mode, journey.subject_id,
      journey.namespace_generation, journey.run_id, journey.arm_id
    )
  FOR UPDATE;
  IF NOT FOUND THEN
    RETURN FALSE;
  END IF;

  SELECT seed.* INTO STRICT seed_row
  FROM public.backend_demo_seed_allowlist AS seed
  WHERE seed.seed_id = journey_row.seed_id
    AND seed.active
  FOR SHARE;
  metadata := seed_row.metadata_json;
  IF NOT (metadata ?& ARRAY[
      'provider_id', 'provider_account_id', 'provider_device_id',
      'device_id', 'device_binding_id', 'binding_version',
      'actor_aliases', 'actor_scopes', 'actor_assertion_issuer',
      'actor_verification_key_sha256'
    ])
     OR jsonb_typeof(metadata -> 'actor_aliases') <> 'object'
     OR jsonb_typeof(metadata -> 'actor_scopes') <> 'object'
     OR metadata ->> 'actor_verification_key_sha256'
       !~ '^[0-9a-f]{64}$'
     OR (SELECT COALESCE(
           array_agg(role_name ORDER BY role_name), ARRAY[]::text[]
         )
         FROM jsonb_object_keys(
           metadata -> 'actor_aliases'
         ) AS roles(role_name))
       <> ARRAY['doctor', 'elder', 'family']::text[] THEN
    RAISE EXCEPTION 'scenario_contract_invalid';
  END IF;
  IF metadata -> 'actor_scopes' -> 'elder' <>
       '["product:sleep:today:read","product:sleep:trends:read",'
         '"product:sleep:care:read","product:sleep:records:read",'
         '"product:sleep:interaction:write",'
         '"product:sleep:interaction:answer","product:sleep:care:confirm",'
         '"product:sleep:feedback:write","product:sleep:operation:read",'
         '"sleep:episode:read","sleep:operation:read",'
         '"sleep:monitoring:write","sleep:feedback:self",'
         '"sleep:reanalysis:write"]'::jsonb
     OR metadata -> 'actor_scopes' -> 'family' <>
       '["product:sleep:today:read","product:sleep:trends:read",'
         '"product:sleep:care:read","product:sleep:records:read",'
         '"product:sleep:interaction:write",'
         '"product:sleep:interaction:answer","product:sleep:care:confirm",'
         '"product:sleep:feedback:write","product:sleep:operation:read",'
         '"sleep:operation:read","sleep:monitoring:write",'
         '"sleep:feedback:family","sleep:reanalysis:write"]'::jsonb
     OR metadata -> 'actor_scopes' -> 'doctor' <>
       '["product:sleep:today:read","product:sleep:trends:read",'
         '"product:sleep:care:read","product:sleep:records:read",'
         '"product:sleep:interaction:write",'
         '"product:sleep:interaction:answer",'
         '"product:sleep:operation:read","sleep:operation:read",'
         '"sleep:reanalysis:write"]'::jsonb THEN
    RAISE EXCEPTION 'scenario_contract_invalid';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM public.backend_replay_arms AS other_arm
    WHERE other_arm.namespace_id = journey_row.namespace_id
      AND other_arm.namespace_generation = journey_row.namespace_generation
      AND other_arm.arm_id <> journey_row.arm_id
  ) THEN
    RAISE EXCEPTION 'generation_fenced';
  END IF;

  INSERT INTO public.sleep_domain_provider_accounts (
    namespace_id, data_mode, provider_account_id, provider_id,
    configuration_fingerprint, status, account_metadata_json, created_at
  ) VALUES (
    journey_row.namespace_id, 'replay',
    metadata ->> 'provider_account_id', metadata ->> 'provider_id',
    seed_row.component_pins_sha256, 'active',
    jsonb_build_object(
      'schema_version', 'replay_provider_account.v1',
      'synthetic_non_release', TRUE
    ), clock_timestamp()
  ) ON CONFLICT (namespace_id, data_mode, provider_account_id) DO NOTHING;

  IF NOT EXISTS (
    SELECT 1
    FROM public.sleep_domain_provider_accounts AS account
    WHERE account.namespace_id = journey_row.namespace_id
      AND account.data_mode = 'replay'
      AND account.provider_account_id = metadata ->> 'provider_account_id'
      AND account.provider_id = metadata ->> 'provider_id'
      AND account.configuration_fingerprint = seed_row.component_pins_sha256
      AND account.status = 'active'
      AND account.account_metadata_json = jsonb_build_object(
        'schema_version', 'replay_provider_account.v1',
        'synthetic_non_release', TRUE
      )
  ) THEN
    RAISE EXCEPTION 'scenario_contract_invalid';
  END IF;

  INSERT INTO public.sleep_domain_device_identities (
    namespace_id, data_mode, provider_id, provider_account_id,
    provider_device_key, device_id, provider_device_json, created_at
  ) VALUES (
    journey_row.namespace_id, 'replay', metadata ->> 'provider_id',
    metadata ->> 'provider_account_id', metadata ->> 'provider_device_id',
    metadata ->> 'device_id', jsonb_build_object(
      'schema_version', 'replay_provider_device.v1',
      'provider_device_id', metadata ->> 'provider_device_id',
      'synthetic_non_release', TRUE
    ), clock_timestamp()
  ) ON CONFLICT (
    namespace_id, data_mode, provider_id, provider_account_id,
    provider_device_key
  ) DO NOTHING;

  IF NOT EXISTS (
    SELECT 1
    FROM public.sleep_domain_device_identities AS identity
    WHERE identity.namespace_id = journey_row.namespace_id
      AND identity.data_mode = 'replay'
      AND identity.provider_id = metadata ->> 'provider_id'
      AND identity.provider_account_id = metadata ->> 'provider_account_id'
      AND identity.provider_device_key = metadata ->> 'provider_device_id'
      AND identity.device_id = metadata ->> 'device_id'
      AND identity.provider_device_json = jsonb_build_object(
        'schema_version', 'replay_provider_device.v1',
        'provider_device_id', metadata ->> 'provider_device_id',
        'synthetic_non_release', TRUE
      )
  ) THEN
    RAISE EXCEPTION 'scenario_contract_invalid';
  END IF;

  INSERT INTO public.sleep_domain_device_bindings (
    device_binding_id, namespace_id, data_mode, binding_version,
    device_id, provider_id, provider_account_id, subject_id,
    timezone_name, effective_from, status, binding_json, recorded_at
  ) VALUES (
    metadata ->> 'device_binding_id', journey_row.namespace_id, 'replay',
    (metadata ->> 'binding_version')::integer,
    metadata ->> 'device_id', metadata ->> 'provider_id',
    metadata ->> 'provider_account_id', journey_row.subject_id,
    metadata ->> 'timezone_name',
    (metadata ->> 'scenario_clock_start')::timestamptz,
    'active', jsonb_build_object(
      'schema_version', 'replay_device_binding.v1',
      'provider_device_id', metadata ->> 'provider_device_id',
      'synthetic_non_release', TRUE
    ), clock_timestamp()
  ) ON CONFLICT (device_binding_id) DO NOTHING;

  IF NOT EXISTS (
    SELECT 1
    FROM public.sleep_domain_device_bindings AS binding
    WHERE binding.device_binding_id = metadata ->> 'device_binding_id'
      AND binding.namespace_id = journey_row.namespace_id
      AND binding.data_mode = 'replay'
      AND binding.binding_version =
        (metadata ->> 'binding_version')::integer
      AND binding.device_id = metadata ->> 'device_id'
      AND binding.provider_id = metadata ->> 'provider_id'
      AND binding.provider_account_id = metadata ->> 'provider_account_id'
      AND binding.subject_id = journey_row.subject_id
      AND binding.timezone_name = metadata ->> 'timezone_name'
      AND binding.effective_from =
        (metadata ->> 'scenario_clock_start')::timestamptz
      AND binding.effective_until IS NULL
      AND binding.status = 'active'
      AND binding.binding_json = jsonb_build_object(
        'schema_version', 'replay_device_binding.v1',
        'provider_device_id', metadata ->> 'provider_device_id',
        'synthetic_non_release', TRUE
      )
  ) THEN
    RAISE EXCEPTION 'scenario_contract_invalid';
  END IF;

  FOR actor_entry IN
    SELECT role_name AS role, actor_json #>> '{}' AS actor_id
    FROM jsonb_each(
      metadata -> 'actor_aliases'
    ) AS actors(role_name, actor_json)
  LOOP
    IF actor_entry.role NOT IN ('elder', 'family', 'doctor')
       OR actor_entry.actor_id IS NULL OR actor_entry.actor_id = ''
       OR jsonb_typeof(metadata -> 'actor_scopes' -> actor_entry.role)
         <> 'array' THEN
      RAISE EXCEPTION 'scenario_contract_invalid';
    END IF;
    INSERT INTO public.backend_actors (
      actor_id, actor_kind, status, external_issuer, external_subject
    ) VALUES (
      actor_entry.actor_id, 'human', 'active',
      metadata ->> 'actor_assertion_issuer', actor_entry.actor_id
    ) ON CONFLICT (actor_id) DO NOTHING;
    IF NOT EXISTS (
      SELECT 1 FROM public.backend_actors AS actor
      WHERE actor.actor_id = actor_entry.actor_id
        AND actor.actor_kind = 'human'
        AND actor.status = 'active'
        AND actor.external_issuer = metadata ->> 'actor_assertion_issuer'
        AND actor.external_subject = actor_entry.actor_id
    ) THEN
      RAISE EXCEPTION 'scenario_contract_invalid';
    END IF;

    INSERT INTO public.backend_actor_subject_bindings (
      binding_id, namespace_id, data_mode, actor_id, subject_id,
      role, status, purpose_json, scopes_json, authorization_epoch,
      valid_from
    ) VALUES (
      'binding:' || encode(digest(
        journey_row.namespace_id || E'\x1f' || actor_entry.actor_id
          || E'\x1f' || actor_entry.role,
        'sha256'
      ), 'hex'),
      journey_row.namespace_id, 'replay', actor_entry.actor_id,
      journey_row.subject_id, actor_entry.role, 'active',
      '["sleep_care"]'::jsonb,
      metadata -> 'actor_scopes' -> actor_entry.role,
      1, clock_timestamp()
    ) ON CONFLICT (
      namespace_id, data_mode, actor_id, subject_id, role
    ) DO NOTHING;
    IF NOT EXISTS (
      SELECT 1
      FROM public.backend_actor_subject_bindings AS binding
      WHERE binding.namespace_id = journey_row.namespace_id
        AND binding.data_mode = 'replay'
        AND binding.actor_id = actor_entry.actor_id
        AND binding.subject_id = journey_row.subject_id
        AND binding.role = actor_entry.role
        AND binding.status = 'active'
        AND binding.purpose_json = '["sleep_care"]'::jsonb
        AND binding.scopes_json =
          metadata -> 'actor_scopes' -> actor_entry.role
        AND binding.authorization_epoch = 1
    ) THEN
      RAISE EXCEPTION 'scenario_contract_invalid';
    END IF;
  END LOOP;

  INSERT INTO public.backend_principal_grants (
    grant_id, principal_id, namespace_id, data_mode, purpose,
    scopes_json, allowed_handlers_json, authorization_epoch,
    status, valid_from
  )
  SELECT
    'grant:' || encode(digest(
      bff.principal_id || E'\x1f' || journey_row.namespace_id
        || E'\x1fsleep_care',
      'sha256'
    ), 'hex'),
    bff.principal_id, journey_row.namespace_id, 'replay', 'sleep_care',
    '["product:sleep:today:read","product:sleep:trends:read",'
      '"product:sleep:care:read","product:sleep:records:read",'
      '"product:sleep:interaction:write",'
      '"product:sleep:interaction:answer","product:sleep:care:confirm",'
      '"product:sleep:feedback:write","product:sleep:operation:read",'
      '"sleep:episode:read","sleep:operation:read",'
      '"sleep:monitoring:write","sleep:feedback:self",'
      '"sleep:feedback:family","sleep:reanalysis:write"]'::jsonb,
    '[]'::jsonb, 1, 'active', clock_timestamp()
  FROM public.backend_service_principals AS bff
  WHERE bff.principal_kind = 'bff' AND bff.status = 'active'
  ON CONFLICT (principal_id, namespace_id, data_mode, purpose)
  DO UPDATE SET status = 'active', authorization_epoch = 1,
    scopes_json = EXCLUDED.scopes_json, valid_until = NULL,
    updated_at = clock_timestamp();

  checkpoint_payload := jsonb_build_object(
    'schema_version', 'demo_journey_checkpoint.v1',
    'checkpoint', 'authority_bootstrapped',
    'actor_verification_key_sha256',
      metadata ->> 'actor_verification_key_sha256',
    'roles', jsonb_build_array('doctor', 'elder', 'family')
  );
  INSERT INTO public.backend_demo_journey_checkpoints (
    checkpoint_id, journey_id, namespace_id, data_mode,
    namespace_generation, run_id, arm_id, subject_id,
    phase, semantic_checkpoint_key, checkpoint_sha256,
    checkpoint_json, lease_generation, fencing_token
  ) VALUES (
    gen_random_uuid()::text, journey_row.journey_id,
    journey_row.namespace_id, 'replay', journey_row.namespace_generation,
    journey_row.run_id, journey_row.arm_id, journey_row.subject_id,
    'accepted', 'authority_bootstrapped',
    encode(digest(checkpoint_payload::text, 'sha256'), 'hex'),
    checkpoint_payload, journey_row.lease_generation,
    journey_row.fencing_token
  );

  INSERT INTO public.backend_demo_journey_events (
    event_id, journey_id, namespace_id, data_mode,
    namespace_generation, run_id, arm_id, subject_id,
    event_type, state, event_json
  ) VALUES (
    gen_random_uuid()::text, journey_row.journey_id,
    journey_row.namespace_id, 'replay', journey_row.namespace_generation,
    journey_row.run_id, journey_row.arm_id, journey_row.subject_id,
    'journey.authority_bootstrapped', 'staging_input', checkpoint_payload
  );

  UPDATE public.backend_demo_journeys
  SET phase = 'staging_input', resume_at = clock_timestamp(),
      worker_instance = NULL, fencing_token = NULL,
      lease_expires_at = NULL, heartbeat_at = NULL,
      version = version + 1, updated_at = clock_timestamp()
  WHERE journey_id = journey_row.journey_id
    AND lease_generation = journey_row.lease_generation
    AND fencing_token = journey_row.fencing_token;
  RETURN FOUND;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_bootstrap_demo_journey(TEXT, TEXT)
  FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_claim_normalization_work(
  claimant_worker_instance TEXT,
  requested_lease_seconds INTEGER
)
RETURNS TABLE (
  work_id TEXT,
  namespace_id TEXT,
  data_mode TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT,
  raw_ingress_record_id TEXT,
  authorization_epoch BIGINT,
  privacy_epoch BIGINT,
  retrieval_policy_epoch BIGINT,
  lease_generation BIGINT,
  fencing_token TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  principal TEXT := NULLIF(
    current_setting('sleepagent.service_principal_id', TRUE), ''
  );
  deployment_mode TEXT := NULLIF(
    current_setting('sleepagent.data_mode', TRUE), ''
  );
  request_purpose TEXT := NULLIF(
    current_setting('sleepagent.purpose', TRUE), ''
  );
BEGIN
  IF claimant_worker_instance IS NULL OR claimant_worker_instance = ''
     OR requested_lease_seconds < 1 OR requested_lease_seconds > 3600
     OR principal IS NULL OR deployment_mode NOT IN ('live', 'replay')
     OR request_purpose IS NULL
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <>
       'worker'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid or untrusted normalization claim context';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT work.work_id,
      epoch_row.authorization_epoch AS claim_authorization_epoch,
      epoch_row.privacy_epoch AS claim_privacy_epoch,
      epoch_row.retrieval_policy_epoch AS claim_retrieval_policy_epoch
    FROM public.sleep_domain_normalization_work AS work
    JOIN public.backend_namespaces AS namespace_row
      ON namespace_row.namespace_id = work.namespace_id
     AND namespace_row.data_mode = work.data_mode
    JOIN public.backend_subject_epochs AS epoch_row
      ON epoch_row.namespace_id = work.namespace_id
     AND epoch_row.data_mode = work.data_mode
     AND epoch_row.subject_id = work.subject_id
    WHERE work.protocol_version >= 2
      AND work.data_mode = deployment_mode
      AND (
        work.status IN ('pending', 'retry')
        OR (work.status = 'running'
          AND work.lease_expires_at <= clock_timestamp())
      )
      AND work.available_at <= clock_timestamp()
      AND work.attempt_count < work.max_attempts
      AND (
        work.predecessor_work_id IS NULL
        OR EXISTS (
          SELECT 1
          FROM public.sleep_domain_normalization_work AS predecessor
          WHERE predecessor.work_id = work.predecessor_work_id
            AND predecessor.status = 'succeeded'
        )
      )
      AND namespace_row.status = 'active'
      AND work.authorization_snapshot_json ->> 'authorization_epoch' =
        epoch_row.authorization_epoch::text
      AND work.authorization_snapshot_json ->> 'privacy_epoch' =
        epoch_row.privacy_epoch::text
      AND work.authorization_snapshot_json ->> 'retrieval_policy_epoch' =
        epoch_row.retrieval_policy_epoch::text
      AND EXISTS (
        SELECT 1
        FROM public.backend_principal_grants AS grant_row
        WHERE grant_row.principal_id = principal
          AND grant_row.namespace_id = work.namespace_id
          AND grant_row.data_mode = work.data_mode
          AND grant_row.purpose = request_purpose
          AND grant_row.authorization_epoch =
            epoch_row.authorization_epoch
          AND grant_row.status = 'active'
          AND grant_row.valid_from <= clock_timestamp()
          AND (
            grant_row.valid_until IS NULL
            OR grant_row.valid_until > clock_timestamp()
          )
          AND grant_row.allowed_handlers_json ? 'normalization'
      )
    ORDER BY work.available_at,
      COALESCE(work.stream_sequence, 9223372036854775807),
      work.created_at, work.work_id
    FOR UPDATE OF namespace_row, work SKIP LOCKED
    LIMIT 1
  ), claimed AS (
    UPDATE public.sleep_domain_normalization_work AS work
    SET status = 'running',
        attempt_count = work.attempt_count + 1,
        lease_generation = work.lease_generation + 1,
        fencing_token = gen_random_uuid()::text,
        worker_instance = claimant_worker_instance,
        lease_owner = claimant_worker_instance,
        heartbeat_at = clock_timestamp(),
        lease_expires_at = clock_timestamp()
          + make_interval(secs => requested_lease_seconds),
        updated_at = clock_timestamp()
    FROM candidate
    WHERE work.work_id = candidate.work_id
    RETURNING work.work_id, work.namespace_id, work.data_mode,
      work.namespace_generation, work.run_id, work.arm_id,
      work.subject_id, work.raw_ingress_record_id,
      candidate.claim_authorization_epoch,
      candidate.claim_privacy_epoch,
      candidate.claim_retrieval_policy_epoch,
      work.lease_generation, work.fencing_token
  )
  SELECT claimed.work_id, claimed.namespace_id, claimed.data_mode,
    claimed.namespace_generation, claimed.run_id, claimed.arm_id,
    claimed.subject_id, claimed.raw_ingress_record_id,
    claimed.claim_authorization_epoch, claimed.claim_privacy_epoch,
    claimed.claim_retrieval_policy_epoch, claimed.lease_generation,
    claimed.fencing_token
  FROM claimed;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_claim_normalization_work(TEXT, INTEGER)
  FROM PUBLIC;
