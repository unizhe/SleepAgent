-- sleepagent:transactional=true
-- Minimal L2 production persistence for the frozen Habit and Governed Memory
-- reducers.  The two ledgers remain independent; backend_pending_handles is
-- reused as the exact-target HITL authority.

CREATE TABLE backend_habit_question_selections_v2 (
  selection_row_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  selection_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('elder', 'family')),
  selection_sha256 TEXT NOT NULL CHECK (
    selection_sha256 ~ '^[0-9a-f]{64}$'
  ),
  selection_json JSONB NOT NULL CHECK (jsonb_typeof(selection_json) = 'object'),
  evidence_json JSONB,
  consumed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  expires_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (namespace_id, data_mode, subject_id)
    REFERENCES backend_subjects (namespace_id, data_mode, subject_id)
    ON DELETE RESTRICT,
  CHECK (expires_at > created_at),
  CHECK (evidence_json IS NULL OR jsonb_typeof(evidence_json) = 'array'),
  CHECK ((consumed_at IS NULL) = (evidence_json IS NULL)),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE UNIQUE INDEX uq_backend_habit_selection_scope_v2
  ON backend_habit_question_selections_v2 (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''), subject_id, selection_id
  );

CREATE TABLE backend_habit_profile_revisions_v2 (
  fact_row_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  fact_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  profile_version BIGINT NOT NULL CHECK (profile_version >= 1),
  concept_id TEXT NOT NULL,
  concept_version TEXT NOT NULL,
  operation TEXT NOT NULL CHECK (
    operation IN ('remember', 'correct', 'expire', 'forget')
  ),
  fact_sha256 TEXT NOT NULL CHECK (fact_sha256 ~ '^[0-9a-f]{64}$'),
  fact_json JSONB NOT NULL CHECK (jsonb_typeof(fact_json) = 'object'),
  confirmation_ref TEXT NOT NULL,
  committed_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (namespace_id, data_mode, subject_id)
    REFERENCES backend_subjects (namespace_id, data_mode, subject_id)
    ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX idx_backend_habit_profile_subject_v2
  ON backend_habit_profile_revisions_v2 (
    namespace_id, data_mode, namespace_generation, subject_id, profile_version
  );

CREATE UNIQUE INDEX uq_backend_habit_profile_version_v2
  ON backend_habit_profile_revisions_v2 (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''), subject_id, profile_version
  );

CREATE UNIQUE INDEX uq_backend_habit_fact_scope_v2
  ON backend_habit_profile_revisions_v2 (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''), subject_id, fact_id
  );

CREATE TABLE backend_governed_memory_revisions_v2 (
  memory_revision_row_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  revision_ref TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  state_version BIGINT NOT NULL CHECK (state_version >= 1),
  memory_id TEXT NOT NULL,
  memory_version BIGINT NOT NULL CHECK (memory_version >= 1),
  concept_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (
    status IN ('active', 'expired', 'forgotten')
  ),
  revision_sha256 TEXT NOT NULL CHECK (
    revision_sha256 ~ '^[0-9a-f]{64}$'
  ),
  revision_json JSONB NOT NULL CHECK (jsonb_typeof(revision_json) = 'object'),
  confirmation_ref TEXT NOT NULL,
  committed_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (namespace_id, data_mode, subject_id)
    REFERENCES backend_subjects (namespace_id, data_mode, subject_id)
    ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX idx_backend_governed_memory_subject_v2
  ON backend_governed_memory_revisions_v2 (
    namespace_id, data_mode, namespace_generation, subject_id, state_version
  );

CREATE UNIQUE INDEX uq_backend_governed_memory_state_version_v2
  ON backend_governed_memory_revisions_v2 (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''), subject_id, state_version
  );

CREATE UNIQUE INDEX uq_backend_governed_memory_lineage_version_v2
  ON backend_governed_memory_revisions_v2 (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''), subject_id,
    memory_id, memory_version
  );

CREATE UNIQUE INDEX uq_backend_memory_revision_scope_v2
  ON backend_governed_memory_revisions_v2 (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''), subject_id, revision_ref
  );

CREATE TABLE backend_memory_read_receipts_v2 (
  receipt_row_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  receipt_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  product_episode_id TEXT,
  requesting_agent TEXT NOT NULL CHECK (
    requesting_agent IN ('sleep_care', 'evidence_reasoning', 'care_strategy')
  ),
  purpose TEXT NOT NULL,
  query_sha256 TEXT NOT NULL CHECK (query_sha256 ~ '^[0-9a-f]{64}$'),
  result_sha256 TEXT NOT NULL CHECK (result_sha256 ~ '^[0-9a-f]{64}$'),
  receipt_sha256 TEXT NOT NULL CHECK (receipt_sha256 ~ '^[0-9a-f]{64}$'),
  authorization_epoch BIGINT NOT NULL CHECK (authorization_epoch >= 0),
  privacy_epoch BIGINT NOT NULL CHECK (privacy_epoch >= 0),
  receipt_json JSONB NOT NULL CHECK (jsonb_typeof(receipt_json) = 'object'),
  completed_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (namespace_id, data_mode, subject_id)
    REFERENCES backend_subjects (namespace_id, data_mode, subject_id)
    ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX idx_backend_memory_receipt_episode_v2
  ON backend_memory_read_receipts_v2 (
    namespace_id, data_mode, namespace_generation, subject_id,
    product_episode_id, requesting_agent
  );

CREATE UNIQUE INDEX uq_backend_memory_receipt_scope_v2
  ON backend_memory_read_receipts_v2 (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''), subject_id, receipt_id
  );

CREATE TRIGGER backend_habit_profile_revision_v2_immutable
BEFORE UPDATE OR DELETE ON backend_habit_profile_revisions_v2
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

CREATE TRIGGER backend_governed_memory_revision_v2_immutable
BEFORE UPDATE OR DELETE ON backend_governed_memory_revisions_v2
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

CREATE TRIGGER backend_memory_read_receipt_v2_immutable
BEFORE UPDATE OR DELETE ON backend_memory_read_receipts_v2
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

ALTER TABLE backend_habit_question_selections_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_habit_question_selections_v2 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_habit_question_selection_v2_scope
  ON backend_habit_question_selections_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_habit_profile_revisions_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_habit_profile_revisions_v2 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_habit_profile_revision_v2_scope
  ON backend_habit_profile_revisions_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_governed_memory_revisions_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_governed_memory_revisions_v2 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_governed_memory_revision_v2_scope
  ON backend_governed_memory_revisions_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_memory_read_receipts_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_memory_read_receipts_v2 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_memory_read_receipt_v2_scope
  ON backend_memory_read_receipts_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

-- A family observer may originate a conflicting Habit proposal, but only the
-- currently bound elder is allowed to consume its exact confirmation handle.
-- The original pending-handle policy deliberately keys visibility to the
-- consuming actor, so this narrowly scoped INSERT policy bridges only that
-- family-to-elder handoff.
CREATE OR REPLACE FUNCTION sleepagent_l2_elder_confirmation_target_allows(
  requested_actor_id TEXT,
  requested_namespace_id TEXT,
  requested_data_mode TEXT,
  requested_subject_id TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT EXISTS (
    SELECT 1
    FROM public.backend_actor_subject_bindings AS binding
    WHERE binding.actor_id = requested_actor_id
      AND binding.namespace_id = requested_namespace_id
      AND binding.data_mode = requested_data_mode
      AND binding.subject_id = requested_subject_id
      AND binding.role = 'elder'
      AND binding.status = 'active'
      AND binding.valid_from <= clock_timestamp()
      AND (
        binding.valid_until IS NULL
        OR binding.valid_until > clock_timestamp()
      )
  )
$$;

CREATE POLICY backend_pending_l2_family_delegation_scope
  ON backend_pending_handles
  FOR INSERT
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND sleepagent_scope_setting('sleepagent.process_role') = 'api'
    AND sleepagent_scope_setting('sleepagent.actor_role') = 'family'
    AND handle_kind = 'confirmation'
    AND target_resource_type = 'l2_habit_change'
    AND actor_id <> sleepagent_scope_setting('sleepagent.actor_id')
    AND role = 'elder'
    AND status = 'pending'
    AND handle_json ->> 'schema_version' = 'l2_pending_change.v1'
    AND handle_json ->> 'capability' = 'habit'
    AND handle_json ->> 'source_actor_id' =
      sleepagent_scope_setting('sleepagent.actor_id')
    AND handle_json ->> 'source_actor_role' = 'family'
    AND handle_json ->> 'confirmation_actor_id' = actor_id
    AND sleepagent_l2_elder_confirmation_target_allows(
      actor_id, namespace_id, data_mode, subject_id
    )
  );
