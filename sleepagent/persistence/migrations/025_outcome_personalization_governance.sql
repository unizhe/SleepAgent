-- sleepagent:transactional=true

-- C3: immutable outcome receipts are adapted into one human-governed pending
-- authority. Confirmed state remains exclusively in the existing governed
-- Memory revision ledger.

CREATE TABLE public.backend_personalization_governance_v1 (
  governance_id TEXT PRIMARY KEY,
  receipt_id TEXT NOT NULL UNIQUE
    REFERENCES public.backend_personalization_effect_receipts_v1(receipt_id)
    ON DELETE RESTRICT,
  care_outcome_id TEXT NOT NULL UNIQUE
    REFERENCES public.backend_care_outcomes_v1(care_outcome_id)
    ON DELETE RESTRICT,
  supersedes_governance_id TEXT
    REFERENCES public.backend_personalization_governance_v1(governance_id)
    ON DELETE RESTRICT,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  care_plan_id TEXT NOT NULL,
  action_type TEXT NOT NULL,
  care_outcome_semantic_sha256 TEXT NOT NULL CHECK (
    care_outcome_semantic_sha256 ~ '^[0-9a-f]{64}$'
  ),
  outcome_policy_version TEXT NOT NULL,
  outcome_policy_sha256 TEXT NOT NULL CHECK (
    outcome_policy_sha256 ~ '^[0-9a-f]{64}$'
  ),
  evaluation_revision INTEGER NOT NULL CHECK (evaluation_revision >= 1),
  outcome_category TEXT NOT NULL CHECK (
    outcome_category IN ('improved', 'stable', 'worsened')
  ),
  candidate_semantic_sha256 TEXT NOT NULL CHECK (
    candidate_semantic_sha256 ~ '^[0-9a-f]{64}$'
  ),
  candidate_target_sha256 TEXT NOT NULL CHECK (
    candidate_target_sha256 ~ '^[0-9a-f]{64}$'
  ),
  memory_id TEXT NOT NULL,
  memory_concept_id TEXT NOT NULL CHECK (
    memory_concept_id = 'care_outcome.consistent_wake_time_episode'
  ),
  memory_purpose TEXT NOT NULL CHECK (
    memory_purpose = 'personal_evidence_context'
  ),
  memory_source_ref TEXT NOT NULL,
  causal_claim BOOLEAN NOT NULL DEFAULT FALSE CHECK (NOT causal_claim),
  confirmation_required BOOLEAN NOT NULL DEFAULT TRUE CHECK (
    confirmation_required
  ),
  status TEXT NOT NULL CHECK (
    status IN ('pending', 'accepted', 'rejected', 'superseded')
  ),
  state_version BIGINT NOT NULL DEFAULT 1 CHECK (state_version >= 1),
  decision_id TEXT,
  memory_revision_ref TEXT,
  memory_revision_sha256 TEXT CHECK (
    memory_revision_sha256 IS NULL
    OR memory_revision_sha256 ~ '^[0-9a-f]{64}$'
  ),
  governance_json JSONB NOT NULL CHECK (
    jsonb_typeof(governance_json) = 'object'
  ),
  candidate_json JSONB NOT NULL CHECK (
    jsonb_typeof(candidate_json) = 'object'
    AND candidate_json ->> 'concept_id' = memory_concept_id
    AND candidate_json ->> 'subject_id' = subject_id
    AND candidate_json ->> 'source_ref' = memory_source_ref
    AND candidate_json ->> 'confirmation_required' = 'true'
    AND candidate_json ->> 'provenance_type' = 'accepted_evidence'
  ),
  registered_at TIMESTAMPTZ NOT NULL,
  decided_at TIMESTAMPTZ,
  superseded_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (namespace_id, data_mode, subject_id)
    REFERENCES public.backend_subjects(namespace_id, data_mode, subject_id)
    ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  ),
  CHECK (
    (status = 'pending' AND decision_id IS NULL AND decided_at IS NULL
      AND superseded_at IS NULL AND memory_revision_ref IS NULL
      AND memory_revision_sha256 IS NULL)
    OR (status = 'accepted' AND decision_id IS NOT NULL
      AND decided_at IS NOT NULL AND superseded_at IS NULL
      AND memory_revision_ref IS NOT NULL
      AND memory_revision_sha256 IS NOT NULL)
    OR (status = 'rejected' AND decision_id IS NOT NULL
      AND decided_at IS NOT NULL AND superseded_at IS NULL
      AND memory_revision_ref IS NULL AND memory_revision_sha256 IS NULL)
    OR (status = 'superseded' AND decision_id IS NULL
      AND decided_at IS NULL AND superseded_at IS NOT NULL
      AND memory_revision_ref IS NULL AND memory_revision_sha256 IS NULL)
  )
);

CREATE INDEX idx_backend_personalization_governance_pending_v1
  ON public.backend_personalization_governance_v1 (
    namespace_id, data_mode, namespace_generation, status,
    registered_at, governance_id
  );

CREATE TABLE public.backend_personalization_governance_decisions_v1 (
  decision_id TEXT PRIMARY KEY,
  governance_id TEXT NOT NULL UNIQUE
    REFERENCES public.backend_personalization_governance_v1(governance_id)
    ON DELETE RESTRICT,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  choice TEXT NOT NULL CHECK (choice IN ('accept', 'reject')),
  idempotency_key TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (actor_role = 'elder'),
  actor_binding_id TEXT NOT NULL,
  authorization_epoch BIGINT NOT NULL CHECK (authorization_epoch >= 1),
  privacy_epoch BIGINT NOT NULL CHECK (privacy_epoch >= 1),
  retrieval_policy_epoch BIGINT NOT NULL CHECK (retrieval_policy_epoch >= 1),
  authority_policy_sha256 TEXT NOT NULL CHECK (
    authority_policy_sha256 ~ '^[0-9a-f]{64}$'
  ),
  candidate_semantic_sha256 TEXT NOT NULL CHECK (
    candidate_semantic_sha256 ~ '^[0-9a-f]{64}$'
  ),
  candidate_target_sha256 TEXT NOT NULL CHECK (
    candidate_target_sha256 ~ '^[0-9a-f]{64}$'
  ),
  handle_state_version BIGINT NOT NULL CHECK (handle_state_version >= 1),
  memory_change_id TEXT,
  memory_change_sha256 TEXT CHECK (
    memory_change_sha256 IS NULL
    OR memory_change_sha256 ~ '^[0-9a-f]{64}$'
  ),
  memory_revision_ref TEXT,
  memory_revision_sha256 TEXT CHECK (
    memory_revision_sha256 IS NULL
    OR memory_revision_sha256 ~ '^[0-9a-f]{64}$'
  ),
  decision_json JSONB NOT NULL CHECK (jsonb_typeof(decision_json) = 'object'),
  decided_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (namespace_id, data_mode, subject_id)
    REFERENCES public.backend_subjects(namespace_id, data_mode, subject_id)
    ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  ),
  CHECK (
    (choice = 'accept' AND memory_change_id IS NOT NULL
      AND memory_change_sha256 IS NOT NULL AND memory_revision_ref IS NOT NULL
      AND memory_revision_sha256 IS NOT NULL)
    OR (choice = 'reject' AND memory_change_id IS NULL
      AND memory_change_sha256 IS NULL AND memory_revision_ref IS NULL
      AND memory_revision_sha256 IS NULL)
  )
);

CREATE UNIQUE INDEX uq_backend_personalization_decision_idempotency_v1
  ON public.backend_personalization_governance_decisions_v1 (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''), subject_id,
    actor_id, idempotency_key
  );

ALTER TABLE public.backend_personalization_governance_v1
  ADD CONSTRAINT backend_personalization_governance_decision_fk_v1
  FOREIGN KEY (decision_id)
  REFERENCES public.backend_personalization_governance_decisions_v1(decision_id)
  DEFERRABLE INITIALLY DEFERRED;

-- Existing schema-022 receipts are bridged without mutating their evidence.
WITH supported AS (
  SELECT receipt.*, outcome.policy_version, outcome.policy_sha256,
    outcome.evaluation_revision, outcome.outcome_category,
    outcome.outcome_json, receipt.candidate_json -> 'semantic_content' AS memory
  FROM public.backend_personalization_effect_receipts_v1 AS receipt
  JOIN public.backend_care_outcomes_v1 AS outcome
    ON outcome.care_outcome_id = receipt.care_outcome_id
  WHERE receipt.state = 'candidate_proposed'
    AND receipt.candidate_type = 'governed_memory'
    AND receipt.candidate_json -> 'semantic_content' ->> 'concept_id' =
      'care_outcome.consistent_wake_time_episode'
    AND receipt.candidate_json -> 'semantic_content' ->> 'provenance_type' =
      'accepted_evidence'
    AND receipt.candidate_json -> 'semantic_content' ->>
      'confirmation_required' = 'true'
    AND receipt.candidate_json -> 'semantic_content' ->> 'source_ref' =
      'evidence:' || receipt.care_outcome_id
    AND receipt.candidate_json -> 'semantic_content' ->> 'subject_id' =
      receipt.subject_id
    AND receipt.candidate_semantic_sha256 =
      receipt.candidate_json -> 'semantic_content' ->> 'candidate_hash'
    AND outcome.outcome_category IN ('improved', 'stable', 'worsened')
    AND NOT outcome.causal_claim
)
INSERT INTO public.backend_personalization_governance_v1 (
  governance_id, receipt_id, care_outcome_id, namespace_id, data_mode,
  namespace_generation, run_id, arm_id, subject_id, care_plan_id,
  action_type, care_outcome_semantic_sha256, outcome_policy_version,
  outcome_policy_sha256, evaluation_revision, outcome_category,
  candidate_semantic_sha256, candidate_target_sha256, memory_id,
  memory_concept_id, memory_purpose, memory_source_ref, causal_claim,
  confirmation_required, status, state_version, governance_json,
  candidate_json, registered_at, superseded_at, updated_at
)
SELECT
  'personalization-governance:' || substring(encode(digest(convert_to(
    item.receipt_id || chr(31) || item.candidate_semantic_sha256, 'UTF8'
  ), 'sha256'), 'hex') FROM 1 FOR 32),
  item.receipt_id, item.care_outcome_id, item.namespace_id, item.data_mode,
  item.namespace_generation, item.run_id, item.arm_id, item.subject_id,
  item.receipt_json ->> 'care_plan_id', item.action_type,
  item.care_outcome_semantic_sha256, item.policy_version,
  item.policy_sha256, item.evaluation_revision, item.outcome_category,
  item.candidate_semantic_sha256,
  encode(digest(convert_to(
    item.receipt_id || chr(31) || item.care_outcome_id || chr(31) ||
    item.care_outcome_semantic_sha256 || chr(31) ||
    item.candidate_semantic_sha256 || chr(31) || item.subject_id || chr(31) ||
    (item.receipt_json ->> 'care_plan_id') || chr(31) || item.action_type ||
    chr(31) || item.policy_version || chr(31) || item.policy_sha256 || chr(31) ||
    item.evaluation_revision::TEXT, 'UTF8'
  ), 'sha256'), 'hex'),
  item.memory ->> 'candidate_id', item.memory ->> 'concept_id',
  'personal_evidence_context', item.memory ->> 'source_ref', FALSE, TRUE,
  CASE WHEN EXISTS (
    SELECT 1 FROM public.backend_personalization_effect_receipts_v1 AS newer
    WHERE newer.supersedes_receipt_id = item.receipt_id
  ) THEN 'superseded' ELSE 'pending' END,
  1,
  jsonb_build_object(
    'schema_version', 'outcome_personalization_governance.v1',
    'receipt_id', item.receipt_id,
    'care_outcome_id', item.care_outcome_id,
    'candidate_semantic_hash', item.candidate_semantic_sha256,
    'causal_claim', FALSE,
    'confirmation_required', TRUE
  ),
  item.memory, item.created_at,
  CASE WHEN EXISTS (
    SELECT 1 FROM public.backend_personalization_effect_receipts_v1 AS newer
    WHERE newer.supersedes_receipt_id = item.receipt_id
  ) THEN item.created_at ELSE NULL END,
  item.created_at
FROM supported AS item
ON CONFLICT (receipt_id) DO NOTHING;

UPDATE public.backend_personalization_governance_v1 AS current_item
SET supersedes_governance_id = prior.governance_id
FROM public.backend_personalization_effect_receipts_v1 AS receipt,
     public.backend_personalization_governance_v1 AS prior
WHERE current_item.receipt_id = receipt.receipt_id
  AND receipt.supersedes_receipt_id = prior.receipt_id
  AND current_item.supersedes_governance_id IS NULL;

CREATE OR REPLACE FUNCTION public.sleepagent_validate_personalization_governance_v1()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF ROW(
    NEW.governance_id, NEW.receipt_id, NEW.care_outcome_id,
    NEW.supersedes_governance_id, NEW.namespace_id, NEW.data_mode,
    NEW.namespace_generation, NEW.run_id, NEW.arm_id, NEW.subject_id,
    NEW.care_plan_id, NEW.action_type, NEW.care_outcome_semantic_sha256,
    NEW.outcome_policy_version, NEW.outcome_policy_sha256,
    NEW.evaluation_revision, NEW.outcome_category,
    NEW.candidate_semantic_sha256, NEW.candidate_target_sha256,
    NEW.memory_id, NEW.memory_concept_id, NEW.memory_purpose,
    NEW.memory_source_ref, NEW.causal_claim, NEW.confirmation_required,
    NEW.governance_json, NEW.candidate_json, NEW.registered_at
  ) IS DISTINCT FROM ROW(
    OLD.governance_id, OLD.receipt_id, OLD.care_outcome_id,
    OLD.supersedes_governance_id, OLD.namespace_id, OLD.data_mode,
    OLD.namespace_generation, OLD.run_id, OLD.arm_id, OLD.subject_id,
    OLD.care_plan_id, OLD.action_type, OLD.care_outcome_semantic_sha256,
    OLD.outcome_policy_version, OLD.outcome_policy_sha256,
    OLD.evaluation_revision, OLD.outcome_category,
    OLD.candidate_semantic_sha256, OLD.candidate_target_sha256,
    OLD.memory_id, OLD.memory_concept_id, OLD.memory_purpose,
    OLD.memory_source_ref, OLD.causal_claim, OLD.confirmation_required,
    OLD.governance_json, OLD.candidate_json, OLD.registered_at
  ) OR OLD.status <> 'pending'
    OR NEW.status NOT IN ('accepted', 'rejected', 'superseded')
    OR NEW.state_version <> OLD.state_version + 1
    OR NEW.updated_at < OLD.updated_at THEN
    RAISE EXCEPTION 'invalid personalization governance transition';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER backend_personalization_governance_validate_v1_trigger
BEFORE UPDATE ON public.backend_personalization_governance_v1
FOR EACH ROW EXECUTE FUNCTION
  public.sleepagent_validate_personalization_governance_v1();

CREATE TRIGGER backend_personalization_governance_no_delete_v1_trigger
BEFORE DELETE ON public.backend_personalization_governance_v1
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_reject_append_only_mutation();

CREATE TRIGGER backend_personalization_decision_append_only_v1_trigger
BEFORE UPDATE OR DELETE
ON public.backend_personalization_governance_decisions_v1
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_reject_append_only_mutation();

ALTER TABLE public.backend_personalization_governance_v1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.backend_personalization_governance_v1
  FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_personalization_governance_scope_v1
  ON public.backend_personalization_governance_v1
  USING (
    public.sleepagent_scope_setting('sleepagent.purpose') = 'internal_status'
    OR (
      public.sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
      )
      AND (
        public.sleepagent_scope_setting('sleepagent.process_role') = 'worker'
        OR (
          public.sleepagent_scope_setting('sleepagent.process_role') = 'api'
          AND public.sleepagent_scope_setting('sleepagent.actor_role') = 'elder'
        )
      )
    )
  )
  WITH CHECK (
    public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND (
      public.sleepagent_scope_setting('sleepagent.process_role') = 'worker'
      OR (
        public.sleepagent_scope_setting('sleepagent.process_role') = 'api'
        AND public.sleepagent_scope_setting('sleepagent.actor_role') = 'elder'
      )
    )
  );

ALTER TABLE public.backend_personalization_governance_decisions_v1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.backend_personalization_governance_decisions_v1
  FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_personalization_governance_decision_scope_v1
  ON public.backend_personalization_governance_decisions_v1
  USING (
    public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND public.sleepagent_scope_setting('sleepagent.process_role') = 'api'
    AND public.sleepagent_scope_setting('sleepagent.actor_role') = 'elder'
  )
  WITH CHECK (
    public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND public.sleepagent_scope_setting('sleepagent.process_role') = 'api'
    AND public.sleepagent_scope_setting('sleepagent.actor_role') = 'elder'
  );

CREATE OR REPLACE FUNCTION
  public.sleepagent_personalization_governance_metrics_v1()
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  result JSONB;
BEGIN
  IF NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'api'
     OR NULLIF(current_setting('sleepagent.purpose', TRUE), '') <>
       'internal_status'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid personalization governance metrics context';
  END IF;
  SELECT jsonb_build_object(
    'schema_version', 'personalization_governance_metrics.v1',
    'pending_personalization_governance', count(*) FILTER (
      WHERE status = 'pending'
    ),
    'oldest_pending_candidate_age_seconds', COALESCE(extract(epoch FROM (
      clock_timestamp() - min(registered_at) FILTER (WHERE status = 'pending')
    ))::BIGINT, 0),
    'accepted_personalization_candidates', count(*) FILTER (
      WHERE status = 'accepted'
    ),
    'rejected_personalization_candidates', count(*) FILTER (
      WHERE status = 'rejected'
    ),
    'superseded_personalization_candidates', count(*) FILTER (
      WHERE status = 'superseded'
    ),
    'decision_conflict_or_error_count', 0
  ) INTO result
  FROM public.backend_personalization_governance_v1
  WHERE data_mode = NULLIF(current_setting('sleepagent.data_mode', TRUE), '');
  RETURN result;
END;
$$;

REVOKE ALL ON TABLE public.backend_personalization_governance_v1 FROM PUBLIC;
REVOKE ALL ON TABLE public.backend_personalization_governance_decisions_v1
  FROM PUBLIC;
REVOKE ALL ON FUNCTION
  public.sleepagent_validate_personalization_governance_v1() FROM PUBLIC;
REVOKE ALL ON FUNCTION
  public.sleepagent_personalization_governance_metrics_v1() FROM PUBLIC;
