-- sleepagent:transactional=true
-- Stage 2: public sleep commands and the persistent Product interaction state
-- machine.  Every query-visible effect is committed with its fenced Operation.

CREATE TABLE backend_monitoring_transition_receipts (
  transition_receipt_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  monitoring_snapshot_id TEXT NOT NULL,
  from_state TEXT NOT NULL CHECK (from_state IN ('dormant', 'active')),
  to_state TEXT NOT NULL CHECK (to_state IN ('dormant', 'active')),
  from_cas_version BIGINT NOT NULL CHECK (from_cas_version >= 0),
  to_cas_version BIGINT NOT NULL CHECK (to_cas_version >= 1),
  receipt_json JSONB NOT NULL CHECK (jsonb_typeof(receipt_json) = 'object'),
  committed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (operation_id),
  UNIQUE (transition_receipt_id, namespace_id, data_mode, subject_id),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  CHECK (from_state <> to_state),
  CHECK (to_cas_version = from_cas_version + 1),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE backend_human_facts (
  human_fact_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  night_episode_revision_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  binding_id TEXT NOT NULL,
  canonical_source TEXT NOT NULL CHECK (
    canonical_source IN ('elder_self_report', 'family_observation')
  ),
  event_at TIMESTAMPTZ NOT NULL,
  fact_sha256 TEXT NOT NULL CHECK (fact_sha256 ~ '^[0-9a-f]{64}$'),
  fact_json JSONB NOT NULL CHECK (jsonb_typeof(fact_json) = 'object'),
  committed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (operation_id),
  UNIQUE (namespace_id, data_mode, fact_sha256),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (night_episode_revision_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episode_revisions (
      night_episode_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX idx_backend_human_facts_subject
  ON backend_human_facts (
    namespace_id, data_mode, namespace_generation, subject_id,
    committed_at, human_fact_id
  );

CREATE TABLE backend_product_interactions (
  interaction_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  binding_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('elder', 'family', 'doctor')),
  night_episode_id TEXT NOT NULL,
  night_episode_revision_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (
    state IN (
      'active', 'waiting_user', 'awaiting_confirmation',
      'confirmed', 'declined', 'completed', 'failed'
    )
  ),
  current_revision BIGINT NOT NULL CHECK (current_revision >= 1),
  cas_version BIGINT NOT NULL CHECK (cas_version >= 1),
  fact_snapshot_sha256 TEXT NOT NULL CHECK (
    fact_snapshot_sha256 ~ '^[0-9a-f]{64}$'
  ),
  current_state_sha256 TEXT NOT NULL CHECK (
    current_state_sha256 ~ '^[0-9a-f]{64}$'
  ),
  interaction_json JSONB NOT NULL CHECK (
    jsonb_typeof(interaction_json) = 'object'
  ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (interaction_id, namespace_id, data_mode, subject_id),
  FOREIGN KEY (night_episode_revision_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episode_revisions (
      night_episode_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX idx_backend_product_interaction_subject
  ON backend_product_interactions (
    namespace_id, data_mode, namespace_generation, subject_id,
    updated_at DESC, interaction_id
  );

CREATE TABLE backend_product_interaction_revisions (
  interaction_revision_id TEXT PRIMARY KEY,
  interaction_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  revision_number BIGINT NOT NULL CHECK (revision_number >= 1),
  state TEXT NOT NULL CHECK (
    state IN (
      'active', 'waiting_user', 'awaiting_confirmation',
      'confirmed', 'declined', 'completed', 'failed'
    )
  ),
  fact_snapshot_sha256 TEXT NOT NULL CHECK (
    fact_snapshot_sha256 ~ '^[0-9a-f]{64}$'
  ),
  fact_snapshot_json JSONB NOT NULL CHECK (
    jsonb_typeof(fact_snapshot_json) = 'object'
  ),
  revision_sha256 TEXT NOT NULL CHECK (
    revision_sha256 ~ '^[0-9a-f]{64}$'
  ),
  revision_json JSONB NOT NULL CHECK (jsonb_typeof(revision_json) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (interaction_id, revision_number),
  UNIQUE (operation_id),
  FOREIGN KEY (interaction_id, namespace_id, data_mode, subject_id)
    REFERENCES backend_product_interactions (
      interaction_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE backend_human_decisions_v2 (
  human_decision_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  interaction_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  binding_id TEXT NOT NULL,
  choice TEXT NOT NULL CHECK (choice IN ('confirm', 'decline')),
  target_state_version BIGINT NOT NULL CHECK (target_state_version >= 1),
  target_sha256 TEXT NOT NULL CHECK (target_sha256 ~ '^[0-9a-f]{64}$'),
  authorization_epoch BIGINT NOT NULL CHECK (authorization_epoch >= 1),
  privacy_epoch BIGINT NOT NULL CHECK (privacy_epoch >= 1),
  retrieval_policy_epoch BIGINT NOT NULL CHECK (
    retrieval_policy_epoch >= 1
  ),
  decision_json JSONB NOT NULL CHECK (jsonb_typeof(decision_json) = 'object'),
  committed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (operation_id),
  UNIQUE (interaction_id),
  UNIQUE (
    human_decision_id, interaction_id, namespace_id, data_mode, subject_id
  ),
  FOREIGN KEY (interaction_id, namespace_id, data_mode, subject_id)
    REFERENCES backend_product_interactions (
      interaction_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE backend_care_actions_v2 (
  care_action_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  interaction_id TEXT NOT NULL,
  human_decision_id TEXT NOT NULL,
  source_analysis_revision_id TEXT,
  state TEXT NOT NULL CHECK (
    state IN ('confirmed_pending_delivery', 'active', 'completed', 'cancelled')
  ),
  action_sha256 TEXT NOT NULL CHECK (action_sha256 ~ '^[0-9a-f]{64}$'),
  action_json JSONB NOT NULL CHECK (jsonb_typeof(action_json) = 'object'),
  confirmed_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (interaction_id),
  UNIQUE (human_decision_id),
  FOREIGN KEY (interaction_id, namespace_id, data_mode, subject_id)
    REFERENCES backend_product_interactions (
      interaction_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (
    human_decision_id, interaction_id, namespace_id, data_mode, subject_id
  ) REFERENCES backend_human_decisions_v2 (
    human_decision_id, interaction_id, namespace_id, data_mode, subject_id
  ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE backend_reanalysis_links (
  reanalysis_link_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  source_operation_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  night_episode_revision_id TEXT NOT NULL,
  product_operation_id TEXT NOT NULL,
  trigger_kind TEXT NOT NULL CHECK (
    trigger_kind IN ('human_feedback', 'explicit_reanalysis')
  ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (source_operation_id),
  UNIQUE (product_operation_id),
  FOREIGN KEY (source_operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (product_operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (night_episode_revision_id, namespace_id, data_mode)
    REFERENCES sleep_domain_night_episode_revisions (
      night_episode_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

-- Append-only evidence, decisions and receipts must not be rewritten by either
-- API or Worker roles after commit.
CREATE TRIGGER backend_monitoring_transition_receipt_immutable
BEFORE UPDATE OR DELETE ON backend_monitoring_transition_receipts
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_demo_append_only_mutation();

CREATE TRIGGER backend_human_fact_immutable
BEFORE UPDATE OR DELETE ON backend_human_facts
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_demo_append_only_mutation();

CREATE TRIGGER backend_product_interaction_revision_immutable
BEFORE UPDATE OR DELETE ON backend_product_interaction_revisions
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_demo_append_only_mutation();

CREATE TRIGGER backend_human_decision_v2_immutable
BEFORE UPDATE OR DELETE ON backend_human_decisions_v2
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_demo_append_only_mutation();

CREATE TRIGGER backend_reanalysis_link_immutable
BEFORE UPDATE OR DELETE ON backend_reanalysis_links
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_demo_append_only_mutation();

-- A Worker can execute a queued user command only while both the originating
-- actor authority and its own exact handler grant remain current.
CREATE OR REPLACE FUNCTION sleepagent_stage2_authority_allows(
  requested_operation_id TEXT,
  requested_queue TEXT,
  requested_scope TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT
    NULLIF(current_setting('sleepagent.process_role', TRUE), '') = 'worker'
    AND requested_queue IN ('sleep_command', 'product_interaction')
    AND requested_scope = CASE operation.operation_type
      WHEN 'sleep_api.monitoring.activate.v1' THEN 'sleep:monitoring:write'
      WHEN 'sleep_api.monitoring.deactivate.v1' THEN 'sleep:monitoring:write'
      WHEN 'sleep_api.feedback.elder.v1' THEN 'sleep:feedback:self'
      WHEN 'sleep_api.feedback.family.v1' THEN 'sleep:feedback:family'
      WHEN 'sleep_api.reanalysis.v1' THEN 'sleep:reanalysis:write'
      WHEN 'interaction.start' THEN 'product:sleep:interaction:write'
      WHEN 'interaction.ask' THEN 'product:sleep:interaction:write'
      WHEN 'interaction.answer' THEN 'product:sleep:interaction:answer'
      WHEN 'interaction.confirm' THEN 'product:sleep:care:confirm'
      WHEN 'interaction.decline' THEN 'product:sleep:care:confirm'
      WHEN 'interaction.feedback' THEN 'product:sleep:feedback:write'
      ELSE NULL
    END
    AND operation.queue_name = requested_queue
    AND operation.origin_kind = 'user'
    AND operation.authorization_snapshot_json ->> 'authorization_epoch' =
      epoch_row.authorization_epoch::text
    AND operation.authorization_snapshot_json ->> 'privacy_epoch' =
      epoch_row.privacy_epoch::text
    AND operation.authorization_snapshot_json ->> 'retrieval_policy_epoch' =
      epoch_row.retrieval_policy_epoch::text
    AND operation.authorization_snapshot_json -> 'effective_scopes'
      ? requested_scope
    AND binding_row.scopes_json ? requested_scope
    AND caller_grant.scopes_json ? requested_scope
    AND EXISTS (
      SELECT 1
      FROM public.backend_principal_grants AS worker_grant
      WHERE worker_grant.principal_id = NULLIF(
          current_setting('sleepagent.service_principal_id', TRUE), ''
        )
        AND worker_grant.namespace_id = operation.namespace_id
        AND worker_grant.data_mode = operation.data_mode
        AND worker_grant.purpose = NULLIF(
          current_setting('sleepagent.purpose', TRUE), ''
        )
        AND worker_grant.authorization_epoch = epoch_row.authorization_epoch
        AND worker_grant.status = 'active'
        AND worker_grant.valid_from <= clock_timestamp()
        AND (worker_grant.valid_until IS NULL
          OR worker_grant.valid_until > clock_timestamp())
        AND worker_grant.allowed_handlers_json ? requested_queue
    )
  FROM public.sleep_domain_operations AS operation
  JOIN public.backend_subject_epochs AS epoch_row
    ON epoch_row.namespace_id = operation.namespace_id
   AND epoch_row.data_mode = operation.data_mode
   AND epoch_row.subject_id = operation.subject_id
  JOIN public.backend_actor_subject_bindings AS binding_row
    ON binding_row.binding_id =
       operation.authorization_snapshot_json ->> 'binding_id'
   AND binding_row.namespace_id = operation.namespace_id
   AND binding_row.data_mode = operation.data_mode
   AND binding_row.subject_id = operation.subject_id
   AND binding_row.actor_id = operation.actor_id
   AND binding_row.role = operation.authorization_snapshot_json ->> 'role'
  JOIN public.backend_actors AS actor_row
    ON actor_row.actor_id = binding_row.actor_id
  JOIN public.backend_subjects AS subject_row
    ON subject_row.namespace_id = operation.namespace_id
   AND subject_row.data_mode = operation.data_mode
   AND subject_row.subject_id = operation.subject_id
  JOIN public.backend_service_principals AS caller_principal
    ON caller_principal.principal_id = operation.service_principal_id
  JOIN public.backend_principal_grants AS caller_grant
    ON caller_grant.principal_id = operation.service_principal_id
   AND caller_grant.namespace_id = operation.namespace_id
   AND caller_grant.data_mode = operation.data_mode
   AND caller_grant.purpose =
       operation.authorization_snapshot_json ->> 'purpose'
  WHERE operation.operation_id = requested_operation_id
    AND operation.namespace_id = NULLIF(
      current_setting('sleepagent.namespace_id', TRUE), ''
    )
    AND operation.data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    )
    AND operation.namespace_generation::text = NULLIF(
      current_setting('sleepagent.namespace_generation', TRUE), ''
    )
    AND operation.subject_id = NULLIF(
      current_setting('sleepagent.subject_id', TRUE), ''
    )
    AND operation.service_principal_id =
      operation.authorization_snapshot_json ->> 'principal_id'
    AND operation.actor_id =
      operation.authorization_snapshot_json ->> 'actor_id'
    AND binding_row.authorization_epoch = epoch_row.authorization_epoch
    AND binding_row.status = 'active'
    AND binding_row.valid_from <= clock_timestamp()
    AND (binding_row.valid_until IS NULL
      OR binding_row.valid_until > clock_timestamp())
    AND binding_row.purpose_json
      ? (operation.authorization_snapshot_json ->> 'purpose')
    AND actor_row.status = 'active'
    AND subject_row.status = 'active'
    AND caller_principal.status = 'active'
    AND caller_grant.authorization_epoch = epoch_row.authorization_epoch
    AND caller_grant.status = 'active'
    AND caller_grant.valid_from <= clock_timestamp()
    AND (caller_grant.valid_until IS NULL
      OR caller_grant.valid_until > clock_timestamp())
$$;

REVOKE ALL ON FUNCTION sleepagent_stage2_authority_allows(TEXT, TEXT, TEXT)
  FROM PUBLIC;

-- Handler grants are queue capabilities.  The original foundation compared
-- the allowlist with operation_type, which made a one-handler/many-command
-- state machine impossible to express.  Preserve the claim protocol while
-- correcting that comparison to the requested queue.
CREATE OR REPLACE FUNCTION sleepagent_claim_operation(
  requested_queue TEXT,
  claimant_worker_instance TEXT,
  requested_lease_seconds INTEGER
)
RETURNS TABLE (
  operation_id TEXT,
  namespace_id TEXT,
  data_mode TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT,
  operation_type TEXT,
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
  caller_process_role TEXT := NULLIF(
    current_setting('sleepagent.process_role', TRUE), ''
  );
BEGIN
  IF principal IS NULL OR deployment_mode NOT IN ('live', 'replay')
     OR request_purpose IS NULL OR caller_process_role <> 'worker'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION
      'trusted worker principal/data mode/purpose context is required';
  END IF;
  IF requested_queue IS NULL OR requested_queue = ''
     OR claimant_worker_instance IS NULL OR claimant_worker_instance = ''
     OR requested_lease_seconds < 1 OR requested_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'invalid operation claim parameters';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT op.operation_id,
      epoch_row.authorization_epoch AS claim_authorization_epoch,
      epoch_row.privacy_epoch AS claim_privacy_epoch,
      epoch_row.retrieval_policy_epoch AS claim_retrieval_policy_epoch
    FROM public.sleep_domain_operations AS op
    JOIN public.backend_namespaces AS ns
      ON ns.namespace_id = op.namespace_id
     AND ns.data_mode = op.data_mode
    JOIN public.backend_subject_epochs AS epoch_row
      ON epoch_row.namespace_id = op.namespace_id
     AND epoch_row.data_mode = op.data_mode
     AND epoch_row.subject_id = op.subject_id
    WHERE op.protocol_version >= 2
      AND op.data_mode = deployment_mode
      AND op.queue_name = requested_queue
      AND (
        op.status IN ('pending', 'retry')
        OR (op.status = 'running'
          AND op.lease_expires_at <= clock_timestamp())
      )
      AND op.available_at <= clock_timestamp()
      AND op.attempt_count < op.max_attempts
      AND (
        op.operation_type <> 'fast_path'
        OR op.data_mode <> 'replay'
        OR NOT EXISTS (
          SELECT 1
          FROM public.sleep_domain_normalization_work AS pending_normalization
          WHERE pending_normalization.namespace_id = op.namespace_id
            AND pending_normalization.data_mode = op.data_mode
            AND pending_normalization.namespace_generation =
              op.namespace_generation
            AND pending_normalization.subject_id = op.subject_id
            AND COALESCE(pending_normalization.run_id, '') =
              COALESCE(op.run_id, '')
            AND COALESCE(pending_normalization.arm_id, '') =
              COALESCE(op.arm_id, '')
            AND pending_normalization.status <> 'succeeded'
        )
      )
      AND ns.status = 'active'
      AND COALESCE(
        op.authorization_snapshot_json,
        op.workload_authorization_snapshot_json
      ) ->> 'authorization_epoch' = epoch_row.authorization_epoch::text
      AND COALESCE(
        op.authorization_snapshot_json,
        op.workload_authorization_snapshot_json
      ) ->> 'privacy_epoch' = epoch_row.privacy_epoch::text
      AND COALESCE(
        op.authorization_snapshot_json,
        op.workload_authorization_snapshot_json
      ) ->> 'retrieval_policy_epoch' =
        epoch_row.retrieval_policy_epoch::text
      AND (
        SELECT COUNT(*)
        FROM public.sleep_domain_operations AS active
        WHERE active.namespace_id = op.namespace_id
          AND active.data_mode = op.data_mode
          AND active.protocol_version >= 2
          AND active.status = 'running'
          AND active.lease_expires_at > clock_timestamp()
      ) < ns.max_worker_concurrency
      AND EXISTS (
        SELECT 1
        FROM public.backend_principal_grants AS grant_row
        WHERE grant_row.principal_id = principal
          AND grant_row.namespace_id = op.namespace_id
          AND grant_row.data_mode = op.data_mode
          AND grant_row.purpose = request_purpose
          AND grant_row.authorization_epoch =
            epoch_row.authorization_epoch
          AND grant_row.status = 'active'
          AND grant_row.valid_from <= clock_timestamp()
          AND (
            grant_row.valid_until IS NULL
            OR grant_row.valid_until > clock_timestamp()
          )
          AND grant_row.allowed_handlers_json ? requested_queue
          AND EXISTS (
            SELECT 1
            FROM public.backend_service_principals AS service_principal
            WHERE service_principal.principal_id = principal
              AND service_principal.status = 'active'
          )
      )
    ORDER BY op.priority DESC, op.available_at, op.created_at, op.operation_id
    FOR UPDATE OF ns, op SKIP LOCKED
    LIMIT 1
  ), claimed AS (
    UPDATE public.sleep_domain_operations AS op
    SET status = 'running',
        attempt_count = op.attempt_count + 1,
        lease_generation = op.lease_generation + 1,
        fencing_token = gen_random_uuid()::text,
        worker_instance = claimant_worker_instance,
        lease_owner = claimant_worker_instance,
        heartbeat_at = clock_timestamp(),
        lease_expires_at = clock_timestamp()
          + make_interval(secs => requested_lease_seconds),
        updated_at = clock_timestamp()
    FROM candidate
    WHERE op.operation_id = candidate.operation_id
    RETURNING op.operation_id, op.namespace_id, op.data_mode,
      op.namespace_generation, op.run_id, op.arm_id, op.subject_id,
      op.operation_type, candidate.claim_authorization_epoch,
      candidate.claim_privacy_epoch, candidate.claim_retrieval_policy_epoch,
      op.lease_generation, op.fencing_token
  )
  SELECT claimed.operation_id, claimed.namespace_id, claimed.data_mode,
    claimed.namespace_generation, claimed.run_id, claimed.arm_id,
    claimed.subject_id, claimed.operation_type,
    claimed.claim_authorization_epoch, claimed.claim_privacy_epoch,
    claimed.claim_retrieval_policy_epoch,
    claimed.lease_generation, claimed.fencing_token
  FROM claimed;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_claim_operation(TEXT, TEXT, INTEGER)
  FROM PUBLIC;

-- Existing Stage-1 grants remain exact at version 2.  Version 3 deliberately
-- expands only the two now-real Stage-2 queue handlers.
UPDATE backend_principal_grants
SET allowed_handlers_json =
      allowed_handlers_json || '["sleep_command","product_interaction"]'::jsonb,
    updated_at = clock_timestamp()
WHERE principal_id = 'sleepagent-worker-test'
  AND data_mode = 'replay'
  AND purpose = 'worker'
  AND status = 'active';

ALTER TABLE backend_monitoring_transition_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_monitoring_transition_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_monitoring_transition_receipt_scope
  ON backend_monitoring_transition_receipts
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_human_facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_human_facts FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_human_fact_scope ON backend_human_facts
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_product_interactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_product_interactions FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_product_interaction_scope ON backend_product_interactions
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_product_interaction_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_product_interaction_revisions FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_product_interaction_revision_scope
  ON backend_product_interaction_revisions
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_human_decisions_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_human_decisions_v2 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_human_decision_v2_scope ON backend_human_decisions_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_care_actions_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_care_actions_v2 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_care_action_v2_scope ON backend_care_actions_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_reanalysis_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_reanalysis_links FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_reanalysis_link_scope ON backend_reanalysis_links
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));
