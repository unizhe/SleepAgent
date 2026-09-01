-- sleepagent:transactional=true

-- G10: deterministic, non-causal care outcome evaluation.  The mutable
-- registration is a current projection; outcome and personalization evidence
-- are append-only revisions.

CREATE TABLE public.backend_care_outcome_evaluations_v1 (
  evaluation_registration_id TEXT PRIMARY KEY,
  care_plan_id TEXT NOT NULL UNIQUE
    REFERENCES public.backend_care_plans_v1(care_plan_id) ON DELETE RESTRICT,
  care_execution_event_id TEXT NOT NULL UNIQUE
    REFERENCES public.backend_care_execution_events_v1(event_id)
    ON DELETE RESTRICT,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  action_type TEXT NOT NULL CHECK (action_type IN (
    'recommend_consistent_wake_time', 'recommend_morning_light',
    'request_manual_follow_up', 'request_morning_review_feedback'
  )),
  execution_authority TEXT NOT NULL CHECK (
    execution_authority = 'human_attested'
  ),
  completed_at TIMESTAMPTZ NOT NULL,
  policy_version TEXT NOT NULL CHECK (
    policy_version = 'care-outcome-evaluation.v1'
  ),
  policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  observation_window_start TIMESTAMPTZ NOT NULL,
  observation_window_end TIMESTAMPTZ NOT NULL,
  state TEXT NOT NULL CHECK (state IN (
    'waiting_for_followup', 'ready_for_evaluation', 'evaluated',
    'insufficient_data', 'not_comparable'
  )),
  reason_code TEXT CHECK (length(reason_code) <= 100),
  current_care_outcome_id TEXT,
  current_evaluation_revision INTEGER NOT NULL DEFAULT 0 CHECK (
    current_evaluation_revision >= 0
  ),
  cas_version BIGINT NOT NULL DEFAULT 1 CHECK (cas_version >= 1),
  registered_at TIMESTAMPTZ NOT NULL,
  last_evaluated_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL,
  CHECK (observation_window_end >= observation_window_start),
  CHECK (
    (current_evaluation_revision = 0 AND current_care_outcome_id IS NULL)
    OR (current_evaluation_revision >= 1 AND current_care_outcome_id IS NOT NULL)
  ),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX idx_backend_care_outcome_evaluation_waiting_v1
  ON public.backend_care_outcome_evaluations_v1 (
    namespace_id, data_mode, state, observation_window_end, registered_at
  );

CREATE TABLE public.backend_care_outcomes_v1 (
  care_outcome_id TEXT PRIMARY KEY,
  evaluation_registration_id TEXT NOT NULL
    REFERENCES public.backend_care_outcome_evaluations_v1(
      evaluation_registration_id
    ) ON DELETE RESTRICT,
  care_plan_id TEXT NOT NULL
    REFERENCES public.backend_care_plans_v1(care_plan_id) ON DELETE RESTRICT,
  care_execution_event_id TEXT NOT NULL
    REFERENCES public.backend_care_execution_events_v1(event_id)
    ON DELETE RESTRICT,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  action_type TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  evaluation_revision INTEGER NOT NULL CHECK (evaluation_revision >= 1),
  supersedes_care_outcome_id TEXT
    REFERENCES public.backend_care_outcomes_v1(care_outcome_id)
    ON DELETE RESTRICT,
  semantic_sha256 TEXT NOT NULL CHECK (semantic_sha256 ~ '^[0-9a-f]{64}$'),
  baseline_revision_ids_json JSONB NOT NULL CHECK (
    jsonb_typeof(baseline_revision_ids_json) = 'array'
  ),
  followup_revision_ids_json JSONB NOT NULL CHECK (
    jsonb_typeof(followup_revision_ids_json) = 'array'
  ),
  outcome_category TEXT NOT NULL CHECK (outcome_category IN (
    'improved', 'stable', 'worsened', 'insufficient_data',
    'not_comparable', 'execution_only'
  )),
  evidence_quality TEXT NOT NULL CHECK (evidence_quality IN (
    'high', 'moderate', 'limited', 'insufficient'
  )),
  execution_authority TEXT NOT NULL CHECK (
    execution_authority = 'human_attested'
  ),
  causal_claim BOOLEAN NOT NULL DEFAULT FALSE CHECK (NOT causal_claim),
  outcome_json JSONB NOT NULL CHECK (
    jsonb_typeof(outcome_json) = 'object'
    AND outcome_json ->> 'schema_version' = 'care_outcome.v1'
    AND outcome_json ->> 'causal_claim' = 'false'
  ),
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (evaluation_registration_id, evaluation_revision),
  UNIQUE (evaluation_registration_id, semantic_sha256),
  CHECK (
    (evaluation_revision = 1 AND supersedes_care_outcome_id IS NULL)
    OR (evaluation_revision > 1 AND supersedes_care_outcome_id IS NOT NULL)
  ),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

ALTER TABLE public.backend_care_outcome_evaluations_v1
  ADD CONSTRAINT backend_care_outcome_evaluation_current_fk_v1
  FOREIGN KEY (current_care_outcome_id)
  REFERENCES public.backend_care_outcomes_v1(care_outcome_id)
  DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX idx_backend_care_outcome_subject_v1
  ON public.backend_care_outcomes_v1 (
    namespace_id, data_mode, subject_id, created_at, care_outcome_id
  );

CREATE TABLE public.backend_personalization_effect_receipts_v1 (
  receipt_id TEXT PRIMARY KEY,
  supersedes_receipt_id TEXT
    REFERENCES public.backend_personalization_effect_receipts_v1(receipt_id)
    ON DELETE RESTRICT,
  care_outcome_id TEXT NOT NULL UNIQUE
    REFERENCES public.backend_care_outcomes_v1(care_outcome_id)
    ON DELETE RESTRICT,
  evaluation_registration_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  action_type TEXT NOT NULL,
  care_outcome_semantic_sha256 TEXT NOT NULL CHECK (
    care_outcome_semantic_sha256 ~ '^[0-9a-f]{64}$'
  ),
  state TEXT NOT NULL CHECK (state IN (
    'no_personalization_change', 'candidate_proposed',
    'candidate_accepted', 'candidate_rejected', 'superseded'
  )),
  personalization_relevant BOOLEAN NOT NULL,
  candidate_type TEXT CHECK (candidate_type IN ('governed_memory', 'habit')),
  candidate_semantic_sha256 TEXT CHECK (
    candidate_semantic_sha256 IS NULL
    OR candidate_semantic_sha256 ~ '^[0-9a-f]{64}$'
  ),
  candidate_json JSONB,
  direct_memory_write BOOLEAN NOT NULL DEFAULT FALSE CHECK (
    NOT direct_memory_write
  ),
  receipt_json JSONB NOT NULL CHECK (
    jsonb_typeof(receipt_json) = 'object'
    AND receipt_json ->> 'schema_version' =
      'personalization_effect_receipt.v1'
  ),
  created_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (evaluation_registration_id)
    REFERENCES public.backend_care_outcome_evaluations_v1(
      evaluation_registration_id
    ) ON DELETE RESTRICT,
  CHECK (
    (state = 'candidate_proposed' AND candidate_type IS NOT NULL
      AND candidate_semantic_sha256 IS NOT NULL
      AND jsonb_typeof(candidate_json) = 'object')
    OR (state <> 'candidate_proposed' AND candidate_type IS NULL
      AND candidate_semantic_sha256 IS NULL AND candidate_json IS NULL)
  ),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX idx_backend_personalization_effect_subject_v1
  ON public.backend_personalization_effect_receipts_v1 (
    namespace_id, data_mode, subject_id, state, created_at
  );

CREATE OR REPLACE FUNCTION public.sleepagent_care_outcome_append_only_v1()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'CareOutcome and PersonalizationEffectReceipt are append-only';
END;
$$;

CREATE TRIGGER backend_care_outcome_append_only_v1_trigger
BEFORE UPDATE OR DELETE ON public.backend_care_outcomes_v1
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_care_outcome_append_only_v1();

CREATE TRIGGER backend_personalization_effect_append_only_v1_trigger
BEFORE UPDATE OR DELETE ON public.backend_personalization_effect_receipts_v1
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_care_outcome_append_only_v1();

CREATE OR REPLACE FUNCTION public.sleepagent_validate_outcome_evaluation_v1()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF ROW(
    NEW.evaluation_registration_id, NEW.care_plan_id,
    NEW.care_execution_event_id, NEW.namespace_id, NEW.data_mode,
    NEW.namespace_generation, NEW.run_id, NEW.arm_id, NEW.subject_id,
    NEW.action_type, NEW.execution_authority, NEW.completed_at,
    NEW.policy_version, NEW.policy_sha256, NEW.observation_window_start,
    NEW.observation_window_end, NEW.registered_at
  ) IS DISTINCT FROM ROW(
    OLD.evaluation_registration_id, OLD.care_plan_id,
    OLD.care_execution_event_id, OLD.namespace_id, OLD.data_mode,
    OLD.namespace_generation, OLD.run_id, OLD.arm_id, OLD.subject_id,
    OLD.action_type, OLD.execution_authority, OLD.completed_at,
    OLD.policy_version, OLD.policy_sha256, OLD.observation_window_start,
    OLD.observation_window_end, OLD.registered_at
  ) THEN
    RAISE EXCEPTION 'Care outcome evaluation identity is immutable';
  END IF;
  IF NEW.cas_version <> OLD.cas_version + 1
     OR NEW.updated_at < OLD.updated_at
     OR COALESCE(NEW.last_evaluated_at, '-infinity'::timestamptz) <
        COALESCE(OLD.last_evaluated_at, '-infinity'::timestamptz)
     OR NEW.current_evaluation_revision < OLD.current_evaluation_revision THEN
    RAISE EXCEPTION 'Care outcome evaluation projection update is invalid';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER backend_care_outcome_evaluation_validate_v1_trigger
BEFORE UPDATE ON public.backend_care_outcome_evaluations_v1
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_validate_outcome_evaluation_v1();

CREATE TRIGGER backend_care_outcome_evaluation_no_delete_v1_trigger
BEFORE DELETE ON public.backend_care_outcome_evaluations_v1
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_care_outcome_append_only_v1();

ALTER TABLE public.backend_care_outcome_evaluations_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.backend_care_outcome_evaluations_v1 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_care_outcome_evaluation_scope_v1
  ON public.backend_care_outcome_evaluations_v1
  USING (
    public.sleepagent_scope_setting('sleepagent.purpose') = 'internal_status'
    OR public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  )
  WITH CHECK (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE public.backend_care_outcomes_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.backend_care_outcomes_v1 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_care_outcome_scope_v1
  ON public.backend_care_outcomes_v1
  USING (
    public.sleepagent_scope_setting('sleepagent.purpose') = 'internal_status'
    OR public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  )
  WITH CHECK (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE public.backend_personalization_effect_receipts_v1
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.backend_personalization_effect_receipts_v1
  FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_personalization_effect_scope_v1
  ON public.backend_personalization_effect_receipts_v1
  USING (
    public.sleepagent_scope_setting('sleepagent.purpose') = 'internal_status'
    OR public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  )
  WITH CHECK (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

-- Evaluation is an internal continuation of NightFinalization.  Only worker
-- grants that already own the finalization handler receive this extra handler.
UPDATE public.backend_principal_grants AS grant_row
SET allowed_handlers_json = grant_row.allowed_handlers_json ||
      '["care.outcome.evaluate.v1"]'::jsonb,
    updated_at = clock_timestamp()
FROM public.backend_service_principals AS principal
WHERE principal.principal_id = grant_row.principal_id
  AND principal.principal_kind = 'worker'
  AND grant_row.purpose = 'worker'
  AND grant_row.allowed_handlers_json ? 'night.finalization_scan'
  AND NOT grant_row.allowed_handlers_json ? 'care.outcome.evaluate.v1';

CREATE OR REPLACE FUNCTION public.sleepagent_queue_care_outcome_v1(
  target_evaluation_registration_id TEXT,
  source_finalization_revision_id TEXT
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  evaluation public.backend_care_outcome_evaluations_v1%ROWTYPE;
  epoch_row public.backend_subject_epochs%ROWTYPE;
  worker_principal TEXT;
  semantic_value TEXT;
  digest_value TEXT;
  timestamp_value TIMESTAMPTZ;
  available_value TIMESTAMPTZ;
  timestamp_hex TEXT;
  operation_value TEXT;
  snapshot JSONB;
  payload JSONB;
BEGIN
  SELECT item.* INTO evaluation
  FROM public.backend_care_outcome_evaluations_v1 AS item
  WHERE item.evaluation_registration_id = target_evaluation_registration_id;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  SELECT item.* INTO epoch_row
  FROM public.backend_subject_epochs AS item
  WHERE item.namespace_id = evaluation.namespace_id
    AND item.data_mode = evaluation.data_mode
    AND item.subject_id = evaluation.subject_id;
  SELECT grant_row.principal_id INTO worker_principal
  FROM public.backend_principal_grants AS grant_row
  JOIN public.backend_service_principals AS principal
    ON principal.principal_id = grant_row.principal_id
  WHERE grant_row.namespace_id = evaluation.namespace_id
    AND grant_row.data_mode = evaluation.data_mode
    AND grant_row.purpose = 'worker'
    AND grant_row.status = 'active'
    AND grant_row.authorization_epoch = epoch_row.authorization_epoch
    AND grant_row.allowed_handlers_json ? 'care.outcome.evaluate.v1'
    AND grant_row.valid_from <= clock_timestamp()
    AND (grant_row.valid_until IS NULL
      OR grant_row.valid_until > clock_timestamp())
    AND principal.principal_kind = 'worker'
    AND principal.status = 'active'
  ORDER BY grant_row.principal_id
  LIMIT 1;
  IF worker_principal IS NULL THEN
    RETURN NULL;
  END IF;
  semantic_value := encode(digest(convert_to(
    evaluation.evaluation_registration_id || chr(31) ||
    COALESCE(source_finalization_revision_id, 'execution_completed') ||
    chr(31) || evaluation.policy_sha256,
    'UTF8'), 'sha256'), 'hex');
  digest_value := encode(digest(convert_to(
    'care-outcome-operation' || chr(31) || semantic_value,
    'UTF8'), 'sha256'), 'hex');
  timestamp_value := GREATEST(evaluation.completed_at, clock_timestamp());
  available_value := CASE
    WHEN source_finalization_revision_id = 'window_expiry'
      THEN evaluation.observation_window_end
    ELSE clock_timestamp()
  END;
  timestamp_hex := lpad(to_hex(floor(
    extract(epoch FROM timestamp_value) * 1000
  )::bigint), 12, '0');
  operation_value :=
    substr(timestamp_hex, 1, 8) || '-' ||
    substr(timestamp_hex, 9, 4) || '-7' ||
    substr(digest_value, 1, 3) || '-8' ||
    substr(digest_value, 4, 3) || '-' ||
    substr(digest_value, 7, 12);
  snapshot := jsonb_build_object(
    'schema_version', 'workload_authorization_snapshot.v1',
    'workload_principal_id', worker_principal,
    'namespace_id', evaluation.namespace_id,
    'namespace_generation', evaluation.namespace_generation,
    'data_mode', evaluation.data_mode,
    'run_id', evaluation.run_id,
    'arm_id', evaluation.arm_id,
    'subject_id', evaluation.subject_id,
    'purpose', 'worker',
    'allowed_handler', 'care.outcome.evaluate.v1',
    'authorization_epoch', epoch_row.authorization_epoch,
    'privacy_epoch', epoch_row.privacy_epoch,
    'retrieval_policy_epoch', epoch_row.retrieval_policy_epoch
  );
  payload := jsonb_build_object(
    'schema_version', 'care_outcome_evaluation_operation.v1',
    'evaluation_registration_id', evaluation.evaluation_registration_id,
    'care_plan_id', evaluation.care_plan_id,
    'source_finalization_revision_id', source_finalization_revision_id,
    'policy_version', evaluation.policy_version,
    'policy_sha256', evaluation.policy_sha256
  );
  INSERT INTO public.sleep_domain_operations (
    operation_id, namespace_id, data_mode, operation_type, subject_id,
    service_principal_id, actor_id, target_resource_id,
    target_resource_key, idempotency_key, request_sha256, status,
    attempt_count, cas_version, operation_json, created_at, updated_at,
    protocol_version, namespace_generation, run_id, arm_id, id_scheme,
    origin_kind, semantic_key, queue_name, priority, available_at,
    max_attempts, workload_authorization_snapshot_json, policy_sha256
  ) VALUES (
    operation_value, evaluation.namespace_id, evaluation.data_mode,
    'care.outcome.evaluate.v1', evaluation.subject_id, worker_principal, NULL,
    evaluation.evaluation_registration_id,
    evaluation.evaluation_registration_id, semantic_value,
    encode(digest(convert_to(payload::text, 'UTF8'), 'sha256'), 'hex'),
    'pending', 0, 0, payload, clock_timestamp(), clock_timestamp(), 2,
    evaluation.namespace_generation, evaluation.run_id, evaluation.arm_id,
    'uuidv7', 'system', semantic_value, 'care.outcome.evaluate.v1', 60,
    available_value, 5, snapshot, evaluation.policy_sha256
  ) ON CONFLICT DO NOTHING;
  RETURN operation_value;
END;
$$;

CREATE OR REPLACE FUNCTION public.sleepagent_register_care_outcome_v1()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  plan public.backend_care_plans_v1%ROWTYPE;
  completion public.backend_care_execution_events_v1%ROWTYPE;
  registration_id TEXT;
  policy_sha TEXT;
  window_days INTEGER;
BEGIN
  IF OLD.state = 'completed' OR NEW.state <> 'completed' THEN
    RETURN NEW;
  END IF;
  SELECT item.* INTO plan FROM public.backend_care_plans_v1 AS item
  WHERE item.care_plan_id = NEW.care_plan_id;
  SELECT item.* INTO completion
  FROM public.backend_care_execution_events_v1 AS item
  WHERE item.care_plan_id = NEW.care_plan_id
    AND item.resulting_state = 'completed'
    AND item.source_authority = 'human_attested'
  ORDER BY item.recorded_at DESC, item.event_id DESC LIMIT 1;
  IF NOT FOUND OR completion.subject_id <> plan.subject_id
     OR completion.occurred_at < plan.valid_from
     OR completion.occurred_at >= plan.valid_until THEN
    RETURN NEW;
  END IF;
  CASE plan.action_type
    WHEN 'recommend_consistent_wake_time' THEN
      policy_sha := 'f586261466954583368e27b7d7dd984419549218d64c7f8f082fdf91ba1bff4f';
      window_days := 14;
    WHEN 'recommend_morning_light' THEN
      policy_sha := 'b0a5e17616add8642ea0306a9c897b13f91b57c4b476381702dd3f98a85ed4ce';
      window_days := 7;
    WHEN 'request_manual_follow_up' THEN
      policy_sha := 'd20f431f9c6719b29a57e3bfa77962158a23e3a0673ea0d9fac3cf74032da3ba';
      window_days := 0;
    WHEN 'request_morning_review_feedback' THEN
      policy_sha := 'd9efbe37fc33df2027c6eddc6e6ee0698f266e2f2a7ce74ae51ab4662d77c1df';
      window_days := 0;
    ELSE
      RETURN NEW;
  END CASE;
  registration_id := 'care-outcome-evaluation:' || substring(encode(digest(
    convert_to(plan.care_plan_id || chr(31) || completion.event_id, 'UTF8'),
    'sha256'), 'hex') FROM 1 FOR 32);
  INSERT INTO public.backend_care_outcome_evaluations_v1 (
    evaluation_registration_id, care_plan_id, care_execution_event_id,
    namespace_id, data_mode, namespace_generation, run_id, arm_id,
    subject_id, action_type, execution_authority, completed_at,
    policy_version, policy_sha256, observation_window_start,
    observation_window_end, state, reason_code, registered_at, updated_at
  ) VALUES (
    registration_id, plan.care_plan_id, completion.event_id,
    plan.namespace_id, plan.data_mode, plan.namespace_generation,
    plan.run_id, plan.arm_id, plan.subject_id, plan.action_type,
    'human_attested', completion.occurred_at, 'care-outcome-evaluation.v1',
    policy_sha, completion.occurred_at,
    completion.occurred_at + make_interval(days => window_days),
    CASE WHEN window_days = 0 THEN 'ready_for_evaluation'
      ELSE 'waiting_for_followup' END,
    CASE WHEN window_days = 0 THEN 'execution_only_ready'
      ELSE 'eligible_hard_finalized_followup_not_available' END,
    clock_timestamp(), clock_timestamp()
  ) ON CONFLICT (care_plan_id) DO NOTHING;
  PERFORM public.sleepagent_queue_care_outcome_v1(registration_id, NULL);
  IF window_days > 0 THEN
    PERFORM public.sleepagent_queue_care_outcome_v1(
      registration_id, 'window_expiry'
    );
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER backend_care_execution_register_outcome_v1_trigger
AFTER UPDATE ON public.backend_care_execution_states_v1
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_register_care_outcome_v1();

-- Upgrade closure: completion events committed under G9 before this migration
-- must enter the same deterministic evaluation path as new completions.  The
-- unique care_plan_id constraint and conflict handling make replay harmless.
WITH eligible_completion AS (
  SELECT
    plan.*,
    completion.event_id AS completion_event_id,
    completion.occurred_at AS completion_occurred_at
  FROM public.backend_care_plans_v1 AS plan
  JOIN public.backend_care_execution_states_v1 AS execution_state
    ON execution_state.care_plan_id = plan.care_plan_id
   AND execution_state.state = 'completed'
  JOIN LATERAL (
    SELECT event.event_id, event.occurred_at
    FROM public.backend_care_execution_events_v1 AS event
    WHERE event.care_plan_id = plan.care_plan_id
      AND event.resulting_state = 'completed'
      AND event.source_authority = 'human_attested'
      AND event.subject_id = plan.subject_id
      AND event.occurred_at >= plan.valid_from
      AND event.occurred_at < plan.valid_until
    ORDER BY event.recorded_at DESC, event.event_id DESC
    LIMIT 1
  ) AS completion ON TRUE
)
INSERT INTO public.backend_care_outcome_evaluations_v1 (
  evaluation_registration_id, care_plan_id, care_execution_event_id,
  namespace_id, data_mode, namespace_generation, run_id, arm_id,
  subject_id, action_type, execution_authority, completed_at,
  policy_version, policy_sha256, observation_window_start,
  observation_window_end, state, reason_code, registered_at, updated_at
)
SELECT
  'care-outcome-evaluation:' || substring(encode(digest(convert_to(
    item.care_plan_id || chr(31) || item.completion_event_id, 'UTF8'
  ), 'sha256'), 'hex') FROM 1 FOR 32),
  item.care_plan_id, item.completion_event_id,
  item.namespace_id, item.data_mode, item.namespace_generation,
  item.run_id, item.arm_id, item.subject_id, item.action_type,
  'human_attested', item.completion_occurred_at,
  'care-outcome-evaluation.v1',
  CASE item.action_type
    WHEN 'recommend_consistent_wake_time' THEN
      'f586261466954583368e27b7d7dd984419549218d64c7f8f082fdf91ba1bff4f'
    WHEN 'recommend_morning_light' THEN
      'b0a5e17616add8642ea0306a9c897b13f91b57c4b476381702dd3f98a85ed4ce'
    WHEN 'request_manual_follow_up' THEN
      'd20f431f9c6719b29a57e3bfa77962158a23e3a0673ea0d9fac3cf74032da3ba'
    WHEN 'request_morning_review_feedback' THEN
      'd9efbe37fc33df2027c6eddc6e6ee0698f266e2f2a7ce74ae51ab4662d77c1df'
  END,
  item.completion_occurred_at,
  item.completion_occurred_at + make_interval(days =>
    CASE item.action_type
      WHEN 'recommend_consistent_wake_time' THEN 14
      WHEN 'recommend_morning_light' THEN 7
      ELSE 0
    END
  ),
  CASE WHEN item.action_type IN (
    'request_manual_follow_up', 'request_morning_review_feedback'
  ) THEN 'ready_for_evaluation' ELSE 'waiting_for_followup' END,
  CASE WHEN item.action_type IN (
    'request_manual_follow_up', 'request_morning_review_feedback'
  ) THEN 'execution_only_ready'
  ELSE 'eligible_hard_finalized_followup_not_available' END,
  clock_timestamp(), clock_timestamp()
FROM eligible_completion AS item
WHERE item.action_type IN (
  'recommend_consistent_wake_time', 'recommend_morning_light',
  'request_manual_follow_up', 'request_morning_review_feedback'
)
ON CONFLICT (care_plan_id) DO NOTHING;

DO $$
DECLARE
  evaluation RECORD;
BEGIN
  FOR evaluation IN
    SELECT item.evaluation_registration_id,
      item.observation_window_start, item.observation_window_end
    FROM public.backend_care_outcome_evaluations_v1 AS item
    WHERE item.current_evaluation_revision = 0
    ORDER BY item.registered_at, item.evaluation_registration_id
  LOOP
    PERFORM public.sleepagent_queue_care_outcome_v1(
      evaluation.evaluation_registration_id, NULL
    );
    IF evaluation.observation_window_end > evaluation.observation_window_start THEN
      PERFORM public.sleepagent_queue_care_outcome_v1(
        evaluation.evaluation_registration_id, 'window_expiry'
      );
    END IF;
  END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION public.sleepagent_trigger_care_outcome_followup_v1()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  evaluation RECORD;
BEGIN
  IF NEW.state <> 'hard_finalized' OR NEW.provisional THEN
    RETURN NEW;
  END IF;
  FOR evaluation IN
    SELECT item.evaluation_registration_id
    FROM public.backend_care_outcome_evaluations_v1 AS item
    WHERE item.namespace_id = NEW.namespace_id
      AND item.data_mode = NEW.data_mode
      AND item.namespace_generation = NEW.namespace_generation
      AND item.run_id IS NOT DISTINCT FROM NEW.run_id
      AND item.arm_id IS NOT DISTINCT FROM NEW.arm_id
      AND item.subject_id = NEW.subject_id
      AND item.completed_at < NEW.created_at
      AND item.state IN (
        'waiting_for_followup', 'evaluated', 'insufficient_data',
        'not_comparable'
      )
    ORDER BY item.registered_at
    LIMIT 100
  LOOP
    UPDATE public.backend_care_outcome_evaluations_v1 AS target
    SET state = 'ready_for_evaluation',
        reason_code = 'eligible_hard_finalized_followup_available',
        cas_version = target.cas_version + 1,
        updated_at = clock_timestamp()
    WHERE target.evaluation_registration_id =
          evaluation.evaluation_registration_id
      AND target.state = 'waiting_for_followup'
      AND 1 + (
        SELECT count(*)
        FROM public.sleep_domain_night_finalizations AS current_finalization
        JOIN public.sleep_domain_night_finalization_revisions AS current_revision
          ON current_revision.night_finalization_revision_id =
             current_finalization.current_finalization_revision_id
        WHERE current_finalization.namespace_id = target.namespace_id
          AND current_finalization.data_mode = target.data_mode
          AND current_finalization.namespace_generation =
              target.namespace_generation
          AND current_finalization.run_id IS NOT DISTINCT FROM target.run_id
          AND current_finalization.arm_id IS NOT DISTINCT FROM target.arm_id
          AND current_finalization.subject_id = target.subject_id
          AND current_finalization.night_episode_id <> NEW.night_episode_id
          AND current_finalization.state = 'hard_finalized'
          AND current_revision.state = 'hard_finalized'
          AND NOT current_revision.provisional
          AND current_revision.created_at > target.completed_at
          AND current_revision.created_at <= target.observation_window_end
      ) >= CASE target.action_type
        WHEN 'recommend_consistent_wake_time' THEN 2
        WHEN 'recommend_morning_light' THEN 1
        ELSE 0
      END
      AND NEW.created_at <= target.observation_window_end;
    PERFORM public.sleepagent_queue_care_outcome_v1(
      evaluation.evaluation_registration_id,
      NEW.night_finalization_revision_id
    );
  END LOOP;
  RETURN NEW;
END;
$$;

CREATE TRIGGER sleep_domain_finalization_trigger_care_outcome_v1
AFTER INSERT ON public.sleep_domain_night_finalization_revisions
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_trigger_care_outcome_followup_v1();

CREATE OR REPLACE FUNCTION public.sleepagent_care_outcome_operational_metrics_v1()
RETURNS JSONB
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT CASE WHEN
    NULLIF(current_setting('sleepagent.process_role', TRUE), '') = 'api'
    AND NULLIF(current_setting('sleepagent.purpose', TRUE), '') = 'internal_status'
    AND public.sleepagent_principal_context_allows()
  THEN jsonb_build_object(
    'pending_outcome_evaluations', count(*) FILTER (
      WHERE evaluation.state = 'waiting_for_followup'),
    'oldest_waiting_evaluation_age_seconds', COALESCE(max(
      extract(epoch FROM clock_timestamp() - evaluation.registered_at)
    ) FILTER (WHERE evaluation.state = 'waiting_for_followup'), 0),
    'ready_evaluations', count(*) FILTER (
      WHERE evaluation.state = 'ready_for_evaluation'),
    'evaluated_outcomes', (SELECT count(*)
      FROM public.backend_care_outcomes_v1),
    'insufficient_data_count', (SELECT count(*)
      FROM public.backend_care_outcomes_v1
      WHERE outcome_category = 'insufficient_data'),
    'not_comparable_count', (SELECT count(*)
      FROM public.backend_care_outcomes_v1
      WHERE outcome_category = 'not_comparable'),
    'superseded_outcome_count', (SELECT count(*)
      FROM public.backend_care_outcomes_v1
      WHERE care_outcome_id IN (
        SELECT supersedes_care_outcome_id
        FROM public.backend_care_outcomes_v1
        WHERE supersedes_care_outcome_id IS NOT NULL)),
    'personalization_candidates_proposed', (SELECT count(*)
      FROM public.backend_personalization_effect_receipts_v1 AS receipt
      JOIN public.backend_care_outcome_evaluations_v1 AS current_evaluation
        ON current_evaluation.current_care_outcome_id = receipt.care_outcome_id
      WHERE receipt.state = 'candidate_proposed'),
    'personalization_candidates_accepted', (SELECT count(*)
      FROM public.backend_personalization_effect_receipts_v1 AS receipt
      JOIN public.backend_care_outcome_evaluations_v1 AS current_evaluation
        ON current_evaluation.current_care_outcome_id = receipt.care_outcome_id
      WHERE receipt.state = 'candidate_accepted'),
    'personalization_candidates_rejected', (SELECT count(*)
      FROM public.backend_personalization_effect_receipts_v1 AS receipt
      JOIN public.backend_care_outcome_evaluations_v1 AS current_evaluation
        ON current_evaluation.current_care_outcome_id = receipt.care_outcome_id
      WHERE receipt.state = 'candidate_rejected')
  ) ELSE NULL END
  FROM public.backend_care_outcome_evaluations_v1 AS evaluation
$$;

REVOKE ALL ON TABLE public.backend_care_outcome_evaluations_v1 FROM PUBLIC;
REVOKE ALL ON TABLE public.backend_care_outcomes_v1 FROM PUBLIC;
REVOKE ALL ON TABLE public.backend_personalization_effect_receipts_v1 FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_care_outcome_append_only_v1() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_validate_outcome_evaluation_v1() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_queue_care_outcome_v1(TEXT, TEXT)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_register_care_outcome_v1() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_trigger_care_outcome_followup_v1()
  FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_care_outcome_operational_metrics_v1()
  FROM PUBLIC;
