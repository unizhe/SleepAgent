-- sleepagent:transactional=true

-- G9: terminal care plans and append-only human-attested execution.
-- This migration creates no delivery intent, provider, notification, or effect.

CREATE TABLE public.backend_care_plans_v1 (
  care_plan_id TEXT PRIMARY KEY,
  care_plan_semantic_sha256 TEXT NOT NULL UNIQUE CHECK (
    care_plan_semantic_sha256 ~ '^[0-9a-f]{64}$'
  ),
  approval_grant_id TEXT NOT NULL UNIQUE
    REFERENCES public.backend_approval_grants_v3(grant_id) ON DELETE RESTRICT,
  approval_grant_sha256 TEXT NOT NULL CHECK (
    approval_grant_sha256 ~ '^[0-9a-f]{64}$'
  ),
  proposal_id TEXT NOT NULL
    REFERENCES public.backend_care_action_proposals_v3(proposal_id)
    ON DELETE RESTRICT,
  proposal_semantic_sha256 TEXT NOT NULL CHECK (
    proposal_semantic_sha256 ~ '^[0-9a-f]{64}$'
  ),
  candidate_sha256 TEXT NOT NULL CHECK (candidate_sha256 ~ '^[0-9a-f]{64}$'),
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
  executor_role TEXT NOT NULL CHECK (executor_role IN ('elder', 'family')),
  source_analysis_revision_id TEXT NOT NULL,
  source_shared_analysis_sha256 TEXT NOT NULL CHECK (
    source_shared_analysis_sha256 ~ '^[0-9a-f]{64}$'
  ),
  source_night_finalization_revision_id TEXT NOT NULL,
  source_care_strategy_invocation_id TEXT NOT NULL,
  source_care_strategy_version TEXT NOT NULL,
  source_night_key TEXT NOT NULL,
  evidence_references_json JSONB NOT NULL CHECK (
    jsonb_typeof(evidence_references_json) = 'array'
    AND jsonb_array_length(evidence_references_json) BETWEEN 1 AND 20
  ),
  structured_action_parameters_json JSONB NOT NULL CHECK (
    jsonb_typeof(structured_action_parameters_json) = 'object'
  ),
  approval_policy_version TEXT NOT NULL,
  approval_policy_sha256 TEXT NOT NULL CHECK (
    approval_policy_sha256 ~ '^[0-9a-f]{64}$'
  ),
  approval_authorization_scope TEXT NOT NULL,
  approval_authorization_epoch BIGINT NOT NULL CHECK (
    approval_authorization_epoch >= 1
  ),
  execution_policy_version TEXT NOT NULL CHECK (
    execution_policy_version = 'terminal-care-execution.v1'
  ),
  execution_policy_sha256 TEXT NOT NULL CHECK (
    execution_policy_sha256 ~ '^[0-9a-f]{64}$'
  ),
  execution_mode TEXT NOT NULL CHECK (
    execution_mode IN ('one_time', 'bounded_period', 'follow_up_task')
  ),
  start_required BOOLEAN NOT NULL,
  direct_complete_allowed BOOLEAN NOT NULL,
  rendering_version TEXT NOT NULL CHECK (
    rendering_version = 'terminal-care-plan.zh-CN.v1'
  ),
  plan_version INTEGER NOT NULL DEFAULT 1 CHECK (plan_version = 1),
  plan_json JSONB NOT NULL CHECK (
    jsonb_typeof(plan_json) = 'object'
    AND plan_json ->> 'schema_version' = 'care_plan_entry.v1'
  ),
  created_at TIMESTAMPTZ NOT NULL,
  valid_from TIMESTAMPTZ NOT NULL,
  valid_until TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
    REFERENCES public.backend_namespace_generations (
      namespace_id, data_mode, generation
    ) ON DELETE RESTRICT,
  FOREIGN KEY (source_analysis_revision_id, namespace_id, data_mode)
    REFERENCES public.sleep_domain_analysis_revisions (
      analysis_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (source_night_finalization_revision_id)
    REFERENCES public.sleep_domain_night_finalization_revisions (
      night_finalization_revision_id
    ) ON DELETE RESTRICT,
  CHECK (valid_from <= created_at AND created_at < valid_until),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  ),
  CHECK (plan_json ->> 'care_plan_id' = care_plan_id),
  CHECK (plan_json ->> 'care_plan_semantic_hash' = care_plan_semantic_sha256),
  CHECK (plan_json ->> 'approval_grant_id' = approval_grant_id),
  CHECK (plan_json ->> 'proposal_id' = proposal_id),
  CHECK (plan_json ->> 'subject_id' = subject_id),
  CHECK (plan_json ->> 'action_type' = action_type),
  CHECK (plan_json ->> 'executor_role' = executor_role),
  CHECK (plan_json ->> 'source_authority' IS NULL),
  CHECK (NOT (structured_action_parameters_json ?| ARRAY[
    'email', 'email_address', 'phone', 'phone_number', 'destination',
    'recipient', 'recipient_address', 'channel', 'smtp', 'sms',
    'alarm', 'alarm_stop', 'device_control'
  ]))
);

CREATE INDEX idx_backend_care_plan_subject_v1
  ON public.backend_care_plans_v1 (
    namespace_id, data_mode, subject_id, valid_until, created_at, care_plan_id
  );

CREATE TABLE public.backend_care_execution_states_v1 (
  care_plan_id TEXT PRIMARY KEY
    REFERENCES public.backend_care_plans_v1(care_plan_id) ON DELETE RESTRICT,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  executor_role TEXT NOT NULL CHECK (executor_role IN ('elder', 'family')),
  state TEXT NOT NULL CHECK (state IN (
    'not_started', 'in_progress', 'completed', 'cancelled',
    'expired', 'invalidated', 'superseded'
  )),
  version BIGINT NOT NULL CHECK (version >= 1),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  cancelled_at TIMESTAMPTZ,
  invalidated_at TIMESTAMPTZ,
  invalidation_reason TEXT CHECK (length(invalidation_reason) <= 100),
  command_conflict_count BIGINT NOT NULL DEFAULT 0 CHECK (
    command_conflict_count >= 0
  ),
  updated_at TIMESTAMPTZ NOT NULL,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  ),
  CHECK ((state <> 'in_progress') OR started_at IS NOT NULL),
  CHECK ((state <> 'completed') OR completed_at IS NOT NULL),
  CHECK ((state <> 'cancelled') OR cancelled_at IS NOT NULL),
  CHECK (
    (state NOT IN ('expired', 'invalidated', 'superseded'))
    OR (invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL)
  )
);

CREATE INDEX idx_backend_care_execution_state_v1
  ON public.backend_care_execution_states_v1 (
    namespace_id, data_mode, state, updated_at, care_plan_id
  );

CREATE TABLE public.backend_care_execution_events_v1 (
  event_id TEXT PRIMARY KEY,
  care_plan_id TEXT NOT NULL
    REFERENCES public.backend_care_plans_v1(care_plan_id) ON DELETE RESTRICT,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  executor_role TEXT NOT NULL CHECK (executor_role IN ('elder', 'family')),
  event_type TEXT NOT NULL CHECK (
    event_type IN ('started', 'completed', 'cancelled')
  ),
  actor_principal_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (actor_role IN ('elder', 'family')),
  actor_binding_id TEXT NOT NULL
    REFERENCES public.backend_actor_subject_bindings(binding_id)
    ON DELETE RESTRICT,
  source_authority TEXT NOT NULL CHECK (source_authority = 'human_attested'),
  occurred_at TIMESTAMPTZ NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL CHECK (recorded_at >= occurred_at),
  idempotency_key TEXT NOT NULL CHECK (
    length(idempotency_key) BETWEEN 1 AND 200
  ),
  command_fingerprint TEXT NOT NULL CHECK (
    command_fingerprint ~ '^[0-9a-f]{64}$'
  ),
  note TEXT CHECK (length(note) <= 500),
  authorization_epoch BIGINT NOT NULL CHECK (authorization_epoch >= 1),
  previous_state TEXT NOT NULL CHECK (previous_state IN (
    'not_started', 'in_progress'
  )),
  resulting_state TEXT NOT NULL CHECK (resulting_state IN (
    'in_progress', 'completed', 'cancelled'
  )),
  previous_version BIGINT NOT NULL CHECK (previous_version >= 1),
  resulting_version BIGINT NOT NULL CHECK (
    resulting_version = previous_version + 1
  ),
  event_json JSONB NOT NULL CHECK (
    jsonb_typeof(event_json) = 'object'
    AND event_json ->> 'schema_version' = 'care_execution_event.v1'
    AND event_json ->> 'source_authority' = 'human_attested'
  ),
  UNIQUE (care_plan_id, idempotency_key),
  CHECK (actor_role = executor_role),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX idx_backend_care_execution_event_history_v1
  ON public.backend_care_execution_events_v1 (
    care_plan_id, occurred_at, recorded_at, event_id
  );

CREATE OR REPLACE FUNCTION public.sleepagent_care_execution_append_only_v1()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'Care plan and execution evidence are append-only';
END;
$$;

CREATE TRIGGER sleepagent_care_plan_immutable_v1_trigger
BEFORE UPDATE OR DELETE ON public.backend_care_plans_v1
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_care_execution_append_only_v1();

CREATE TRIGGER sleepagent_care_execution_event_immutable_v1_trigger
BEFORE UPDATE OR DELETE ON public.backend_care_execution_events_v1
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_care_execution_append_only_v1();

CREATE OR REPLACE FUNCTION public.sleepagent_validate_care_execution_state_v1()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF ROW(
    NEW.care_plan_id, NEW.namespace_id, NEW.data_mode,
    NEW.namespace_generation, NEW.run_id, NEW.arm_id,
    NEW.subject_id, NEW.executor_role
  ) IS DISTINCT FROM ROW(
    OLD.care_plan_id, OLD.namespace_id, OLD.data_mode,
    OLD.namespace_generation, OLD.run_id, OLD.arm_id,
    OLD.subject_id, OLD.executor_role
  ) THEN
    RAISE EXCEPTION 'Care execution identity is immutable';
  END IF;

  IF NEW.state = OLD.state AND NEW.version = OLD.version THEN
    IF NEW.command_conflict_count <> OLD.command_conflict_count + 1
       OR ROW(
         NEW.started_at, NEW.completed_at, NEW.cancelled_at,
         NEW.invalidated_at, NEW.invalidation_reason, NEW.updated_at
       ) IS DISTINCT FROM ROW(
         OLD.started_at, OLD.completed_at, OLD.cancelled_at,
         OLD.invalidated_at, OLD.invalidation_reason, OLD.updated_at
       ) THEN
      RAISE EXCEPTION 'Care execution conflict counter update is invalid';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.version <> OLD.version + 1 OR NEW.updated_at < OLD.updated_at THEN
    RAISE EXCEPTION 'Care execution CAS version/time is invalid';
  END IF;
  IF NOT (
    (OLD.state = 'not_started' AND NEW.state IN (
      'in_progress', 'completed', 'cancelled', 'expired',
      'invalidated', 'superseded'
    ))
    OR (OLD.state = 'in_progress' AND NEW.state IN (
      'completed', 'cancelled', 'expired', 'invalidated', 'superseded'
    ))
  ) THEN
    RAISE EXCEPTION 'Care execution transition is invalid';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER sleepagent_validate_care_execution_state_v1_trigger
BEFORE UPDATE ON public.backend_care_execution_states_v1
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_validate_care_execution_state_v1();

CREATE TRIGGER sleepagent_care_execution_state_no_delete_v1_trigger
BEFORE DELETE ON public.backend_care_execution_states_v1
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_care_execution_append_only_v1();

CREATE OR REPLACE FUNCTION public.sleepagent_ensure_care_plan_v1(
  target_grant_id TEXT,
  requested_at TIMESTAMPTZ
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  grant_row public.backend_approval_grants_v3%ROWTYPE;
  proposal public.backend_care_action_proposals_v3%ROWTYPE;
  night_key TEXT;
  plan_id TEXT;
  plan_sha TEXT;
  policy_sha TEXT;
  mode_value TEXT;
  start_value BOOLEAN;
  direct_value BOOLEAN;
  plan_payload JSONB;
  existing_sha TEXT;
BEGIN
  SELECT item.* INTO grant_row
  FROM public.backend_approval_grants_v3 AS item
  WHERE item.grant_id = target_grant_id;
  IF NOT FOUND OR grant_row.state <> 'active'
     OR grant_row.expires_at <= requested_at THEN
    RAISE EXCEPTION 'CarePlan requires an active ApprovalGrant';
  END IF;

  SELECT item.* INTO proposal
  FROM public.backend_care_action_proposals_v3 AS item
  WHERE item.proposal_id = grant_row.proposal_id;
  IF NOT FOUND OR proposal.state <> 'approved'
     OR proposal.subject_id <> grant_row.subject_id
     OR proposal.action_type <> grant_row.action_type
     OR proposal.audience_role <> grant_row.audience_role
     OR proposal.proposal_semantic_sha256 <>
       grant_row.proposal_semantic_sha256
     OR proposal.candidate_sha256 <> grant_row.candidate_sha256 THEN
    RAISE EXCEPTION 'CarePlan grant/proposal authority mismatch';
  END IF;

  SELECT episode.night_key INTO night_key
  FROM public.sleep_domain_night_episodes AS episode
  WHERE episode.night_episode_id = proposal.night_episode_id
    AND episode.namespace_id = proposal.namespace_id
    AND episode.data_mode = proposal.data_mode;
  IF night_key IS NULL THEN
    RAISE EXCEPTION 'CarePlan source night is unavailable';
  END IF;

  CASE grant_row.action_type
    WHEN 'recommend_consistent_wake_time' THEN
      policy_sha := 'aa6d2fe4416b4b42417d4c9f17b136438db4d8e89dbb1bf87a66c259aca6e7fc';
      mode_value := 'bounded_period'; start_value := TRUE; direct_value := FALSE;
      IF jsonb_typeof(proposal.candidate_json -> 'parameters' -> 'tolerance_minutes')
           <> 'number'
         OR (proposal.candidate_json -> 'parameters' ->> 'tolerance_minutes')::NUMERIC
           NOT BETWEEN 0 AND 60 THEN
        RAISE EXCEPTION 'CarePlan wake-time parameters are invalid';
      END IF;
    WHEN 'recommend_morning_light' THEN
      policy_sha := '18eb98e840dbbed8a114f693c93a7cf179a55bc8f3f471b4d43503e9b928fd93';
      mode_value := 'one_time'; start_value := FALSE; direct_value := TRUE;
      IF jsonb_typeof(proposal.candidate_json -> 'parameters' -> 'minutes')
           <> 'number'
         OR (proposal.candidate_json -> 'parameters' ->> 'minutes')::NUMERIC
           NOT BETWEEN 5 AND 45 THEN
        RAISE EXCEPTION 'CarePlan morning-light parameters are invalid';
      END IF;
    WHEN 'request_manual_follow_up' THEN
      policy_sha := '86faf1d01de0bf2f33dbd7305e84868949a71e72c09dab57670acd66874ca1ce';
      mode_value := 'follow_up_task'; start_value := FALSE; direct_value := TRUE;
    WHEN 'request_morning_review_feedback' THEN
      policy_sha := '551c665471e234b296992161646d88f203d0cd8e305f605310f784dfcfc75313';
      mode_value := 'follow_up_task'; start_value := FALSE; direct_value := TRUE;
    ELSE
      RAISE EXCEPTION 'CarePlan action is outside the closed taxonomy';
  END CASE;

  plan_id := 'care-plan:' || substring(encode(digest(
    grant_row.grant_id, 'sha256'
  ), 'hex') FROM 1 FOR 32);
  plan_sha := encode(digest(
    grant_row.grant_sha256 || chr(31) || grant_row.candidate_sha256 || chr(31) ||
    grant_row.action_type || chr(31) || policy_sha || chr(31) ||
    'terminal-care-plan.zh-CN.v1', 'sha256'
  ), 'hex');
  plan_payload := jsonb_build_object(
    'schema_version', 'care_plan_entry.v1',
    'care_plan_id', plan_id,
    'care_plan_semantic_hash', plan_sha,
    'approval_grant_id', grant_row.grant_id,
    'approval_grant_hash', grant_row.grant_sha256,
    'proposal_id', grant_row.proposal_id,
    'proposal_semantic_hash', grant_row.proposal_semantic_sha256,
    'candidate_hash', grant_row.candidate_sha256,
    'subject_id', grant_row.subject_id,
    'action_type', grant_row.action_type,
    'executor_role', grant_row.audience_role,
    'source_analysis_revision_id', proposal.source_analysis_revision_id,
    'source_shared_analysis_sha256', proposal.source_shared_analysis_sha256,
    'source_night_finalization_revision_id',
      proposal.source_night_finalization_revision_id,
    'source_care_strategy_invocation_id',
      proposal.source_care_strategy_invocation_id,
    'source_care_strategy_version',
      proposal.candidate_json ->> 'source_care_strategy_version',
    'source_night_key', night_key,
    'evidence_references',
      proposal.candidate_json -> 'rationale_evidence_refs',
    'structured_action_parameters', proposal.candidate_json -> 'parameters',
    'approval_policy_version', grant_row.policy_version,
    'approval_policy_hash', grant_row.policy_sha256,
    'approval_authorization_scope', grant_row.authorization_scope,
    'approval_authorization_epoch', grant_row.authorization_epoch,
    'execution_policy_version', 'terminal-care-execution.v1',
    'execution_policy_hash', policy_sha,
    'execution_mode', mode_value,
    'start_required', start_value,
    'direct_complete_allowed', direct_value,
    'rendering_version', 'terminal-care-plan.zh-CN.v1',
    'created_at', GREATEST(requested_at, grant_row.issued_at),
    'valid_from', grant_row.issued_at,
    'valid_until', LEAST(grant_row.expires_at, proposal.expires_at),
    'version', 1
  );

  INSERT INTO public.backend_care_plans_v1 (
    care_plan_id, care_plan_semantic_sha256, approval_grant_id,
    approval_grant_sha256, proposal_id, proposal_semantic_sha256,
    candidate_sha256, namespace_id, data_mode, namespace_generation,
    run_id, arm_id, subject_id, action_type, executor_role,
    source_analysis_revision_id, source_shared_analysis_sha256,
    source_night_finalization_revision_id,
    source_care_strategy_invocation_id, source_care_strategy_version,
    source_night_key, evidence_references_json,
    structured_action_parameters_json, approval_policy_version,
    approval_policy_sha256, approval_authorization_scope,
    approval_authorization_epoch, execution_policy_version,
    execution_policy_sha256, execution_mode, start_required,
    direct_complete_allowed, rendering_version, plan_version, plan_json,
    created_at, valid_from, valid_until
  ) VALUES (
    plan_id, plan_sha, grant_row.grant_id, grant_row.grant_sha256,
    grant_row.proposal_id, grant_row.proposal_semantic_sha256,
    grant_row.candidate_sha256, grant_row.namespace_id, grant_row.data_mode,
    grant_row.namespace_generation, grant_row.run_id, grant_row.arm_id,
    grant_row.subject_id, grant_row.action_type, grant_row.audience_role,
    proposal.source_analysis_revision_id,
    proposal.source_shared_analysis_sha256,
    proposal.source_night_finalization_revision_id,
    proposal.source_care_strategy_invocation_id,
    proposal.candidate_json ->> 'source_care_strategy_version', night_key,
    proposal.candidate_json -> 'rationale_evidence_refs',
    proposal.candidate_json -> 'parameters', grant_row.policy_version,
    grant_row.policy_sha256, grant_row.authorization_scope,
    grant_row.authorization_epoch, 'terminal-care-execution.v1', policy_sha,
    mode_value, start_value, direct_value, 'terminal-care-plan.zh-CN.v1',
    1, plan_payload, GREATEST(requested_at, grant_row.issued_at),
    grant_row.issued_at,
    LEAST(grant_row.expires_at, proposal.expires_at)
  ) ON CONFLICT (approval_grant_id) DO NOTHING;

  SELECT item.care_plan_semantic_sha256 INTO existing_sha
  FROM public.backend_care_plans_v1 AS item
  WHERE item.approval_grant_id = grant_row.grant_id;
  IF existing_sha IS DISTINCT FROM plan_sha THEN
    RAISE EXCEPTION 'CarePlan semantic identity collision';
  END IF;

  INSERT INTO public.backend_care_execution_states_v1 (
    care_plan_id, namespace_id, data_mode, namespace_generation,
    run_id, arm_id, subject_id, executor_role, state, version, updated_at
  ) VALUES (
    plan_id, grant_row.namespace_id, grant_row.data_mode,
    grant_row.namespace_generation, grant_row.run_id, grant_row.arm_id,
    grant_row.subject_id, grant_row.audience_role, 'not_started', 1,
    requested_at
  ) ON CONFLICT (care_plan_id) DO NOTHING;
  RETURN plan_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.sleepagent_create_care_plan_from_grant_v1()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  PERFORM public.sleepagent_ensure_care_plan_v1(NEW.grant_id, NEW.issued_at);
  RETURN NEW;
END;
$$;

CREATE TRIGGER sleepagent_create_care_plan_from_grant_v1_trigger
AFTER INSERT ON public.backend_approval_grants_v3
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_create_care_plan_from_grant_v1();

-- Bounded idempotent upgrade backfill for grants that remain valid at deploy.
DO $$
DECLARE item RECORD; migration_time TIMESTAMPTZ := clock_timestamp();
BEGIN
  FOR item IN
    SELECT grant_id FROM public.backend_approval_grants_v3
    WHERE state = 'active' AND expires_at > migration_time
    ORDER BY grant_id
  LOOP
    PERFORM public.sleepagent_ensure_care_plan_v1(
      item.grant_id, migration_time
    );
  END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION public.sleepagent_project_care_authority_change_v1()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE target_state TEXT; reason TEXT; changed_at TIMESTAMPTZ;
BEGIN
  IF TG_TABLE_NAME = 'backend_approval_grants_v3' THEN
    IF OLD.state = NEW.state OR NEW.state NOT IN ('revoked', 'expired') THEN
      RETURN NEW;
    END IF;
    target_state := CASE NEW.state WHEN 'expired' THEN 'expired' ELSE 'invalidated' END;
    reason := CASE NEW.state
      WHEN 'expired' THEN 'approval_grant_expired'
      ELSE COALESCE(NEW.revocation_reason_code, 'approval_grant_revoked')
    END;
    changed_at := COALESCE(NEW.revoked_at, clock_timestamp());
    UPDATE public.backend_care_execution_states_v1 AS state_row
    SET state = target_state, version = state_row.version + 1,
        invalidated_at = changed_at, invalidation_reason = reason,
        updated_at = GREATEST(state_row.updated_at, changed_at)
    FROM public.backend_care_plans_v1 AS plan
    WHERE plan.approval_grant_id = NEW.grant_id
      AND state_row.care_plan_id = plan.care_plan_id
      AND state_row.state IN ('not_started', 'in_progress');
  ELSE
    IF OLD.state = NEW.state OR NEW.state NOT IN ('expired', 'revoked') THEN
      RETURN NEW;
    END IF;
    target_state := CASE
      WHEN NEW.state = 'expired' AND NEW.expires_at > clock_timestamp()
        THEN 'superseded'
      WHEN NEW.state = 'expired' THEN 'expired'
      ELSE 'invalidated'
    END;
    reason := CASE target_state
      WHEN 'superseded' THEN 'source_analysis_superseded'
      WHEN 'expired' THEN 'proposal_expired'
      ELSE 'proposal_revoked'
    END;
    changed_at := NEW.updated_at;
    UPDATE public.backend_care_execution_states_v1 AS state_row
    SET state = target_state, version = state_row.version + 1,
        invalidated_at = changed_at, invalidation_reason = reason,
        updated_at = GREATEST(state_row.updated_at, changed_at)
    FROM public.backend_care_plans_v1 AS plan
    WHERE plan.proposal_id = NEW.proposal_id
      AND state_row.care_plan_id = plan.care_plan_id
      AND state_row.state IN ('not_started', 'in_progress');
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER sleepagent_project_grant_authority_change_v1_trigger
AFTER UPDATE OF state ON public.backend_approval_grants_v3
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_project_care_authority_change_v1();

CREATE TRIGGER sleepagent_project_proposal_authority_change_v1_trigger
AFTER UPDATE OF state ON public.backend_care_action_proposals_v3
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_project_care_authority_change_v1();

CREATE OR REPLACE FUNCTION public.sleepagent_execute_care_plan_v1(
  target_care_plan_id TEXT,
  expected_version BIGINT,
  requested_event_type TEXT,
  requested_idempotency_key TEXT,
  requested_binding_id TEXT,
  requested_note TEXT,
  occurred_at TIMESTAMPTZ
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  plan public.backend_care_plans_v1%ROWTYPE;
  execution public.backend_care_execution_states_v1%ROWTYPE;
  grant_row public.backend_approval_grants_v3%ROWTYPE;
  proposal public.backend_care_action_proposals_v3%ROWTYPE;
  binding public.backend_actor_subject_bindings%ROWTYPE;
  existing public.backend_care_execution_events_v1%ROWTYPE;
  command_at TIMESTAMPTZ := clock_timestamp();
  command_sha TEXT;
  event_id TEXT;
  next_state TEXT;
  next_version BIGINT;
  event_payload JSONB;
BEGIN
  IF requested_event_type NOT IN ('started', 'completed', 'cancelled')
     OR requested_idempotency_key IS NULL
     OR length(requested_idempotency_key) NOT BETWEEN 1 AND 200
     OR length(COALESCE(requested_note, '')) > 500
     OR requested_note ~ '[[:cntrl:]]'
     OR occurred_at IS NULL
     OR occurred_at > command_at
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'api'
     OR NULLIF(current_setting('sleepagent.purpose', TRUE), '') <> 'sleep_care'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'care_execution_invalid_context';
  END IF;

  SELECT item.* INTO plan
  FROM public.backend_care_plans_v1 AS item
  WHERE item.care_plan_id = target_care_plan_id
    AND item.namespace_id = NULLIF(current_setting('sleepagent.namespace_id', TRUE), '')
    AND item.data_mode = NULLIF(current_setting('sleepagent.data_mode', TRUE), '')
    AND item.namespace_generation =
      current_setting('sleepagent.namespace_generation')::BIGINT
    AND item.run_id IS NOT DISTINCT FROM
      NULLIF(current_setting('sleepagent.run_id', TRUE), '')
    AND item.arm_id IS NOT DISTINCT FROM
      NULLIF(current_setting('sleepagent.arm_id', TRUE), '')
    AND item.subject_id = NULLIF(current_setting('sleepagent.subject_id', TRUE), '')
  FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'care_plan_not_found_or_unauthorized'; END IF;

  SELECT item.* INTO execution
  FROM public.backend_care_execution_states_v1 AS item
  WHERE item.care_plan_id = plan.care_plan_id
  FOR UPDATE;
  SELECT item.* INTO grant_row
  FROM public.backend_approval_grants_v3 AS item
  WHERE item.grant_id = plan.approval_grant_id;
  SELECT item.* INTO proposal
  FROM public.backend_care_action_proposals_v3 AS item
  WHERE item.proposal_id = plan.proposal_id;

  SELECT item.* INTO binding
  FROM public.backend_actor_subject_bindings AS item
  WHERE item.binding_id = requested_binding_id
    AND item.namespace_id = plan.namespace_id
    AND item.data_mode = plan.data_mode
    AND item.subject_id = plan.subject_id
    AND item.actor_id = NULLIF(current_setting('sleepagent.actor_id', TRUE), '')
    AND item.role = NULLIF(current_setting('sleepagent.actor_role', TRUE), '')
    AND item.role = plan.executor_role
    AND item.status = 'active'
    AND item.authorization_epoch =
      current_setting('sleepagent.authorization_epoch')::BIGINT
    AND item.scopes_json ? 'product:sleep:care:execute'
    AND item.valid_from <= command_at
    AND (item.valid_until IS NULL OR item.valid_until > command_at);
  IF NOT FOUND THEN RAISE EXCEPTION 'care_executor_unauthorized'; END IF;

  command_sha := encode(digest(
    plan.care_plan_id || chr(31) || requested_event_type || chr(31) ||
    binding.actor_id || chr(31) || COALESCE(requested_note, ''), 'sha256'
  ), 'hex');
  SELECT item.* INTO existing
  FROM public.backend_care_execution_events_v1 AS item
  WHERE item.care_plan_id = plan.care_plan_id
    AND item.idempotency_key = requested_idempotency_key;
  IF FOUND THEN
    IF existing.command_fingerprint = command_sha
       AND existing.actor_id = binding.actor_id THEN
      RETURN jsonb_build_object(
        'outcome', 'idempotent', 'care_plan_id', plan.care_plan_id,
        'state', existing.resulting_state,
        'version', existing.resulting_version,
        'event_id', existing.event_id
      );
    END IF;
    UPDATE public.backend_care_execution_states_v1
    SET command_conflict_count = command_conflict_count + 1
    WHERE care_plan_id = plan.care_plan_id;
    RETURN jsonb_build_object(
      'outcome', 'conflict', 'care_plan_id', plan.care_plan_id,
      'state', execution.state, 'version', execution.version
    );
  END IF;

  IF grant_row.state <> 'active' OR proposal.state <> 'approved' THEN
    IF execution.state IN ('not_started', 'in_progress') THEN
      next_state := CASE
        WHEN proposal.state = 'expired' AND proposal.expires_at > command_at
          THEN 'superseded'
        WHEN grant_row.state = 'expired' OR proposal.state = 'expired'
          THEN 'expired'
        ELSE 'invalidated'
      END;
      UPDATE public.backend_care_execution_states_v1
      SET state = next_state, version = version + 1,
          invalidated_at = command_at,
          invalidation_reason = CASE next_state
            WHEN 'superseded' THEN 'source_analysis_superseded'
            WHEN 'expired' THEN 'approval_authority_expired'
            ELSE 'approval_authority_invalidated'
          END,
          updated_at = command_at
      WHERE care_plan_id = plan.care_plan_id;
    END IF;
    RETURN jsonb_build_object(
      'outcome', CASE
        WHEN proposal.state = 'expired' AND proposal.expires_at > command_at
          THEN 'superseded'
        WHEN grant_row.state = 'expired' OR proposal.state = 'expired'
          THEN 'expired'
        ELSE 'invalidated'
      END,
      'care_plan_id', plan.care_plan_id,
      'state', CASE WHEN execution.state IN ('completed', 'cancelled')
        THEN execution.state ELSE COALESCE(next_state, execution.state) END,
      'version', CASE WHEN execution.state IN ('completed', 'cancelled')
        THEN execution.version ELSE execution.version + 1 END
    );
  END IF;

  IF command_at >= plan.valid_until OR occurred_at < plan.valid_from
     OR occurred_at >= plan.valid_until THEN
    IF execution.state IN ('not_started', 'in_progress') THEN
      UPDATE public.backend_care_execution_states_v1
      SET state = 'expired', version = version + 1,
          invalidated_at = command_at,
          invalidation_reason = 'execution_window_expired',
          updated_at = command_at
      WHERE care_plan_id = plan.care_plan_id;
    END IF;
    RETURN jsonb_build_object(
      'outcome', 'expired', 'care_plan_id', plan.care_plan_id,
      'state', CASE WHEN execution.state IN ('completed', 'cancelled')
        THEN execution.state ELSE 'expired' END,
      'version', CASE WHEN execution.state IN ('completed', 'cancelled')
        THEN execution.version ELSE execution.version + 1 END
    );
  END IF;

  IF execution.version <> expected_version THEN
    UPDATE public.backend_care_execution_states_v1
    SET command_conflict_count = command_conflict_count + 1
    WHERE care_plan_id = plan.care_plan_id;
    RETURN jsonb_build_object(
      'outcome', 'conflict', 'care_plan_id', plan.care_plan_id,
      'state', execution.state, 'version', execution.version
    );
  END IF;

  IF requested_event_type = 'started' AND execution.state = 'not_started' THEN
    next_state := 'in_progress';
  ELSIF requested_event_type = 'completed'
        AND execution.state = 'in_progress' THEN
    next_state := 'completed';
  ELSIF requested_event_type = 'completed'
        AND execution.state = 'not_started'
        AND plan.direct_complete_allowed THEN
    next_state := 'completed';
  ELSIF requested_event_type = 'cancelled'
        AND execution.state IN ('not_started', 'in_progress') THEN
    next_state := 'cancelled';
  ELSE
    UPDATE public.backend_care_execution_states_v1
    SET command_conflict_count = command_conflict_count + 1
    WHERE care_plan_id = plan.care_plan_id;
    RETURN jsonb_build_object(
      'outcome', 'conflict', 'care_plan_id', plan.care_plan_id,
      'state', execution.state, 'version', execution.version
    );
  END IF;

  next_version := execution.version + 1;
  event_id := 'care-execution-event:' || encode(digest(
    plan.care_plan_id || chr(31) || requested_idempotency_key, 'sha256'
  ), 'hex');
  event_payload := jsonb_build_object(
    'schema_version', 'care_execution_event.v1',
    'event_id', event_id,
    'care_plan_id', plan.care_plan_id,
    'event_type', requested_event_type,
    'actor_principal_id',
      NULLIF(current_setting('sleepagent.service_principal_id', TRUE), ''),
    'actor_id', binding.actor_id,
    'actor_role', binding.role,
    'actor_binding_id', binding.binding_id,
    'subject_id', plan.subject_id,
    'source_authority', 'human_attested',
    'occurred_at', occurred_at,
    'recorded_at', command_at,
    'idempotency_key', requested_idempotency_key,
    'command_fingerprint', command_sha,
    'note', NULLIF(requested_note, ''),
    'authorization_epoch', binding.authorization_epoch,
    'previous_state', execution.state,
    'resulting_state', next_state,
    'previous_version', execution.version,
    'resulting_version', next_version
  );

  INSERT INTO public.backend_care_execution_events_v1 (
    event_id, care_plan_id, namespace_id, data_mode, namespace_generation,
    run_id, arm_id, subject_id, executor_role, event_type,
    actor_principal_id, actor_id, actor_role, actor_binding_id,
    source_authority, occurred_at, recorded_at, idempotency_key,
    command_fingerprint, note, authorization_epoch, previous_state,
    resulting_state, previous_version, resulting_version, event_json
  ) VALUES (
    event_id, plan.care_plan_id, plan.namespace_id, plan.data_mode,
    plan.namespace_generation, plan.run_id, plan.arm_id, plan.subject_id,
    plan.executor_role, requested_event_type,
    NULLIF(current_setting('sleepagent.service_principal_id', TRUE), ''),
    binding.actor_id, binding.role, binding.binding_id, 'human_attested',
    occurred_at, command_at, requested_idempotency_key, command_sha,
    NULLIF(requested_note, ''), binding.authorization_epoch,
    execution.state, next_state, execution.version, next_version,
    event_payload
  );

  UPDATE public.backend_care_execution_states_v1
  SET state = next_state, version = next_version,
      started_at = CASE WHEN requested_event_type = 'started'
        THEN occurred_at ELSE started_at END,
      completed_at = CASE WHEN requested_event_type = 'completed'
        THEN occurred_at ELSE completed_at END,
      cancelled_at = CASE WHEN requested_event_type = 'cancelled'
        THEN occurred_at ELSE cancelled_at END,
      updated_at = command_at
  WHERE care_plan_id = plan.care_plan_id AND version = execution.version;
  IF NOT FOUND THEN RAISE EXCEPTION 'care_execution_cas_lost'; END IF;

  RETURN jsonb_build_object(
    'outcome', 'applied', 'care_plan_id', plan.care_plan_id,
    'state', next_state, 'version', next_version, 'event_id', event_id
  );
END;
$$;

ALTER TABLE public.backend_care_plans_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.backend_care_plans_v1 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_care_plan_scope_v1 ON public.backend_care_plans_v1
  USING (
    public.sleepagent_scope_setting('sleepagent.purpose') = 'internal_status'
    OR (
      public.sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
      )
      AND (
        public.sleepagent_scope_setting('sleepagent.process_role') = 'worker'
        OR executor_role = public.sleepagent_scope_setting('sleepagent.actor_role')
      )
    )
  );

ALTER TABLE public.backend_care_execution_states_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.backend_care_execution_states_v1 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_care_execution_state_scope_v1
  ON public.backend_care_execution_states_v1
  USING (
    public.sleepagent_scope_setting('sleepagent.purpose') = 'internal_status'
    OR (
      public.sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
      )
      AND (
        public.sleepagent_scope_setting('sleepagent.process_role') = 'worker'
        OR executor_role = public.sleepagent_scope_setting('sleepagent.actor_role')
      )
    )
  );

ALTER TABLE public.backend_care_execution_events_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.backend_care_execution_events_v1 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_care_execution_event_scope_v1
  ON public.backend_care_execution_events_v1
  USING (
    public.sleepagent_scope_setting('sleepagent.purpose') = 'internal_status'
    OR (
      public.sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
      )
      AND (
        public.sleepagent_scope_setting('sleepagent.process_role') = 'worker'
        OR executor_role = public.sleepagent_scope_setting('sleepagent.actor_role')
      )
    )
  );

CREATE OR REPLACE FUNCTION public.sleepagent_care_execution_operational_metrics_v1()
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE result JSONB;
BEGIN
  IF NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'api'
     OR NULLIF(current_setting('sleepagent.purpose', TRUE), '') <> 'internal_status'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid care execution metrics context';
  END IF;
  SELECT jsonb_build_object(
    'schema_version', 'care_execution_operational_metrics.v1',
    'active_care_plans', count(*) FILTER (
      WHERE state.state IN ('not_started', 'in_progress')
        AND plan.valid_until > clock_timestamp()
        AND grant_row.state = 'active' AND proposal.state = 'approved'
    ),
    'not_started_care_plans', count(*) FILTER (
      WHERE state.state = 'not_started' AND plan.valid_until > clock_timestamp()
        AND grant_row.state = 'active' AND proposal.state = 'approved'
    ),
    'in_progress_care_plans', count(*) FILTER (
      WHERE state.state = 'in_progress' AND plan.valid_until > clock_timestamp()
        AND grant_row.state = 'active' AND proposal.state = 'approved'
    ),
    'completed_care_plans', count(*) FILTER (WHERE state.state = 'completed'),
    'cancelled_care_plans', count(*) FILTER (WHERE state.state = 'cancelled'),
    'expired_invalidated_care_plans', count(*) FILTER (
      WHERE state.state IN ('expired', 'invalidated', 'superseded')
        OR (state.state IN ('not_started', 'in_progress')
          AND (plan.valid_until <= clock_timestamp()
            OR grant_row.state <> 'active' OR proposal.state <> 'approved'))
    ),
    'oldest_executable_plan_age_seconds', COALESCE(max(
      EXTRACT(EPOCH FROM (clock_timestamp() - plan.created_at))
    ) FILTER (
      WHERE state.state IN ('not_started', 'in_progress')
        AND plan.valid_until > clock_timestamp()
        AND grant_row.state = 'active' AND proposal.state = 'approved'
    ), 0),
    'execution_command_conflict_error_count',
      COALESCE(sum(state.command_conflict_count), 0)
  ) INTO result
  FROM public.backend_care_plans_v1 AS plan
  JOIN public.backend_care_execution_states_v1 AS state
    ON state.care_plan_id = plan.care_plan_id
  JOIN public.backend_approval_grants_v3 AS grant_row
    ON grant_row.grant_id = plan.approval_grant_id
  JOIN public.backend_care_action_proposals_v3 AS proposal
    ON proposal.proposal_id = plan.proposal_id
  WHERE plan.data_mode = NULLIF(current_setting('sleepagent.data_mode', TRUE), '');
  RETURN result;
END;
$$;

REVOKE ALL ON FUNCTION public.sleepagent_care_execution_append_only_v1()
  FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_validate_care_execution_state_v1()
  FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_ensure_care_plan_v1(TEXT, TIMESTAMPTZ)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_create_care_plan_from_grant_v1()
  FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_project_care_authority_change_v1()
  FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_execute_care_plan_v1(
  TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT, TIMESTAMPTZ
) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_care_execution_operational_metrics_v1()
  FROM PUBLIC;
