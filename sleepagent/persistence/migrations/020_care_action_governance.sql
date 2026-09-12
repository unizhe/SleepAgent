-- sleepagent:transactional=true

-- G8: durable governed CareAction proposal, human decision, and inert grant.
-- This migration deliberately creates no DeliveryIntent and no effect handler.

CREATE TABLE public.backend_care_action_proposals_v3 (
  proposal_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  source_analysis_revision_id TEXT NOT NULL,
  source_shared_analysis_sha256 TEXT NOT NULL CHECK (
    source_shared_analysis_sha256 ~ '^[0-9a-f]{64}$'
  ),
  source_night_finalization_revision_id TEXT NOT NULL,
  source_care_strategy_invocation_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  candidate_sha256 TEXT NOT NULL CHECK (candidate_sha256 ~ '^[0-9a-f]{64}$'),
  proposal_semantic_key TEXT NOT NULL CHECK (
    proposal_semantic_key ~ '^[0-9a-f]{64}$'
  ),
  proposal_semantic_sha256 TEXT NOT NULL CHECK (
    proposal_semantic_sha256 ~ '^[0-9a-f]{64}$'
  ),
  action_type TEXT NOT NULL CHECK (action_type IN (
    'recommend_consistent_wake_time', 'recommend_morning_light',
    'request_manual_follow_up', 'request_morning_review_feedback'
  )),
  catalog_action_id TEXT NOT NULL CHECK (catalog_action_id IN (
    'consistent-wake-time', 'morning-light',
    'nighttime-gentle-support', 'morning-review-feedback'
  )),
  catalog_action_version INTEGER NOT NULL CHECK (catalog_action_version = 1),
  audience_role TEXT NOT NULL CHECK (audience_role IN ('elder', 'family', 'doctor')),
  required_approver_role TEXT NOT NULL CHECK (
    required_approver_role IN ('elder', 'family', 'doctor')
  ),
  authorization_scope TEXT NOT NULL,
  urgency TEXT NOT NULL CHECK (urgency IN ('normal', 'watch')),
  policy_version TEXT NOT NULL,
  policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  policy_decision TEXT NOT NULL CHECK (policy_decision = 'eligible'),
  policy_reason_code TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN (
    'proposed', 'awaiting_approval', 'approved', 'rejected', 'expired', 'revoked'
  )),
  version BIGINT NOT NULL CHECK (version >= 1),
  candidate_json JSONB NOT NULL CHECK (
    jsonb_typeof(candidate_json) = 'object'
    AND candidate_json ->> 'schema_version' = 'care_action_candidate.v2'
  ),
  proposal_json JSONB NOT NULL CHECK (
    jsonb_typeof(proposal_json) = 'object'
    AND proposal_json ->> 'schema_version' = 'care_action_proposal.v1'
  ),
  created_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL CHECK (expires_at > created_at),
  updated_at TIMESTAMPTZ NOT NULL,
  UNIQUE (proposal_id, namespace_id, data_mode, subject_id),
  FOREIGN KEY (source_analysis_revision_id, namespace_id, data_mode)
    REFERENCES public.sleep_domain_analysis_revisions (
      analysis_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (source_night_finalization_revision_id)
    REFERENCES public.sleep_domain_night_finalization_revisions (
      night_finalization_revision_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (night_episode_id, namespace_id, data_mode)
    REFERENCES public.sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
    REFERENCES public.backend_namespace_generations (
      namespace_id, data_mode, generation
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  ),
  CHECK (proposal_json ->> 'proposal_id' = proposal_id),
  CHECK (proposal_json ->> 'proposal_semantic_hash' = proposal_semantic_sha256),
  CHECK (candidate_json ->> 'candidate_hash' = candidate_sha256),
  CHECK (candidate_json ->> 'subject_id' = subject_id),
  CHECK (candidate_json ->> 'action_type' = action_type),
  CHECK (candidate_json ->> 'audience' = audience_role),
  CHECK (candidate_json ->> 'source_analysis_revision_id' = source_analysis_revision_id),
  CHECK (candidate_json ->> 'source_shared_analysis_sha256' = source_shared_analysis_sha256),
  CHECK (candidate_json ->> 'source_night_finalization_revision_id' = source_night_finalization_revision_id),
  CHECK (NOT (candidate_json -> 'parameters') ?| ARRAY[
    'email', 'email_address', 'phone', 'phone_number', 'destination',
    'recipient', 'recipient_address', 'channel', 'smtp', 'sms'
  ])
);

CREATE INDEX idx_backend_care_action_proposal_pending_v3
  ON public.backend_care_action_proposals_v3 (
    namespace_id, data_mode, subject_id, state, created_at, proposal_id
  );
CREATE UNIQUE INDEX uq_backend_care_action_proposal_semantic_v3
  ON public.backend_care_action_proposals_v3 (
    namespace_id, data_mode, namespace_generation,
    COALESCE(run_id, ''), COALESCE(arm_id, ''), proposal_semantic_key
  );
CREATE INDEX idx_backend_care_action_proposal_source_v3
  ON public.backend_care_action_proposals_v3 (
    namespace_id, data_mode, night_episode_id, source_analysis_revision_id
  );

CREATE TABLE public.backend_care_action_decisions_v3 (
  decision_id TEXT PRIMARY KEY,
  proposal_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  actor_id TEXT,
  binding_id TEXT,
  actor_role TEXT CHECK (actor_role IN ('elder', 'family', 'doctor', 'system')),
  choice TEXT NOT NULL CHECK (choice IN (
    'approve', 'reject', 'revoke', 'expire', 'supersede', 'conflict'
  )),
  idempotency_key TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  reason_text TEXT CHECK (length(reason_text) <= 500),
  previous_state TEXT NOT NULL CHECK (previous_state IN (
    'proposed', 'awaiting_approval', 'approved', 'rejected', 'expired', 'revoked'
  )),
  resulting_state TEXT NOT NULL CHECK (resulting_state IN (
    'proposed', 'awaiting_approval', 'approved', 'rejected', 'expired', 'revoked'
  )),
  proposal_version BIGINT NOT NULL CHECK (proposal_version >= 1),
  policy_version TEXT NOT NULL,
  policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  decision_json JSONB NOT NULL CHECK (jsonb_typeof(decision_json) = 'object'),
  decided_at TIMESTAMPTZ NOT NULL,
  UNIQUE (proposal_id, actor_id, idempotency_key),
  FOREIGN KEY (proposal_id, namespace_id, data_mode, subject_id)
    REFERENCES public.backend_care_action_proposals_v3 (
      proposal_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  CHECK (
    (actor_role = 'system' AND actor_id IS NULL AND binding_id IS NULL)
    OR (actor_role IN ('elder', 'family', 'doctor')
      AND actor_id IS NOT NULL AND binding_id IS NOT NULL)
  ),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX idx_backend_care_action_decision_proposal_v3
  ON public.backend_care_action_decisions_v3 (
    proposal_id, decided_at, decision_id
  );

CREATE TABLE public.backend_approval_grants_v3 (
  grant_id TEXT PRIMARY KEY,
  proposal_id TEXT NOT NULL UNIQUE,
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
  audience_role TEXT NOT NULL CHECK (audience_role IN ('elder', 'family', 'doctor')),
  authorization_scope TEXT NOT NULL,
  proposal_semantic_sha256 TEXT NOT NULL CHECK (
    proposal_semantic_sha256 ~ '^[0-9a-f]{64}$'
  ),
  candidate_sha256 TEXT NOT NULL CHECK (candidate_sha256 ~ '^[0-9a-f]{64}$'),
  approver_actor_id TEXT NOT NULL,
  approver_role TEXT NOT NULL CHECK (approver_role IN ('elder', 'family', 'doctor')),
  approver_binding_id TEXT NOT NULL,
  authorization_epoch BIGINT NOT NULL CHECK (authorization_epoch >= 1),
  policy_version TEXT NOT NULL,
  policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  idempotency_key TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('active', 'revoked', 'expired')),
  version BIGINT NOT NULL CHECK (version >= 1),
  grant_sha256 TEXT NOT NULL UNIQUE CHECK (grant_sha256 ~ '^[0-9a-f]{64}$'),
  grant_json JSONB NOT NULL CHECK (
    jsonb_typeof(grant_json) = 'object'
    AND grant_json ->> 'schema_version' = 'care_approval_grant.v1'
  ),
  issued_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL CHECK (expires_at > issued_at),
  revoked_at TIMESTAMPTZ,
  revoked_by_actor_id TEXT,
  revocation_reason_code TEXT,
  UNIQUE (proposal_id, approver_actor_id, idempotency_key),
  FOREIGN KEY (proposal_id, namespace_id, data_mode, subject_id)
    REFERENCES public.backend_care_action_proposals_v3 (
      proposal_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (approver_binding_id)
    REFERENCES public.backend_actor_subject_bindings(binding_id) ON DELETE RESTRICT,
  CHECK (grant_json ->> 'grant_id' = grant_id),
  CHECK (grant_json ->> 'grant_hash' = grant_sha256),
  CHECK (grant_json ->> 'proposal_id' = proposal_id),
  CHECK (grant_json ->> 'proposal_semantic_hash' = proposal_semantic_sha256),
  CHECK (grant_json ->> 'candidate_hash' = candidate_sha256),
  CHECK (grant_json ->> 'subject_id' = subject_id),
  CHECK (grant_json ->> 'action_type' = action_type),
  CHECK (
    (state = 'active' AND revoked_at IS NULL AND revoked_by_actor_id IS NULL
      AND revocation_reason_code IS NULL)
    OR (state = 'revoked' AND revoked_at IS NOT NULL
      AND revoked_by_actor_id IS NOT NULL AND revocation_reason_code IS NOT NULL)
    OR state = 'expired'
  ),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX idx_backend_approval_grant_state_v3
  ON public.backend_approval_grants_v3 (
    namespace_id, data_mode, state, expires_at, grant_id
  );

CREATE OR REPLACE FUNCTION public.sleepagent_validate_care_proposal_update_v3()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF ROW(
    NEW.proposal_id, NEW.namespace_id, NEW.data_mode,
    NEW.namespace_generation, NEW.run_id, NEW.arm_id, NEW.subject_id,
    NEW.night_episode_id, NEW.source_analysis_revision_id,
    NEW.source_shared_analysis_sha256,
    NEW.source_night_finalization_revision_id,
    NEW.source_care_strategy_invocation_id, NEW.candidate_id,
    NEW.candidate_sha256, NEW.proposal_semantic_key,
    NEW.proposal_semantic_sha256, NEW.action_type, NEW.catalog_action_id,
    NEW.catalog_action_version, NEW.audience_role,
    NEW.required_approver_role, NEW.authorization_scope, NEW.urgency,
    NEW.policy_version, NEW.policy_sha256, NEW.policy_decision,
    NEW.policy_reason_code, NEW.candidate_json, NEW.proposal_json,
    NEW.created_at, NEW.expires_at
  ) IS DISTINCT FROM ROW(
    OLD.proposal_id, OLD.namespace_id, OLD.data_mode,
    OLD.namespace_generation, OLD.run_id, OLD.arm_id, OLD.subject_id,
    OLD.night_episode_id, OLD.source_analysis_revision_id,
    OLD.source_shared_analysis_sha256,
    OLD.source_night_finalization_revision_id,
    OLD.source_care_strategy_invocation_id, OLD.candidate_id,
    OLD.candidate_sha256, OLD.proposal_semantic_key,
    OLD.proposal_semantic_sha256, OLD.action_type, OLD.catalog_action_id,
    OLD.catalog_action_version, OLD.audience_role,
    OLD.required_approver_role, OLD.authorization_scope, OLD.urgency,
    OLD.policy_version, OLD.policy_sha256, OLD.policy_decision,
    OLD.policy_reason_code, OLD.candidate_json, OLD.proposal_json,
    OLD.created_at, OLD.expires_at
  ) THEN
    RAISE EXCEPTION 'CareActionProposal semantic content is immutable';
  END IF;
  IF NEW.version <> OLD.version + 1 OR NEW.updated_at <= OLD.updated_at THEN
    RAISE EXCEPTION 'CareActionProposal CAS version/time is invalid';
  END IF;
  IF NOT (
    (OLD.state = 'proposed' AND NEW.state = 'awaiting_approval')
    OR (OLD.state = 'awaiting_approval' AND NEW.state IN (
      'approved', 'rejected', 'expired'
    ))
    OR (OLD.state = 'approved' AND NEW.state IN ('revoked', 'expired'))
  ) THEN
    RAISE EXCEPTION 'CareActionProposal transition is invalid';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER sleepagent_validate_care_proposal_update_v3_trigger
BEFORE UPDATE ON public.backend_care_action_proposals_v3
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_validate_care_proposal_update_v3();

CREATE OR REPLACE FUNCTION public.sleepagent_care_governance_append_only_v3()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'Care governance audit rows are append-only';
END;
$$;

CREATE TRIGGER sleepagent_care_proposal_no_delete_v3_trigger
BEFORE DELETE ON public.backend_care_action_proposals_v3
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_care_governance_append_only_v3();
CREATE TRIGGER sleepagent_care_decision_append_only_v3_trigger
BEFORE UPDATE OR DELETE ON public.backend_care_action_decisions_v3
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_care_governance_append_only_v3();

CREATE OR REPLACE FUNCTION public.sleepagent_validate_approval_grant_update_v3()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF ROW(
    NEW.grant_id, NEW.proposal_id, NEW.namespace_id, NEW.data_mode,
    NEW.namespace_generation, NEW.run_id, NEW.arm_id, NEW.subject_id,
    NEW.action_type, NEW.audience_role, NEW.authorization_scope,
    NEW.proposal_semantic_sha256, NEW.candidate_sha256,
    NEW.approver_actor_id, NEW.approver_role, NEW.approver_binding_id,
    NEW.authorization_epoch, NEW.policy_version, NEW.policy_sha256,
    NEW.idempotency_key, NEW.grant_sha256, NEW.grant_json,
    NEW.issued_at, NEW.expires_at
  ) IS DISTINCT FROM ROW(
    OLD.grant_id, OLD.proposal_id, OLD.namespace_id, OLD.data_mode,
    OLD.namespace_generation, OLD.run_id, OLD.arm_id, OLD.subject_id,
    OLD.action_type, OLD.audience_role, OLD.authorization_scope,
    OLD.proposal_semantic_sha256, OLD.candidate_sha256,
    OLD.approver_actor_id, OLD.approver_role, OLD.approver_binding_id,
    OLD.authorization_epoch, OLD.policy_version, OLD.policy_sha256,
    OLD.idempotency_key, OLD.grant_sha256, OLD.grant_json,
    OLD.issued_at, OLD.expires_at
  ) THEN
    RAISE EXCEPTION 'ApprovalGrant semantic content is immutable';
  END IF;
  IF NEW.version <> OLD.version + 1
     OR OLD.state <> 'active' OR NEW.state NOT IN ('revoked', 'expired') THEN
    RAISE EXCEPTION 'ApprovalGrant transition is invalid';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER sleepagent_validate_approval_grant_update_v3_trigger
BEFORE UPDATE ON public.backend_approval_grants_v3
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_validate_approval_grant_update_v3();
CREATE TRIGGER sleepagent_approval_grant_no_delete_v3_trigger
BEFORE DELETE ON public.backend_approval_grants_v3
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_care_governance_append_only_v3();

ALTER TABLE public.backend_care_action_proposals_v3 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.backend_care_action_proposals_v3 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_care_action_proposal_scope_v3
  ON public.backend_care_action_proposals_v3
  USING (
    public.sleepagent_scope_setting('sleepagent.purpose') = 'internal_status'
    OR (
      public.sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
      )
      AND (
        public.sleepagent_scope_setting('sleepagent.process_role') = 'worker'
        OR audience_role = public.sleepagent_scope_setting('sleepagent.actor_role')
        OR required_approver_role = public.sleepagent_scope_setting('sleepagent.actor_role')
      )
    )
  )
  WITH CHECK (
    public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND public.sleepagent_scope_setting('sleepagent.process_role') IN ('worker', 'api')
  );

ALTER TABLE public.backend_care_action_decisions_v3 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.backend_care_action_decisions_v3 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_care_action_decision_scope_v3
  ON public.backend_care_action_decisions_v3
  USING (
    public.sleepagent_scope_setting('sleepagent.purpose') = 'internal_status'
    OR (
      public.sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
      )
      AND (
        public.sleepagent_scope_setting('sleepagent.process_role') = 'worker'
        OR actor_id = public.sleepagent_scope_setting('sleepagent.actor_id')
      )
    )
  )
  WITH CHECK (
    public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND public.sleepagent_scope_setting('sleepagent.process_role') IN ('worker', 'api')
  );

ALTER TABLE public.backend_approval_grants_v3 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.backend_approval_grants_v3 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_approval_grant_scope_v3
  ON public.backend_approval_grants_v3
  USING (
    public.sleepagent_scope_setting('sleepagent.purpose') = 'internal_status'
    OR (
      public.sleepagent_subject_generation_scope_allows(
        namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
      )
      AND (
        public.sleepagent_scope_setting('sleepagent.process_role') = 'worker'
        OR approver_actor_id = public.sleepagent_scope_setting('sleepagent.actor_id')
      )
    )
  )
  WITH CHECK (
    public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND public.sleepagent_scope_setting('sleepagent.process_role') IN ('worker', 'api')
  );

-- Mutations run only through authenticated, server-context-checking functions.
-- API roles receive EXECUTE but no direct INSERT/UPDATE on authority tables.
CREATE OR REPLACE FUNCTION public.sleepagent_decide_care_action_proposal_v3(
  target_proposal_id TEXT,
  expected_version BIGINT,
  requested_choice TEXT,
  requested_idempotency_key TEXT,
  requested_binding_id TEXT,
  requested_reason_code TEXT,
  requested_reason_text TEXT,
  requested_at TIMESTAMPTZ
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  proposal public.backend_care_action_proposals_v3%ROWTYPE;
  binding public.backend_actor_subject_bindings%ROWTYPE;
  existing public.backend_care_action_decisions_v3%ROWTYPE;
  next_state TEXT;
  decision_id TEXT;
  grant_id TEXT;
  grant_sha TEXT;
  grant_payload JSONB;
BEGIN
  IF requested_choice NOT IN ('approve', 'reject')
     OR requested_idempotency_key IS NULL
     OR length(requested_idempotency_key) NOT BETWEEN 1 AND 200
     OR requested_reason_code IS NULL
     OR length(requested_reason_code) NOT BETWEEN 1 AND 100
     OR length(COALESCE(requested_reason_text, '')) > 500
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'api'
     OR NULLIF(current_setting('sleepagent.purpose', TRUE), '') <> 'sleep_care'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'care_decision_invalid_context';
  END IF;

  SELECT item.* INTO proposal
  FROM public.backend_care_action_proposals_v3 AS item
  WHERE item.proposal_id = target_proposal_id
    AND item.namespace_id = NULLIF(current_setting('sleepagent.namespace_id', TRUE), '')
    AND item.data_mode = NULLIF(current_setting('sleepagent.data_mode', TRUE), '')
    AND item.namespace_generation::TEXT = NULLIF(current_setting('sleepagent.namespace_generation', TRUE), '')
    AND item.subject_id = NULLIF(current_setting('sleepagent.subject_id', TRUE), '')
  FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'care_proposal_not_found';
  END IF;

  SELECT item.* INTO existing
  FROM public.backend_care_action_decisions_v3 AS item
  WHERE item.proposal_id = proposal.proposal_id
    AND item.actor_id = NULLIF(current_setting('sleepagent.actor_id', TRUE), '')
    AND item.idempotency_key = requested_idempotency_key;
  IF FOUND THEN
    IF existing.choice = requested_choice THEN
      RETURN jsonb_build_object(
        'outcome', 'idempotent', 'proposal_id', proposal.proposal_id,
        'state', proposal.state, 'version', proposal.version,
        'grant_id', (SELECT existing_grant.grant_id
          FROM public.backend_approval_grants_v3 AS existing_grant
          WHERE existing_grant.proposal_id = proposal.proposal_id)
      );
    END IF;
    RETURN jsonb_build_object(
      'outcome', 'conflict', 'proposal_id', proposal.proposal_id,
      'state', proposal.state, 'version', proposal.version
    );
  END IF;

  SELECT item.* INTO binding
  FROM public.backend_actor_subject_bindings AS item
  WHERE item.binding_id = requested_binding_id
    AND item.namespace_id = proposal.namespace_id
    AND item.data_mode = proposal.data_mode
    AND item.subject_id = proposal.subject_id
    AND item.actor_id = NULLIF(current_setting('sleepagent.actor_id', TRUE), '')
    AND item.role = NULLIF(current_setting('sleepagent.actor_role', TRUE), '')
    AND item.role = proposal.required_approver_role
    AND item.status = 'active'
    AND item.authorization_epoch = current_setting('sleepagent.authorization_epoch')::BIGINT
    AND item.scopes_json ? 'product:sleep:care:confirm'
    AND item.valid_from <= requested_at
    AND (item.valid_until IS NULL OR item.valid_until > requested_at);
  IF NOT FOUND THEN
    RAISE EXCEPTION 'care_approver_unauthorized';
  END IF;

  IF proposal.expires_at <= requested_at
     AND proposal.state IN ('awaiting_approval', 'approved') THEN
    IF proposal.state IN ('awaiting_approval', 'approved') THEN
      UPDATE public.backend_care_action_proposals_v3
      SET state = 'expired', version = version + 1, updated_at = requested_at
      WHERE proposal_id = proposal.proposal_id AND version = proposal.version;
    END IF;
    IF proposal.state = 'approved' THEN
      UPDATE public.backend_approval_grants_v3
      SET state = 'expired', version = version + 1
      WHERE proposal_id = proposal.proposal_id AND state = 'active';
    END IF;
    decision_id := 'care-decision:' || encode(digest(
      proposal.proposal_id || E'\x1fsystem\x1fexpire', 'sha256'
    ), 'hex');
    INSERT INTO public.backend_care_action_decisions_v3 (
      decision_id, proposal_id, namespace_id, data_mode,
      namespace_generation, run_id, arm_id, subject_id,
      actor_id, binding_id, actor_role, choice, idempotency_key,
      reason_code, reason_text, previous_state, resulting_state,
      proposal_version, policy_version, policy_sha256, decision_json, decided_at
    ) VALUES (
      decision_id, proposal.proposal_id, proposal.namespace_id,
      proposal.data_mode, proposal.namespace_generation, proposal.run_id,
      proposal.arm_id, proposal.subject_id, NULL, NULL, 'system',
      'expire', 'system:proposal-expired', 'proposal_expired', NULL,
      proposal.state, 'expired', proposal.version + 1,
      proposal.policy_version, proposal.policy_sha256,
      '{"schema_version":"care_action_decision.v1","choice":"expire"}'::JSONB,
      requested_at
    ) ON CONFLICT ON CONSTRAINT backend_care_action_decisions_v3_pkey
      DO NOTHING;
    RETURN jsonb_build_object(
      'outcome', 'expired', 'proposal_id', proposal.proposal_id,
      'state', 'expired', 'version', proposal.version + 1
    );
  END IF;

  IF proposal.state <> 'awaiting_approval' OR proposal.version <> expected_version THEN
    decision_id := 'care-decision:' || encode(digest(
      proposal.proposal_id || E'\x1f' || binding.actor_id || E'\x1f' ||
      requested_idempotency_key || E'\x1fconflict', 'sha256'
    ), 'hex');
    INSERT INTO public.backend_care_action_decisions_v3 (
      decision_id, proposal_id, namespace_id, data_mode,
      namespace_generation, run_id, arm_id, subject_id,
      actor_id, binding_id, actor_role, choice, idempotency_key,
      reason_code, reason_text, previous_state, resulting_state,
      proposal_version, policy_version, policy_sha256, decision_json, decided_at
    ) VALUES (
      decision_id, proposal.proposal_id, proposal.namespace_id,
      proposal.data_mode, proposal.namespace_generation, proposal.run_id,
      proposal.arm_id, proposal.subject_id, binding.actor_id,
      binding.binding_id, binding.role, 'conflict', requested_idempotency_key,
      'terminal_or_version_conflict', NULL, proposal.state, proposal.state,
      proposal.version, proposal.policy_version, proposal.policy_sha256,
      jsonb_build_object('schema_version', 'care_action_decision.v1',
        'choice', requested_choice, 'outcome', 'conflict'), requested_at
    );
    RETURN jsonb_build_object(
      'outcome', 'conflict', 'proposal_id', proposal.proposal_id,
      'state', proposal.state, 'version', proposal.version
    );
  END IF;

  -- Current source analysis and finalization are revalidated under the lock.
  IF NOT EXISTS (
    SELECT 1
    FROM public.sleep_domain_night_finalizations AS finalization
    JOIN public.sleep_domain_analysis_revisions AS analysis
      ON analysis.analysis_revision_id = proposal.source_analysis_revision_id
     AND analysis.namespace_id = proposal.namespace_id
     AND analysis.data_mode = proposal.data_mode
     AND analysis.subject_id = proposal.subject_id
     AND analysis.night_episode_id = proposal.night_episode_id
    WHERE finalization.namespace_id = proposal.namespace_id
      AND finalization.data_mode = proposal.data_mode
      AND finalization.subject_id = proposal.subject_id
      AND finalization.night_episode_id = proposal.night_episode_id
      AND finalization.state = 'hard_finalized'
      AND finalization.current_finalization_revision_id =
        proposal.source_night_finalization_revision_id
      AND NOT EXISTS (
        SELECT 1 FROM public.sleep_domain_analysis_revisions AS newer
        WHERE newer.namespace_id = analysis.namespace_id
          AND newer.data_mode = analysis.data_mode
          AND newer.subject_id = analysis.subject_id
          AND newer.night_episode_id = analysis.night_episode_id
          AND newer.revision_number > analysis.revision_number
      )
  ) THEN
    UPDATE public.backend_care_action_proposals_v3
    SET state = 'expired', version = version + 1, updated_at = requested_at
    WHERE proposal_id = proposal.proposal_id AND version = proposal.version;
    decision_id := 'care-decision:' || encode(digest(
      proposal.proposal_id || E'\x1fsystem\x1fsupersede', 'sha256'
    ), 'hex');
    INSERT INTO public.backend_care_action_decisions_v3 (
      decision_id, proposal_id, namespace_id, data_mode,
      namespace_generation, run_id, arm_id, subject_id,
      actor_id, binding_id, actor_role, choice, idempotency_key,
      reason_code, reason_text, previous_state, resulting_state,
      proposal_version, policy_version, policy_sha256, decision_json, decided_at
    ) VALUES (
      decision_id, proposal.proposal_id, proposal.namespace_id,
      proposal.data_mode, proposal.namespace_generation, proposal.run_id,
      proposal.arm_id, proposal.subject_id, NULL, NULL, 'system',
      'supersede', 'system:source-superseded', 'source_analysis_superseded',
      NULL, proposal.state, 'expired', proposal.version + 1,
      proposal.policy_version, proposal.policy_sha256,
      '{"schema_version":"care_action_decision.v1","choice":"supersede"}'::JSONB,
      requested_at
    ) ON CONFLICT ON CONSTRAINT backend_care_action_decisions_v3_pkey
      DO NOTHING;
    RETURN jsonb_build_object(
      'outcome', 'superseded', 'proposal_id', proposal.proposal_id,
      'state', 'expired', 'version', proposal.version + 1
    );
  END IF;

  next_state := CASE requested_choice WHEN 'approve' THEN 'approved' ELSE 'rejected' END;
  UPDATE public.backend_care_action_proposals_v3
  SET state = next_state, version = version + 1, updated_at = requested_at
  WHERE proposal_id = proposal.proposal_id AND state = 'awaiting_approval'
    AND version = expected_version;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'care_decision_cas_conflict';
  END IF;

  decision_id := 'care-decision:' || encode(digest(
    proposal.proposal_id || E'\x1f' || binding.actor_id || E'\x1f' ||
    requested_idempotency_key, 'sha256'
  ), 'hex');
  INSERT INTO public.backend_care_action_decisions_v3 (
    decision_id, proposal_id, namespace_id, data_mode,
    namespace_generation, run_id, arm_id, subject_id,
    actor_id, binding_id, actor_role, choice, idempotency_key,
    reason_code, reason_text, previous_state, resulting_state,
    proposal_version, policy_version, policy_sha256, decision_json, decided_at
  ) VALUES (
    decision_id, proposal.proposal_id, proposal.namespace_id,
    proposal.data_mode, proposal.namespace_generation, proposal.run_id,
    proposal.arm_id, proposal.subject_id, binding.actor_id,
    binding.binding_id, binding.role, requested_choice,
    requested_idempotency_key, requested_reason_code,
    NULLIF(requested_reason_text, ''), proposal.state, next_state,
    proposal.version + 1, proposal.policy_version, proposal.policy_sha256,
    jsonb_build_object('schema_version', 'care_action_decision.v1',
      'choice', requested_choice, 'reason_code', requested_reason_code), requested_at
  );

  IF requested_choice = 'approve' THEN
    grant_sha := encode(digest(
      proposal.proposal_semantic_sha256 || E'\x1f' || binding.actor_id ||
      E'\x1f' || requested_idempotency_key || E'\x1f' ||
      requested_at::TEXT, 'sha256'
    ), 'hex');
    grant_id := 'care-grant:' || substring(grant_sha, 1, 32);
    grant_payload := jsonb_build_object(
      'schema_version', 'care_approval_grant.v1',
      'grant_id', grant_id, 'grant_hash', grant_sha,
      'proposal_id', proposal.proposal_id,
      'proposal_semantic_hash', proposal.proposal_semantic_sha256,
      'candidate_hash', proposal.candidate_sha256,
      'subject_id', proposal.subject_id, 'action_type', proposal.action_type,
      'audience', proposal.audience_role,
      'authorization_scope', proposal.authorization_scope,
      'approver_actor_id', binding.actor_id, 'approver_role', binding.role,
      'approver_binding_id', binding.binding_id,
      'authorization_epoch', binding.authorization_epoch,
      'issued_at', requested_at, 'expires_at', proposal.expires_at,
      'policy_version', proposal.policy_version,
      'policy_hash', proposal.policy_sha256,
      'idempotency_key', requested_idempotency_key,
      'state', 'active', 'version', 1
    );
    INSERT INTO public.backend_approval_grants_v3 (
      grant_id, proposal_id, namespace_id, data_mode, namespace_generation,
      run_id, arm_id, subject_id, action_type, audience_role,
      authorization_scope, proposal_semantic_sha256, candidate_sha256,
      approver_actor_id, approver_role, approver_binding_id,
      authorization_epoch, policy_version, policy_sha256, idempotency_key,
      state, version, grant_sha256, grant_json, issued_at, expires_at
    ) VALUES (
      grant_id, proposal.proposal_id, proposal.namespace_id, proposal.data_mode,
      proposal.namespace_generation, proposal.run_id, proposal.arm_id,
      proposal.subject_id, proposal.action_type, proposal.audience_role,
      proposal.authorization_scope, proposal.proposal_semantic_sha256,
      proposal.candidate_sha256, binding.actor_id, binding.role,
      binding.binding_id, binding.authorization_epoch, proposal.policy_version,
      proposal.policy_sha256, requested_idempotency_key, 'active', 1,
      grant_sha, grant_payload, requested_at, proposal.expires_at
    );
  END IF;

  RETURN jsonb_build_object(
    'outcome', 'applied', 'proposal_id', proposal.proposal_id,
    'state', next_state, 'version', proposal.version + 1,
    'decision_id', decision_id, 'grant_id', grant_id
  );
END;
$$;

CREATE OR REPLACE FUNCTION public.sleepagent_revoke_care_approval_v3(
  target_proposal_id TEXT,
  expected_version BIGINT,
  requested_idempotency_key TEXT,
  requested_binding_id TEXT,
  requested_reason_code TEXT,
  requested_reason_text TEXT,
  requested_at TIMESTAMPTZ
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  proposal public.backend_care_action_proposals_v3%ROWTYPE;
  grant_row public.backend_approval_grants_v3%ROWTYPE;
  binding public.backend_actor_subject_bindings%ROWTYPE;
  existing public.backend_care_action_decisions_v3%ROWTYPE;
  decision_id TEXT;
BEGIN
  IF requested_idempotency_key IS NULL
     OR length(requested_idempotency_key) NOT BETWEEN 1 AND 200
     OR requested_reason_code IS NULL
     OR length(requested_reason_code) NOT BETWEEN 1 AND 100
     OR length(COALESCE(requested_reason_text, '')) > 500
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'api'
     OR NULLIF(current_setting('sleepagent.purpose', TRUE), '') <> 'sleep_care'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'care_revoke_invalid_context';
  END IF;
  SELECT item.* INTO proposal
  FROM public.backend_care_action_proposals_v3 AS item
  WHERE item.proposal_id = target_proposal_id
    AND item.namespace_id = NULLIF(current_setting('sleepagent.namespace_id', TRUE), '')
    AND item.data_mode = NULLIF(current_setting('sleepagent.data_mode', TRUE), '')
    AND item.subject_id = NULLIF(current_setting('sleepagent.subject_id', TRUE), '')
  FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'care_proposal_not_found'; END IF;

  SELECT item.* INTO existing
  FROM public.backend_care_action_decisions_v3 AS item
  WHERE item.proposal_id = proposal.proposal_id
    AND item.actor_id = NULLIF(current_setting('sleepagent.actor_id', TRUE), '')
    AND item.idempotency_key = requested_idempotency_key;
  IF FOUND AND existing.choice = 'revoke' THEN
    RETURN jsonb_build_object('outcome', 'idempotent',
      'proposal_id', proposal.proposal_id, 'state', proposal.state,
      'version', proposal.version);
  END IF;

  SELECT item.* INTO grant_row
  FROM public.backend_approval_grants_v3 AS item
  WHERE item.proposal_id = proposal.proposal_id
  FOR UPDATE;
  SELECT item.* INTO binding
  FROM public.backend_actor_subject_bindings AS item
  WHERE item.binding_id = requested_binding_id
    AND item.namespace_id = proposal.namespace_id
    AND item.data_mode = proposal.data_mode
    AND item.subject_id = proposal.subject_id
    AND item.actor_id = NULLIF(current_setting('sleepagent.actor_id', TRUE), '')
    AND item.role = NULLIF(current_setting('sleepagent.actor_role', TRUE), '')
    AND item.status = 'active'
    AND item.authorization_epoch = current_setting('sleepagent.authorization_epoch')::BIGINT
    AND item.scopes_json ? 'product:sleep:care:confirm'
    AND item.valid_from <= requested_at
    AND (item.valid_until IS NULL OR item.valid_until > requested_at)
    AND (item.actor_id = grant_row.approver_actor_id OR item.role = 'elder');
  IF NOT FOUND THEN RAISE EXCEPTION 'care_revoker_unauthorized'; END IF;
  IF proposal.state <> 'approved' OR grant_row.state <> 'active'
     OR proposal.version <> expected_version THEN
    RETURN jsonb_build_object('outcome', 'conflict',
      'proposal_id', proposal.proposal_id, 'state', proposal.state,
      'version', proposal.version);
  END IF;

  UPDATE public.backend_care_action_proposals_v3
  SET state = 'revoked', version = version + 1, updated_at = requested_at
  WHERE proposal_id = proposal.proposal_id AND version = expected_version;
  UPDATE public.backend_approval_grants_v3
  SET state = 'revoked', version = version + 1, revoked_at = requested_at,
      revoked_by_actor_id = binding.actor_id,
      revocation_reason_code = requested_reason_code
  WHERE grant_id = grant_row.grant_id AND state = 'active';
  decision_id := 'care-decision:' || encode(digest(
    proposal.proposal_id || E'\x1f' || binding.actor_id || E'\x1f' ||
    requested_idempotency_key, 'sha256'
  ), 'hex');
  INSERT INTO public.backend_care_action_decisions_v3 (
    decision_id, proposal_id, namespace_id, data_mode,
    namespace_generation, run_id, arm_id, subject_id,
    actor_id, binding_id, actor_role, choice, idempotency_key,
    reason_code, reason_text, previous_state, resulting_state,
    proposal_version, policy_version, policy_sha256, decision_json, decided_at
  ) VALUES (
    decision_id, proposal.proposal_id, proposal.namespace_id,
    proposal.data_mode, proposal.namespace_generation, proposal.run_id,
    proposal.arm_id, proposal.subject_id, binding.actor_id,
    binding.binding_id, binding.role, 'revoke', requested_idempotency_key,
    requested_reason_code, NULLIF(requested_reason_text, ''),
    'approved', 'revoked', proposal.version + 1, proposal.policy_version,
    proposal.policy_sha256,
    jsonb_build_object('schema_version', 'care_action_decision.v1',
      'choice', 'revoke', 'reason_code', requested_reason_code), requested_at
  );
  RETURN jsonb_build_object('outcome', 'applied',
    'proposal_id', proposal.proposal_id, 'state', 'revoked',
    'version', proposal.version + 1, 'decision_id', decision_id,
    'grant_id', grant_row.grant_id);
END;
$$;

CREATE OR REPLACE FUNCTION public.sleepagent_care_governance_operational_metrics_v3()
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
     OR NULLIF(current_setting('sleepagent.purpose', TRUE), '') <> 'internal_status'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid care governance metrics context';
  END IF;
  SELECT jsonb_build_object(
    'schema_version', 'care_governance_operational_metrics.v1',
    'pending_care_proposals', count(*) FILTER (
      WHERE state = 'awaiting_approval' AND expires_at > clock_timestamp()
    ),
    'oldest_pending_approval_age_seconds', COALESCE(extract(epoch FROM (
      clock_timestamp() - min(created_at) FILTER (
        WHERE state = 'awaiting_approval' AND expires_at > clock_timestamp()
      )
    ))::BIGINT, 0),
    'approved_not_consumed_count', (
      SELECT count(*) FROM public.backend_approval_grants_v3
      WHERE state = 'active' AND expires_at > clock_timestamp()
    ),
    'expired_proposal_count', count(*) FILTER (
      WHERE state = 'expired' OR expires_at <= clock_timestamp()
    ),
    'revoked_grant_count', (
      SELECT count(*) FROM public.backend_approval_grants_v3 WHERE state = 'revoked'
    ),
    'decision_conflict_error_count', (
      SELECT count(*) FROM public.backend_care_action_decisions_v3
      WHERE choice = 'conflict'
    )
  ) INTO result
  FROM public.backend_care_action_proposals_v3
  WHERE data_mode = NULLIF(current_setting('sleepagent.data_mode', TRUE), '');
  RETURN result;
END;
$$;

REVOKE ALL ON FUNCTION public.sleepagent_validate_care_proposal_update_v3() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_care_governance_append_only_v3() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_validate_approval_grant_update_v3() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_decide_care_action_proposal_v3(
  TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT, TEXT, TIMESTAMPTZ
) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_revoke_care_approval_v3(
  TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT, TIMESTAMPTZ
) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_care_governance_operational_metrics_v3()
  FROM PUBLIC;
