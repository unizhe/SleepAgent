-- sleepagent:transactional=true

-- R1-D: internal status is aggregate authority, never a caller-selected
-- base-table RLS bypass. Existing subject/role worker and API policy remains.

DROP POLICY backend_care_action_proposal_scope_v3
  ON public.backend_care_action_proposals_v3;
CREATE POLICY backend_care_action_proposal_scope_v3
  ON public.backend_care_action_proposals_v3
  USING (
    public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND (
      public.sleepagent_scope_setting('sleepagent.process_role') = 'worker'
      OR audience_role = public.sleepagent_scope_setting('sleepagent.actor_role')
      OR required_approver_role =
        public.sleepagent_scope_setting('sleepagent.actor_role')
    )
  )
  WITH CHECK (
    public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND public.sleepagent_scope_setting('sleepagent.process_role')
      IN ('worker', 'api')
  );

DROP POLICY backend_care_action_decision_scope_v3
  ON public.backend_care_action_decisions_v3;
CREATE POLICY backend_care_action_decision_scope_v3
  ON public.backend_care_action_decisions_v3
  USING (
    public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND (
      public.sleepagent_scope_setting('sleepagent.process_role') = 'worker'
      OR actor_id = public.sleepagent_scope_setting('sleepagent.actor_id')
    )
  )
  WITH CHECK (
    public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND public.sleepagent_scope_setting('sleepagent.process_role')
      IN ('worker', 'api')
  );

DROP POLICY backend_approval_grant_scope_v3
  ON public.backend_approval_grants_v3;
CREATE POLICY backend_approval_grant_scope_v3
  ON public.backend_approval_grants_v3
  USING (
    public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND (
      public.sleepagent_scope_setting('sleepagent.process_role') = 'worker'
      OR approver_actor_id = public.sleepagent_scope_setting('sleepagent.actor_id')
    )
  )
  WITH CHECK (
    public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND public.sleepagent_scope_setting('sleepagent.process_role')
      IN ('worker', 'api')
  );

DROP POLICY backend_care_plan_scope_v1 ON public.backend_care_plans_v1;
CREATE POLICY backend_care_plan_scope_v1 ON public.backend_care_plans_v1
  USING (
    public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND (
      public.sleepagent_scope_setting('sleepagent.process_role') = 'worker'
      OR executor_role = public.sleepagent_scope_setting('sleepagent.actor_role')
    )
  );

DROP POLICY backend_care_execution_state_scope_v1
  ON public.backend_care_execution_states_v1;
CREATE POLICY backend_care_execution_state_scope_v1
  ON public.backend_care_execution_states_v1
  USING (
    public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND (
      public.sleepagent_scope_setting('sleepagent.process_role') = 'worker'
      OR executor_role = public.sleepagent_scope_setting('sleepagent.actor_role')
    )
  );

DROP POLICY backend_care_execution_event_scope_v1
  ON public.backend_care_execution_events_v1;
CREATE POLICY backend_care_execution_event_scope_v1
  ON public.backend_care_execution_events_v1
  USING (
    public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND (
      public.sleepagent_scope_setting('sleepagent.process_role') = 'worker'
      OR executor_role = public.sleepagent_scope_setting('sleepagent.actor_role')
    )
  );

DROP POLICY backend_care_outcome_evaluation_scope_v1
  ON public.backend_care_outcome_evaluations_v1;
CREATE POLICY backend_care_outcome_evaluation_scope_v1
  ON public.backend_care_outcome_evaluations_v1
  USING (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

DROP POLICY backend_care_outcome_scope_v1 ON public.backend_care_outcomes_v1;
CREATE POLICY backend_care_outcome_scope_v1 ON public.backend_care_outcomes_v1
  USING (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

DROP POLICY backend_personalization_effect_scope_v1
  ON public.backend_personalization_effect_receipts_v1;
CREATE POLICY backend_personalization_effect_scope_v1
  ON public.backend_personalization_effect_receipts_v1
  USING (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

DROP POLICY backend_personalization_governance_scope_v1
  ON public.backend_personalization_governance_v1;
CREATE POLICY backend_personalization_governance_scope_v1
  ON public.backend_personalization_governance_v1
  USING (
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

CREATE OR REPLACE FUNCTION public.sleepagent_internal_status_principal_allows_v1()
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT
    NULLIF(current_setting('sleepagent.process_role', TRUE), '') = 'api'
    AND NULLIF(current_setting('sleepagent.purpose', TRUE), '') =
      'internal_status'
    AND NULLIF(current_setting('sleepagent.data_mode', TRUE), '')
      IN ('live', 'replay')
    AND public.sleepagent_principal_context_allows()
    AND EXISTS (
      SELECT 1
      FROM public.backend_service_principals AS principal
      WHERE principal.principal_id = NULLIF(
          current_setting('sleepagent.service_principal_id', TRUE), ''
        )
        AND principal.database_role_name::text = session_user::text
        AND principal.principal_kind = 'bff'
        AND principal.status = 'active'
    )
$$;
REVOKE ALL ON FUNCTION public.sleepagent_internal_status_principal_allows_v1()
  FROM PUBLIC;

-- Each aggregate remains SECURITY DEFINER with a fixed search_path, no PUBLIC
-- execute, and aggregate-only output; strengthen its entry guard uniformly.
DO $$
DECLARE
  signature REGPROCEDURE;
  definition TEXT;
  original TEXT;
BEGIN
  FOREACH signature IN ARRAY ARRAY[
    'public.sleepagent_care_governance_operational_metrics_v3()'::regprocedure,
    'public.sleepagent_care_execution_operational_metrics_v1()'::regprocedure,
    'public.sleepagent_care_outcome_operational_metrics_v1()'::regprocedure,
    'public.sleepagent_care_evaluation_operational_metrics_v1()'::regprocedure,
    'public.sleepagent_personalization_governance_metrics_v1()'::regprocedure
  ] LOOP
    definition := pg_get_functiondef(signature);
    original := definition;
    definition := replace(
      definition,
      'public.sleepagent_principal_context_allows()',
      'public.sleepagent_internal_status_principal_allows_v1()'
    );
    IF definition = original
       OR position('sleepagent_internal_status_principal_allows_v1' IN definition)
          = 0 THEN
      RAISE EXCEPTION 'Care metrics principal guard patch drifted: %', signature;
    END IF;
    EXECUTE definition;
  END LOOP;
END;
$$;
