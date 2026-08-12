-- sleepagent:transactional=true
-- Stage 4: one induction Operation per committed Product analysis and one
-- queryable, idempotent replay delivery sink behind a current-authority permit.

CREATE TABLE backend_induction_manifests_v2 (
  manifest_id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL UNIQUE,
  source_product_operation_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  analysis_revision_id TEXT NOT NULL UNIQUE,
  night_episode_revision_id TEXT NOT NULL,
  source_fact_snapshot_sha256 TEXT NOT NULL CHECK (
    source_fact_snapshot_sha256 ~ '^[0-9a-f]{64}$'
  ),
  policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  manifest_sha256 TEXT NOT NULL UNIQUE CHECK (
    manifest_sha256 ~ '^[0-9a-f]{64}$'
  ),
  manifest_json JSONB NOT NULL CHECK (jsonb_typeof(manifest_json) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (source_product_operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (analysis_revision_id, namespace_id, data_mode)
    REFERENCES sleep_domain_analysis_revisions (
      analysis_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (night_episode_revision_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episode_revisions (
      night_episode_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
    REFERENCES backend_namespace_generations (
      namespace_id, data_mode, generation
    ) ON DELETE RESTRICT,
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
    REFERENCES backend_replay_arms (
      namespace_id, namespace_generation, run_id, arm_id
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  ),
  CHECK (manifest_json ->> 'schema_version' = 'induction_manifest.v1')
);

CREATE TABLE backend_personalization_profiles_v2 (
  profile_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  current_profile_revision_id TEXT NOT NULL,
  current_version BIGINT NOT NULL CHECK (current_version >= 1),
  cas_version BIGINT NOT NULL CHECK (cas_version >= 1),
  profile_sha256 TEXT NOT NULL CHECK (profile_sha256 ~ '^[0-9a-f]{64}$'),
  profile_json JSONB NOT NULL CHECK (jsonb_typeof(profile_json) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (profile_id, namespace_id, data_mode, subject_id),
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
    REFERENCES backend_namespace_generations (
      namespace_id, data_mode, generation
    ) ON DELETE RESTRICT,
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
    REFERENCES backend_replay_arms (
      namespace_id, namespace_generation, run_id, arm_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (namespace_id, data_mode, subject_id)
    REFERENCES backend_subjects (
      namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  ),
  CHECK (profile_json ->> 'schema_version' = 'personalization_profile.v1')
);

CREATE UNIQUE INDEX uq_backend_personalization_profile_v2_scope
ON backend_personalization_profiles_v2 (
  namespace_id,
  data_mode,
  namespace_generation,
  COALESCE(run_id, ''),
  COALESCE(arm_id, ''),
  subject_id
);

CREATE TABLE backend_personalization_profile_revisions_v2 (
  profile_revision_id TEXT PRIMARY KEY,
  profile_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  profile_version BIGINT NOT NULL CHECK (profile_version >= 1),
  parent_profile_revision_id TEXT,
  source_analysis_revision_id TEXT NOT NULL,
  manifest_id TEXT NOT NULL,
  profile_sha256 TEXT NOT NULL CHECK (profile_sha256 ~ '^[0-9a-f]{64}$'),
  profile_json JSONB NOT NULL CHECK (jsonb_typeof(profile_json) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (profile_id, profile_version),
  UNIQUE (namespace_id, data_mode, namespace_generation, source_analysis_revision_id),
  FOREIGN KEY (profile_id, namespace_id, data_mode, subject_id)
    REFERENCES backend_personalization_profiles_v2 (
      profile_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (source_analysis_revision_id, namespace_id, data_mode)
    REFERENCES sleep_domain_analysis_revisions (
      analysis_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (manifest_id)
    REFERENCES backend_induction_manifests_v2(manifest_id) ON DELETE RESTRICT,
  FOREIGN KEY (parent_profile_revision_id)
    REFERENCES backend_personalization_profile_revisions_v2(profile_revision_id)
    ON DELETE RESTRICT,
  CHECK (
    (profile_version = 1 AND parent_profile_revision_id IS NULL)
    OR (profile_version > 1 AND parent_profile_revision_id IS NOT NULL)
  ),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE backend_induction_receipts_v2 (
  receipt_id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL UNIQUE,
  manifest_id TEXT NOT NULL UNIQUE,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  profile_revision_id TEXT NOT NULL UNIQUE,
  from_version BIGINT NOT NULL CHECK (from_version >= 0),
  to_version BIGINT NOT NULL CHECK (to_version = from_version + 1),
  status TEXT NOT NULL CHECK (status IN ('succeeded', 'excluded', 'dead_letter')),
  receipt_json JSONB NOT NULL CHECK (jsonb_typeof(receipt_json) = 'object'),
  committed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (manifest_id)
    REFERENCES backend_induction_manifests_v2(manifest_id) ON DELETE RESTRICT,
  FOREIGN KEY (profile_revision_id)
    REFERENCES backend_personalization_profile_revisions_v2(profile_revision_id)
    ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

ALTER TABLE backend_delivery_intents
  ADD COLUMN protocol_version INTEGER NOT NULL DEFAULT 1,
  ADD COLUMN recipient_actor_id TEXT,
  ADD COLUMN recipient_binding_id TEXT,
  ADD COLUMN confirmation_decision_id TEXT,
  ADD CONSTRAINT backend_delivery_intent_v2_contract CHECK (
    protocol_version < 2 OR (
      destination = 'replay_care_notification'
      AND handler_name = 'deterministic_replay_sink'
      AND recipient_actor_id IS NOT NULL
      AND recipient_binding_id IS NOT NULL
      AND confirmation_decision_id IS NOT NULL
      AND intent_json ->> 'schema_version' = 'delivery_intent.v2'
      AND intent_json ->> 'recipient_actor_id' = recipient_actor_id
      AND intent_json ->> 'recipient_binding_id' = recipient_binding_id
      AND intent_json ->> 'human_decision_id' = confirmation_decision_id
    )
  ),
  ADD CONSTRAINT backend_delivery_recipient_actor_fk
    FOREIGN KEY (recipient_actor_id)
    REFERENCES backend_actors(actor_id) ON DELETE RESTRICT,
  ADD CONSTRAINT backend_delivery_recipient_binding_fk
    FOREIGN KEY (recipient_binding_id)
    REFERENCES backend_actor_subject_bindings(binding_id) ON DELETE RESTRICT,
  ADD CONSTRAINT backend_delivery_confirmation_decision_fk
    FOREIGN KEY (confirmation_decision_id)
    REFERENCES backend_human_decisions_v2(human_decision_id) ON DELETE RESTRICT;

CREATE TABLE backend_replay_delivery_effects_v2 (
  effect_id TEXT PRIMARY KEY,
  consumer_id TEXT NOT NULL,
  delivery_intent_id TEXT NOT NULL UNIQUE,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT NOT NULL,
  arm_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  semantic_effect_key TEXT NOT NULL UNIQUE,
  payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  result_sha256 TEXT NOT NULL CHECK (result_sha256 ~ '^[0-9a-f]{64}$'),
  result_json JSONB NOT NULL CHECK (jsonb_typeof(result_json) = 'object'),
  applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (delivery_intent_id, namespace_id, data_mode, subject_id)
    REFERENCES backend_delivery_intents (
      delivery_intent_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  CHECK (result_json ->> 'status' = 'delivered')
);

CREATE TABLE backend_delivery_reconciliation_receipts_v2 (
  reconciliation_receipt_id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL UNIQUE,
  delivery_intent_id TEXT NOT NULL UNIQUE,
  invocation_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  resolution TEXT NOT NULL CHECK (
    resolution IN ('known_delivered', 'known_not_delivered', 'unknown')
  ),
  provider_evidence_ref TEXT,
  receipt_json JSONB NOT NULL CHECK (jsonb_typeof(receipt_json) = 'object'),
  committed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (delivery_intent_id, namespace_id, data_mode, subject_id)
    REFERENCES backend_delivery_intents (
      delivery_intent_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (invocation_id, namespace_id, data_mode, subject_id)
    REFERENCES backend_invocations (
      invocation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE OR REPLACE FUNCTION sleepagent_stage4_delivery_authority_allows(
  requested_delivery_intent_id TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT EXISTS (
    SELECT 1
    FROM public.backend_delivery_intents AS intent
    JOIN public.backend_namespaces AS namespace_row
      ON namespace_row.namespace_id = intent.namespace_id
     AND namespace_row.data_mode = intent.data_mode
     AND namespace_row.current_generation = intent.namespace_generation
     AND namespace_row.status = 'active'
    JOIN public.backend_subjects AS subject_row
      ON subject_row.namespace_id = intent.namespace_id
     AND subject_row.data_mode = intent.data_mode
     AND subject_row.subject_id = intent.subject_id
     AND subject_row.status = 'active'
    JOIN public.backend_subject_epochs AS epoch_row
      ON epoch_row.namespace_id = intent.namespace_id
     AND epoch_row.data_mode = intent.data_mode
     AND epoch_row.subject_id = intent.subject_id
    JOIN public.backend_human_decisions_v2 AS decision
      ON decision.human_decision_id = intent.confirmation_decision_id
     AND decision.namespace_id = intent.namespace_id
     AND decision.data_mode = intent.data_mode
     AND decision.namespace_generation = intent.namespace_generation
     AND decision.subject_id = intent.subject_id
     AND decision.choice = 'confirm'
    JOIN public.backend_care_actions_v2 AS care_action
      ON care_action.human_decision_id = decision.human_decision_id
     AND care_action.namespace_id = intent.namespace_id
     AND care_action.data_mode = intent.data_mode
     AND care_action.namespace_generation = intent.namespace_generation
     AND care_action.subject_id = intent.subject_id
     AND care_action.care_action_id = intent.intent_json ->> 'care_action_id'
     AND care_action.state IN ('confirmed_pending_delivery', 'active')
    JOIN public.backend_actor_subject_bindings AS binding_row
      ON binding_row.binding_id = intent.recipient_binding_id
     AND binding_row.namespace_id = intent.namespace_id
     AND binding_row.data_mode = intent.data_mode
     AND binding_row.subject_id = intent.subject_id
     AND binding_row.actor_id = intent.recipient_actor_id
     AND binding_row.role = intent.intent_json ->> 'recipient_role'
     AND binding_row.status = 'active'
     AND binding_row.authorization_epoch = epoch_row.authorization_epoch
     AND binding_row.scopes_json ? 'product:sleep:care:confirm'
     AND binding_row.valid_from <= clock_timestamp()
     AND (binding_row.valid_until IS NULL
       OR binding_row.valid_until > clock_timestamp())
    JOIN public.backend_actors AS actor_row
      ON actor_row.actor_id = binding_row.actor_id
     AND actor_row.status = 'active'
    WHERE intent.delivery_intent_id = requested_delivery_intent_id
      AND intent.protocol_version >= 2
      AND intent.status IN ('running', 'dispatching')
      AND intent.worker_instance = NULLIF(
        current_setting('sleepagent.worker_instance', TRUE), ''
      )
      AND intent.lease_expires_at > clock_timestamp()
      AND intent.namespace_id = NULLIF(
        current_setting('sleepagent.namespace_id', TRUE), ''
      )
      AND intent.data_mode = NULLIF(
        current_setting('sleepagent.data_mode', TRUE), ''
      )
      AND intent.namespace_generation::text = NULLIF(
        current_setting('sleepagent.namespace_generation', TRUE), ''
      )
      AND intent.subject_id = NULLIF(
        current_setting('sleepagent.subject_id', TRUE), ''
      )
      AND intent.authorization_snapshot_json ->> 'authorization_epoch' =
        epoch_row.authorization_epoch::text
      AND intent.authorization_snapshot_json ->> 'privacy_epoch' =
        epoch_row.privacy_epoch::text
      AND intent.authorization_snapshot_json ->> 'retrieval_policy_epoch' =
        epoch_row.retrieval_policy_epoch::text
      AND decision.actor_id = intent.recipient_actor_id
      AND decision.binding_id = intent.recipient_binding_id
      AND decision.authorization_epoch = epoch_row.authorization_epoch
      AND decision.privacy_epoch = epoch_row.privacy_epoch
      AND decision.retrieval_policy_epoch = epoch_row.retrieval_policy_epoch
      AND EXISTS (
        SELECT 1
        FROM public.backend_principal_grants AS worker_grant
        JOIN public.backend_service_principals AS worker_principal
          ON worker_principal.principal_id = worker_grant.principal_id
         AND worker_principal.status = 'active'
        WHERE worker_grant.principal_id = NULLIF(
            current_setting('sleepagent.service_principal_id', TRUE), ''
          )
          AND worker_grant.namespace_id = intent.namespace_id
          AND worker_grant.data_mode = intent.data_mode
          AND worker_grant.purpose = NULLIF(
            current_setting('sleepagent.purpose', TRUE), ''
          )
          AND worker_grant.authorization_epoch = epoch_row.authorization_epoch
          AND worker_grant.status = 'active'
          AND worker_grant.valid_from <= clock_timestamp()
          AND (worker_grant.valid_until IS NULL
            OR worker_grant.valid_until > clock_timestamp())
          AND worker_grant.allowed_handlers_json ? intent.handler_name
      )
  )
$$;

REVOKE ALL ON FUNCTION sleepagent_stage4_delivery_authority_allows(TEXT)
  FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_mark_delivery_dispatching(
  target_delivery_intent_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  UPDATE public.backend_delivery_intents
  SET status = 'dispatching',
      dispatch_permit_at = clock_timestamp(),
      updated_at = clock_timestamp()
  WHERE delivery_intent_id = target_delivery_intent_id
    AND status = 'running'
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp()
    AND (
      protocol_version < 2
      OR public.sleepagent_stage4_delivery_authority_allows(
        target_delivery_intent_id
      )
    );
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_mark_delivery_dispatching(
  TEXT, BIGINT, TEXT
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_internal_reconciliation_status(
  requested_operation_id TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  result JSONB;
BEGIN
  IF requested_operation_id IS NULL OR requested_operation_id = ''
     OR octet_length(requested_operation_id) > 200
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'api'
     OR NULLIF(current_setting('sleepagent.purpose', TRUE), '') <>
       'internal_status'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid internal reconciliation status context';
  END IF;
  SELECT jsonb_build_object(
    'schema_version', 'internal_reconciliation_status.v1',
    'operation_id', operation.operation_id,
    'operation_type', operation.operation_type,
    'status', operation.status,
    'outcome_class', operation.outcome_class,
    'attempt_count', operation.attempt_count,
    'queue_name', operation.queue_name,
    'target_resource_id', operation.target_resource_id,
    'resolution', COALESCE(
      delivery_receipt.resolution,
      date_reconciliation.status
    ),
    'delivery_state', delivery_receipt.receipt_json ->> 'delivery_state',
    'updated_at', operation.updated_at
  ) INTO result
  FROM public.sleep_domain_operations AS operation
  LEFT JOIN public.backend_delivery_reconciliation_receipts_v2
    AS delivery_receipt
    ON delivery_receipt.operation_id = operation.operation_id
  LEFT JOIN public.backend_episode_date_reconciliation AS date_reconciliation
    ON date_reconciliation.operation_id = operation.operation_id
  WHERE operation.operation_id = requested_operation_id
    AND operation.operation_type IN (
      'delivery_reconciliation', 'episode_date_reconciliation'
    )
    AND operation.data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    );
  RETURN result;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_internal_reconciliation_status(TEXT)
  FROM PUBLIC;

CREATE TRIGGER backend_induction_manifest_v2_immutable
BEFORE UPDATE OR DELETE ON backend_induction_manifests_v2
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_demo_append_only_mutation();
CREATE TRIGGER backend_personalization_profile_revision_v2_immutable
BEFORE UPDATE OR DELETE ON backend_personalization_profile_revisions_v2
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_demo_append_only_mutation();
CREATE TRIGGER backend_induction_receipt_v2_immutable
BEFORE UPDATE OR DELETE ON backend_induction_receipts_v2
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_demo_append_only_mutation();
CREATE TRIGGER backend_replay_delivery_effect_v2_immutable
BEFORE UPDATE OR DELETE ON backend_replay_delivery_effects_v2
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_demo_append_only_mutation();
CREATE TRIGGER backend_delivery_reconciliation_receipt_v2_immutable
BEFORE UPDATE OR DELETE ON backend_delivery_reconciliation_receipts_v2
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_demo_append_only_mutation();

ALTER TABLE backend_induction_manifests_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_induction_manifests_v2 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_induction_manifest_v2_scope
  ON backend_induction_manifests_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_personalization_profiles_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_personalization_profiles_v2 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_personalization_profile_v2_scope
  ON backend_personalization_profiles_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_personalization_profile_revisions_v2
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_personalization_profile_revisions_v2
  FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_personalization_profile_revision_v2_scope
  ON backend_personalization_profile_revisions_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_induction_receipts_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_induction_receipts_v2 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_induction_receipt_v2_scope
  ON backend_induction_receipts_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_replay_delivery_effects_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_replay_delivery_effects_v2 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_replay_delivery_effect_v2_scope
  ON backend_replay_delivery_effects_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_delivery_reconciliation_receipts_v2
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_delivery_reconciliation_receipts_v2
  FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_delivery_reconciliation_receipt_v2_scope
  ON backend_delivery_reconciliation_receipts_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

UPDATE backend_principal_grants
SET allowed_handlers_json = allowed_handlers_json
      || '["induction","reconciliation","deterministic_replay_sink"]'::jsonb,
    updated_at = clock_timestamp()
WHERE purpose = 'worker' AND status = 'active';
