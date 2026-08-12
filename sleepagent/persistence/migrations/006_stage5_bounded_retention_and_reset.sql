-- sleepagent:transactional=true
-- Stage 5: activate the inherited retention substrate for bounded replay raw
-- crypto-shred.  Reset coordination is declared later in this same release.

INSERT INTO backend_retention_classes (
  retention_class, policy_version, retention_seconds, expiry_action,
  legal_hold_supported, configuration_sha256, active
) VALUES (
  'replay_raw_short_v1', 'bounded-replay-raw.v1', 2, 'crypto_shred',
  FALSE, 'b22a2dc23dd7a9d8a3d5651afca933ed0fba3d9edf4a45c249db71962f889355',
  TRUE
)
ON CONFLICT (retention_class) DO UPDATE
SET policy_version = EXCLUDED.policy_version,
    retention_seconds = EXCLUDED.retention_seconds,
    expiry_action = EXCLUDED.expiry_action,
    legal_hold_supported = EXCLUDED.legal_hold_supported,
    configuration_sha256 = EXCLUDED.configuration_sha256,
    active = TRUE,
    updated_at = clock_timestamp();

DROP INDEX ux_backend_retention_dek_active;
CREATE UNIQUE INDEX ux_backend_retention_dek_active
  ON backend_retention_deks (
    namespace_id, data_mode, namespace_generation, subject_id,
    retention_domain
  )
  WHERE status = 'active';

ALTER TABLE backend_retention_bindings
  ADD CONSTRAINT backend_retention_binding_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT;

ALTER TABLE backend_retention_bindings
  ADD CONSTRAINT backend_retention_binding_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT;

ALTER TABLE sleep_domain_raw_inbox
  VALIDATE CONSTRAINT sleep_domain_raw_encryption_v2_contract;
ALTER TABLE sleep_domain_raw_inbox
  VALIDATE CONSTRAINT sleep_domain_raw_encryption_v2_dek_fk;

ALTER TABLE backend_shred_receipts
  ADD CONSTRAINT backend_shred_receipt_v2_privacy_contract CHECK (
    receipt_json ->> 'schema_version' <> 'shred_receipt.v2'
    OR (
      receipt_json ->> 'retention_domain' IS NOT NULL
      AND receipt_json ->> 'reason_code' IS NOT NULL
      AND NOT receipt_json ?| ARRAY[
        'subject_id', 'subject_pseudonym', 'raw_ingress_record_id',
        'encrypted_payload', 'plaintext'
      ]
    )
  );

CREATE OR REPLACE FUNCTION sleepagent_protect_retention_binding()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'retention bindings cannot be deleted';
  END IF;
  IF TG_OP = 'UPDATE' AND (
    NEW.retention_binding_id <> OLD.retention_binding_id
    OR NEW.namespace_id <> OLD.namespace_id
    OR NEW.data_mode <> OLD.data_mode
    OR NEW.namespace_generation <> OLD.namespace_generation
    OR NEW.run_id IS DISTINCT FROM OLD.run_id
    OR NEW.arm_id IS DISTINCT FROM OLD.arm_id
    OR NEW.subject_id <> OLD.subject_id
    OR NEW.retention_domain <> OLD.retention_domain
    OR NEW.dek_generation <> OLD.dek_generation
    OR NEW.retention_class <> OLD.retention_class
    OR NEW.resource_type <> OLD.resource_type
    OR NEW.resource_id <> OLD.resource_id
    OR NEW.expires_at <> OLD.expires_at
    OR NEW.created_at <> OLD.created_at
  ) THEN
    RAISE EXCEPTION 'retention binding identity and deadline are immutable';
  END IF;
  IF TG_OP = 'INSERT' AND NOT EXISTS (
    SELECT 1
    FROM public.backend_retention_deks AS dek
    WHERE dek.namespace_id = NEW.namespace_id
      AND dek.data_mode = NEW.data_mode
      AND dek.namespace_generation = NEW.namespace_generation
      AND dek.run_id IS NOT DISTINCT FROM NEW.run_id
      AND dek.arm_id IS NOT DISTINCT FROM NEW.arm_id
      AND dek.subject_id = NEW.subject_id
      AND dek.retention_domain = NEW.retention_domain
      AND dek.generation = NEW.dek_generation
      AND dek.status = 'active'
      AND dek.wrapped_dek IS NOT NULL
  ) THEN
    RAISE EXCEPTION 'new retention binding requires an active scoped DEK';
  END IF;
  NEW.updated_at := clock_timestamp();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS backend_retention_binding_guard
  ON backend_retention_bindings;
CREATE TRIGGER backend_retention_binding_guard
BEFORE INSERT OR UPDATE OR DELETE ON backend_retention_bindings
FOR EACH ROW EXECUTE FUNCTION sleepagent_protect_retention_binding();

CREATE TABLE backend_demo_resets_v2 (
  reset_id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL UNIQUE,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  source_namespace_generation BIGINT NOT NULL CHECK (
    source_namespace_generation >= 1
  ),
  target_namespace_generation BIGINT NOT NULL CHECK (
    target_namespace_generation = source_namespace_generation + 1
  ),
  source_run_id TEXT NOT NULL,
  source_arm_id TEXT NOT NULL,
  target_run_id TEXT NOT NULL,
  target_arm_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed')),
  required_dek_count INTEGER NOT NULL DEFAULT 0 CHECK (required_dek_count >= 0),
  draft_json JSONB NOT NULL CHECK (jsonb_typeof(draft_json) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  completed_at TIMESTAMPTZ,
  UNIQUE (namespace_id, data_mode, target_namespace_generation),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (namespace_id, data_mode, target_namespace_generation)
    REFERENCES backend_namespace_generations (
      namespace_id, data_mode, generation
    ) ON DELETE RESTRICT,
  FOREIGN KEY (
    namespace_id, target_namespace_generation, target_run_id, target_arm_id
  ) REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT,
  CHECK (draft_json ->> 'schema_version' = 'demo_reset_draft.v1'),
  CHECK (
    (status = 'pending' AND completed_at IS NULL)
    OR (status IN ('succeeded', 'failed') AND completed_at IS NOT NULL)
  )
);

CREATE TABLE backend_demo_reset_key_events_v2 (
  event_id TEXT PRIMARY KEY,
  reset_id TEXT NOT NULL REFERENCES backend_demo_resets_v2(reset_id)
    ON DELETE RESTRICT,
  retention_job_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL,
  run_id TEXT NOT NULL,
  arm_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  retention_domain TEXT NOT NULL,
  dek_generation BIGINT NOT NULL,
  lease_generation BIGINT NOT NULL,
  fencing_token TEXT NOT NULL,
  phase TEXT NOT NULL CHECK (
    phase IN ('provider_call_reserved', 'provider_call_committed')
  ),
  event_json JSONB NOT NULL CHECK (jsonb_typeof(event_json) = 'object'),
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (retention_job_id, lease_generation, phase),
  FOREIGN KEY (
    retention_job_id, namespace_id, data_mode, subject_id,
    retention_domain, dek_generation
  ) REFERENCES backend_retention_jobs (
    retention_job_id, namespace_id, data_mode, subject_id,
    retention_domain, dek_generation
  ) ON DELETE RESTRICT
);

CREATE TABLE backend_demo_reset_key_receipts_v2 (
  key_receipt_id TEXT PRIMARY KEY,
  reset_id TEXT NOT NULL REFERENCES backend_demo_resets_v2(reset_id)
    ON DELETE RESTRICT,
  retention_job_id TEXT NOT NULL UNIQUE,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL,
  run_id TEXT NOT NULL,
  arm_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  source_namespace_generation BIGINT NOT NULL,
  retention_domain TEXT NOT NULL,
  dek_generation BIGINT NOT NULL,
  lease_generation BIGINT NOT NULL,
  fencing_token TEXT NOT NULL,
  object_count BIGINT NOT NULL CHECK (object_count >= 0),
  receipt_json JSONB NOT NULL CHECK (
    jsonb_typeof(receipt_json) = 'object'
    AND receipt_json ->> 'schema_version' = 'demo_reset_key_receipt.v1'
    AND NOT receipt_json ?| ARRAY[
      'subject_id', 'subject_pseudonym', 'raw_ingress_record_id',
      'encrypted_payload', 'plaintext'
    ]
  ),
  completed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (
    namespace_id, data_mode, source_namespace_generation,
    retention_domain, dek_generation
  ),
  FOREIGN KEY (
    retention_job_id, namespace_id, data_mode, subject_id,
    retention_domain, dek_generation
  ) REFERENCES backend_retention_jobs (
    retention_job_id, namespace_id, data_mode, subject_id,
    retention_domain, dek_generation
  ) ON DELETE RESTRICT
);

CREATE TABLE backend_demo_reset_receipts_v2 (
  reset_receipt_id TEXT PRIMARY KEY,
  reset_id TEXT NOT NULL UNIQUE REFERENCES backend_demo_resets_v2(reset_id)
    ON DELETE RESTRICT,
  operation_id TEXT NOT NULL UNIQUE,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL,
  run_id TEXT NOT NULL,
  arm_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  lease_generation BIGINT NOT NULL,
  fencing_token TEXT NOT NULL,
  receipt_json JSONB NOT NULL CHECK (
    jsonb_typeof(receipt_json) = 'object'
    AND receipt_json ->> 'schema_version' = 'demo_reset_receipt.v1'
    AND NOT receipt_json ?| ARRAY[
      'subject_id', 'subject_pseudonym', 'raw_ingress_record_id',
      'encrypted_payload', 'plaintext'
    ]
  ),
  completed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT
);

ALTER TABLE backend_demo_resets_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_demo_resets_v2 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_demo_reset_v2_scope ON backend_demo_resets_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, target_namespace_generation,
    target_run_id, target_arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, target_namespace_generation,
    target_run_id, target_arm_id
  ));

ALTER TABLE backend_demo_reset_key_events_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_demo_reset_key_events_v2 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_demo_reset_key_event_v2_scope
  ON backend_demo_reset_key_events_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_demo_reset_key_receipts_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_demo_reset_key_receipts_v2 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_demo_reset_key_receipt_v2_scope
  ON backend_demo_reset_key_receipts_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE backend_demo_reset_receipts_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_demo_reset_receipts_v2 FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_demo_reset_receipt_v2_scope
  ON backend_demo_reset_receipts_v2
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

CREATE TRIGGER backend_demo_reset_key_event_v2_immutable
BEFORE UPDATE OR DELETE ON backend_demo_reset_key_events_v2
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

CREATE TRIGGER backend_demo_reset_key_receipt_v2_immutable
BEFORE UPDATE OR DELETE ON backend_demo_reset_key_receipts_v2
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

CREATE TRIGGER backend_demo_reset_receipt_v2_immutable
BEFORE UPDATE OR DELETE ON backend_demo_reset_receipts_v2
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

CREATE OR REPLACE FUNCTION sleepagent_enforce_demo_reset_key_fence()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM public.backend_retention_jobs AS job
    WHERE job.retention_job_id = NEW.retention_job_id
      AND public.sleepagent_retention_fence_allows(
        job.retention_job_id, job.namespace_id, job.data_mode,
        job.namespace_generation, job.run_id, job.arm_id, job.subject_id,
        job.retention_domain, job.dek_generation, NEW.lease_generation,
        NEW.fencing_token
      )
  ) THEN
    RAISE EXCEPTION 'stale or unauthorized demo reset key fence';
  END IF;
  NEW.occurred_at := clock_timestamp();
  RETURN NEW;
END;
$$;

CREATE TRIGGER backend_demo_reset_key_event_v2_fence
BEFORE INSERT ON backend_demo_reset_key_events_v2
FOR EACH ROW EXECUTE FUNCTION sleepagent_enforce_demo_reset_key_fence();

CREATE OR REPLACE FUNCTION sleepagent_enforce_demo_reset_key_receipt_fence()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM public.backend_retention_jobs AS job
    WHERE job.retention_job_id = NEW.retention_job_id
      AND public.sleepagent_retention_fence_allows(
        job.retention_job_id, job.namespace_id, job.data_mode,
        job.namespace_generation, job.run_id, job.arm_id, job.subject_id,
        job.retention_domain, job.dek_generation, NEW.lease_generation,
        NEW.fencing_token
      )
  ) THEN
    RAISE EXCEPTION 'stale or unauthorized demo reset key receipt fence';
  END IF;
  NEW.completed_at := clock_timestamp();
  RETURN NEW;
END;
$$;

CREATE TRIGGER backend_demo_reset_key_receipt_v2_fence
BEFORE INSERT ON backend_demo_reset_key_receipts_v2
FOR EACH ROW EXECUTE FUNCTION sleepagent_enforce_demo_reset_key_receipt_fence();

CREATE OR REPLACE FUNCTION sleepagent_protect_retention_dek()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'retention DEK metadata cannot be deleted';
  END IF;
  IF NEW.namespace_id <> OLD.namespace_id
     OR NEW.data_mode <> OLD.data_mode
     OR NEW.subject_id <> OLD.subject_id
     OR NEW.retention_domain <> OLD.retention_domain
     OR NEW.generation <> OLD.generation
     OR NEW.dek_sha256 <> OLD.dek_sha256
     OR NEW.created_at <> OLD.created_at THEN
    RAISE EXCEPTION 'retention DEK identity is immutable';
  END IF;
  IF OLD.status = 'shredded' THEN
    RAISE EXCEPTION 'shredded retention DEK metadata is terminal';
  END IF;
  IF OLD.status = 'retired' AND NEW.status NOT IN ('retired', 'shredded') THEN
    RAISE EXCEPTION 'retired retention DEK may only transition to shredded';
  END IF;
  IF OLD.status = 'active'
     AND NEW.status NOT IN ('active', 'retired', 'shredded') THEN
    RAISE EXCEPTION 'invalid retention DEK state transition';
  END IF;
  IF NEW.status = 'shredded' AND OLD.status <> 'shredded'
     AND NOT EXISTS (
       SELECT 1
       FROM public.backend_shred_receipts AS receipt
       WHERE receipt.namespace_id = NEW.namespace_id
         AND receipt.data_mode = NEW.data_mode
         AND receipt.namespace_generation = NEW.namespace_generation
         AND receipt.run_id IS NOT DISTINCT FROM NEW.run_id
         AND receipt.arm_id IS NOT DISTINCT FROM NEW.arm_id
         AND receipt.subject_id = NEW.subject_id
         AND receipt.retention_domain = NEW.retention_domain
         AND receipt.dek_generation = NEW.generation
         AND public.sleepagent_retention_fence_allows(
           receipt.retention_job_id, receipt.namespace_id,
           receipt.data_mode, receipt.namespace_generation,
           receipt.run_id, receipt.arm_id, receipt.subject_id,
           receipt.retention_domain, receipt.dek_generation,
           receipt.lease_generation, receipt.fencing_token
         )
     )
     AND NOT EXISTS (
       SELECT 1
       FROM public.backend_demo_reset_key_receipts_v2 AS receipt
       JOIN public.backend_retention_jobs AS job
         ON job.retention_job_id = receipt.retention_job_id
       WHERE receipt.namespace_id = NEW.namespace_id
         AND receipt.data_mode = NEW.data_mode
         AND receipt.source_namespace_generation = NEW.namespace_generation
         AND receipt.subject_id = NEW.subject_id
         AND receipt.retention_domain = NEW.retention_domain
         AND receipt.dek_generation = NEW.generation
         AND public.sleepagent_retention_fence_allows(
           job.retention_job_id, job.namespace_id, job.data_mode,
           job.namespace_generation, job.run_id, job.arm_id, job.subject_id,
           job.retention_domain, job.dek_generation,
           receipt.lease_generation, receipt.fencing_token
         )
     ) THEN
    RAISE EXCEPTION
      'retention DEK shred requires a current fenced job and receipt';
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION sleepagent_reserve_demo_reset(
  requested_caller_idempotency_key TEXT,
  requested_sha256 TEXT,
  new_operation_id TEXT,
  new_reset_id TEXT,
  new_command_receipt_id TEXT,
  new_event_id TEXT,
  new_audit_id TEXT
)
RETURNS TABLE (
  reserved_operation_id TEXT,
  reserved_namespace_generation BIGINT,
  reservation_reused BOOLEAN
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  principal TEXT := NULLIF(
    current_setting('sleepagent.service_principal_id', TRUE), ''
  );
  existing_receipt public.backend_command_receipts%ROWTYPE;
  journey_row public.backend_demo_journeys%ROWTYPE;
  epoch_row public.backend_subject_epochs%ROWTYPE;
  source_generation public.backend_namespace_generations%ROWTYPE;
  source_run public.backend_replay_runs%ROWTYPE;
  source_arm public.backend_replay_arms%ROWTYPE;
  target_generation BIGINT;
  target_run_id TEXT;
  target_arm_id TEXT;
  worker_principal TEXT;
  worker_count INTEGER;
  required_count INTEGER;
  workload JSONB;
  operation_payload JSONB;
  semantic_value TEXT;
BEGIN
  IF NOT public.sleepagent_demo_workload_allows()
     OR requested_caller_idempotency_key IS NULL
     OR requested_caller_idempotency_key = ''
     OR requested_sha256 !~ '^[0-9a-f]{64}$'
     OR new_operation_id IS NULL OR new_operation_id = ''
     OR new_reset_id IS NULL OR new_reset_id = ''
     OR new_command_receipt_id IS NULL OR new_command_receipt_id = ''
     OR new_event_id IS NULL OR new_event_id = ''
     OR new_audit_id IS NULL OR new_audit_id = '' THEN
    RAISE EXCEPTION 'invalid_request';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    principal || E'\x1f/demo/v1/reset\x1f'
      || requested_caller_idempotency_key, 0
  ));
  SELECT receipt.* INTO existing_receipt
  FROM public.backend_command_receipts AS receipt
  WHERE receipt.service_principal_id = principal
    AND receipt.actor_id IS NULL
    AND receipt.route_template = '/demo/v1/reset'
    AND receipt.caller_idempotency_key = requested_caller_idempotency_key
  FOR UPDATE;
  IF FOUND THEN
    IF existing_receipt.request_sha256 <> requested_sha256 THEN
      RAISE EXCEPTION 'idempotency_conflict';
    END IF;
    RETURN QUERY SELECT existing_receipt.operation_id,
      existing_receipt.namespace_generation, TRUE;
    RETURN;
  END IF;

  SELECT journey.* INTO journey_row
  FROM public.backend_demo_journeys AS journey
  JOIN public.sleep_domain_operations AS root
    ON root.operation_id = journey.root_operation_id
  JOIN public.backend_namespaces AS namespace_row
    ON namespace_row.namespace_id = journey.namespace_id
   AND namespace_row.data_mode = journey.data_mode
   AND namespace_row.current_generation = journey.namespace_generation
   AND namespace_row.status = 'active'
  WHERE root.service_principal_id = principal
    AND root.origin_kind = 'system'
    AND journey.phase = 'succeeded'
  ORDER BY journey.created_at DESC, journey.journey_id DESC
  LIMIT 1
  FOR UPDATE OF journey, namespace_row;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'operation_not_found';
  END IF;
  IF EXISTS (
    SELECT 1
    FROM public.backend_demo_journeys AS other
    JOIN public.sleep_domain_operations AS other_root
      ON other_root.operation_id = other.root_operation_id
    JOIN public.backend_namespaces AS other_namespace
      ON other_namespace.namespace_id = other.namespace_id
     AND other_namespace.current_generation = other.namespace_generation
    WHERE other_root.service_principal_id = principal
      AND other.phase = 'succeeded'
      AND other.journey_id <> journey_row.journey_id
  ) THEN
    RAISE EXCEPTION 'operation_authority_ambiguous';
  END IF;

  SELECT * INTO STRICT epoch_row
  FROM public.backend_subject_epochs AS epoch
  WHERE epoch.namespace_id = journey_row.namespace_id
    AND epoch.data_mode = 'replay'
    AND epoch.subject_id = journey_row.subject_id
  FOR UPDATE;
  SELECT * INTO STRICT source_generation
  FROM public.backend_namespace_generations AS generation_row
  WHERE generation_row.namespace_id = journey_row.namespace_id
    AND generation_row.data_mode = 'replay'
    AND generation_row.generation = journey_row.namespace_generation
    AND generation_row.status = 'active'
  FOR UPDATE;
  SELECT * INTO STRICT source_run
  FROM public.backend_replay_runs AS run_row
  WHERE run_row.run_id = journey_row.run_id
    AND run_row.status = 'active'
  FOR UPDATE;
  SELECT * INTO STRICT source_arm
  FROM public.backend_replay_arms AS arm_row
  WHERE arm_row.arm_id = journey_row.arm_id
  FOR SHARE;
  -- Serialize the generation cutover with raw ingress and scheduled shred.
  -- After this transaction commits, old-generation UoWs fail current-generation
  -- RLS and cannot bind or shred a key behind the reset coordinator.
  PERFORM 1
  FROM public.backend_retention_deks AS dek
  WHERE dek.namespace_id = journey_row.namespace_id
    AND dek.data_mode = 'replay'
    AND dek.namespace_generation = journey_row.namespace_generation
    AND dek.subject_id = journey_row.subject_id
    AND dek.status IN ('active', 'retired')
  FOR UPDATE;
  SELECT count(*), min(worker.principal_id)
  INTO worker_count, worker_principal
  FROM public.backend_service_principals AS worker
  WHERE worker.principal_kind = 'worker' AND worker.status = 'active';
  IF worker_count <> 1 OR worker_principal IS NULL THEN
    RAISE EXCEPTION 'authorization_denied';
  END IF;

  target_generation := journey_row.namespace_generation + 1;
  target_run_id := 'reset-run:' || new_reset_id || ':g' || target_generation;
  target_arm_id := 'reset-arm:' || new_reset_id || ':g' || target_generation;

  UPDATE public.backend_namespace_generations
  SET status = 'sealed', sealed_at = clock_timestamp()
  WHERE namespace_id = journey_row.namespace_id
    AND data_mode = 'replay'
    AND generation = journey_row.namespace_generation
    AND status = 'active';
  IF NOT FOUND THEN RAISE EXCEPTION 'generation_fenced'; END IF;

  UPDATE public.backend_replay_runs
  SET status = 'reset', sealed_at = clock_timestamp()
  WHERE run_id = journey_row.run_id AND status = 'active';
  IF NOT FOUND THEN RAISE EXCEPTION 'generation_fenced'; END IF;

  UPDATE public.backend_namespaces
  SET current_generation = target_generation, updated_at = clock_timestamp()
  WHERE namespace_id = journey_row.namespace_id
    AND data_mode = 'replay'
    AND current_generation = journey_row.namespace_generation
    AND status = 'active';
  IF NOT FOUND THEN RAISE EXCEPTION 'generation_fenced'; END IF;

  INSERT INTO public.backend_namespace_generations (
    namespace_id, data_mode, generation, status, configuration_sha256
  ) VALUES (
    journey_row.namespace_id, 'replay', target_generation, 'active',
    source_generation.configuration_sha256
  );
  INSERT INTO public.backend_replay_runs (
    run_id, namespace_id, data_mode, namespace_generation, scenario_id,
    scenario_sha256, generation, status, synthetic_non_release
  ) VALUES (
    target_run_id, journey_row.namespace_id, 'replay', target_generation,
    source_run.scenario_id, source_run.scenario_sha256,
    target_generation::INTEGER, 'active', TRUE
  );
  INSERT INTO public.backend_replay_arms (
    arm_id, run_id, namespace_id, data_mode, namespace_generation,
    arm_name, configuration_sha256
  ) VALUES (
    target_arm_id, target_run_id, journey_row.namespace_id, 'replay',
    target_generation, 'control', source_arm.configuration_sha256
  );

  UPDATE public.backend_subject_epochs
  SET authorization_epoch = authorization_epoch + 1,
      privacy_epoch = privacy_epoch + 1,
      retrieval_policy_epoch = retrieval_policy_epoch + 1,
      cas_version = cas_version + 1,
      updated_at = clock_timestamp()
  WHERE namespace_id = journey_row.namespace_id
    AND data_mode = 'replay' AND subject_id = journey_row.subject_id
  RETURNING * INTO epoch_row;

  UPDATE public.backend_actor_subject_bindings
  SET authorization_epoch = epoch_row.authorization_epoch,
      updated_at = clock_timestamp()
  WHERE namespace_id = journey_row.namespace_id
    AND data_mode = 'replay' AND subject_id = journey_row.subject_id
    AND status = 'active';
  UPDATE public.backend_principal_grants
  SET authorization_epoch = epoch_row.authorization_epoch,
      allowed_handlers_json = CASE
        WHEN purpose = 'worker' THEN allowed_handlers_json
          || '["demo_reset","retention"]'::jsonb
        ELSE allowed_handlers_json
      END,
      updated_at = clock_timestamp()
  WHERE namespace_id = journey_row.namespace_id
    AND data_mode = 'replay' AND status = 'active';
  UPDATE public.backend_pending_handles
  SET status = 'revoked', cas_version = cas_version + 1
  WHERE namespace_id = journey_row.namespace_id
    AND data_mode = 'replay'
    AND namespace_generation = journey_row.namespace_generation
    AND subject_id = journey_row.subject_id AND status = 'pending';

  workload := jsonb_build_object(
    'schema_version', 'workload_authorization_snapshot.v1',
    'workload_principal_id', worker_principal,
    'namespace_id', journey_row.namespace_id,
    'namespace_generation', target_generation,
    'data_mode', 'replay', 'run_id', target_run_id, 'arm_id', target_arm_id,
    'subject_id', journey_row.subject_id, 'purpose', 'worker',
    'allowed_handler', 'demo_reset',
    'authorization_epoch', epoch_row.authorization_epoch,
    'privacy_epoch', epoch_row.privacy_epoch,
    'retrieval_policy_epoch', epoch_row.retrieval_policy_epoch
  );
  semantic_value := encode(digest(
    journey_row.namespace_id || E'\x1f' || target_generation::text
      || E'\x1f' || new_reset_id, 'sha256'
  ), 'hex');
  operation_payload := jsonb_build_object(
    'schema_version', 'backend_operation.v2',
    'command_type', 'demo.reset.v1',
    'payload', jsonb_build_object(
      'reset_id', new_reset_id,
      'source_namespace_generation', journey_row.namespace_generation,
      'target_namespace_generation', target_generation
    ),
    'correlation_id', new_operation_id,
    'generation', target_generation,
    'authorization_snapshot', workload
  );
  INSERT INTO public.sleep_domain_operations (
    operation_id, namespace_id, data_mode, operation_type, subject_id,
    service_principal_id, actor_id, target_resource_id, target_resource_key,
    idempotency_key, request_sha256, status, attempt_count, cas_version,
    operation_json, created_at, updated_at, protocol_version,
    namespace_generation, run_id, arm_id, id_scheme, origin_kind,
    semantic_key, queue_name, priority, available_at, max_attempts,
    workload_authorization_snapshot_json, policy_sha256
  ) VALUES (
    new_operation_id, journey_row.namespace_id, 'replay', 'demo_reset',
    journey_row.subject_id, principal, NULL, new_reset_id,
    'demo-reset:' || target_generation::text,
    requested_caller_idempotency_key,
    requested_sha256, 'pending', 0, 0, operation_payload,
    clock_timestamp(), clock_timestamp(), 2, target_generation,
    target_run_id, target_arm_id, 'uuidv7', 'system', semantic_value,
    'demo_reset', 110, clock_timestamp(), 20, workload,
    journey_row.policy_sha256
  );

  SELECT count(*) INTO required_count
  FROM public.backend_retention_deks AS dek
  WHERE dek.namespace_id = journey_row.namespace_id
    AND dek.data_mode = 'replay'
    AND dek.namespace_generation = journey_row.namespace_generation
    AND dek.subject_id = journey_row.subject_id
    AND dek.status IN ('active', 'retired');

  INSERT INTO public.backend_demo_resets_v2 (
    reset_id, operation_id, namespace_id, data_mode,
    source_namespace_generation, target_namespace_generation,
    source_run_id, source_arm_id, target_run_id, target_arm_id, subject_id,
    status, required_dek_count, draft_json
  ) VALUES (
    new_reset_id, new_operation_id, journey_row.namespace_id, 'replay',
    journey_row.namespace_generation, target_generation,
    journey_row.run_id, journey_row.arm_id, target_run_id, target_arm_id,
    journey_row.subject_id, 'pending', required_count,
    jsonb_build_object(
      'schema_version', 'demo_reset_draft.v1',
      'source_generation', journey_row.namespace_generation,
      'target_generation', target_generation,
      'required_domain_key_count', required_count,
      'reason_code', 'replay_subject_forget'
    )
  );

  INSERT INTO public.backend_retention_jobs (
    retention_job_id, namespace_id, data_mode, namespace_generation,
    run_id, arm_id, subject_id, retention_domain, dek_generation,
    semantic_key, job_kind, status, priority, available_at,
    authorization_snapshot_json, job_json
  )
  SELECT gen_random_uuid()::text, dek.namespace_id, dek.data_mode,
    target_generation, target_run_id, target_arm_id, dek.subject_id,
    dek.retention_domain, dek.generation,
    encode(digest(new_reset_id || E'\x1f' || dek.retention_domain
      || E'\x1f' || dek.generation::text, 'sha256'), 'hex'),
    'subject_forget', 'pending', 120, clock_timestamp(),
    workload || jsonb_build_object('allowed_handler', 'retention'),
    jsonb_build_object(
      'schema_version', 'retention_job.v1',
      'job_kind', 'subject_forget',
      'retention_domain', dek.retention_domain,
      'dek_generation', dek.generation,
      'policy_version', 'bounded-replay-forget.v1',
      'reset_id', new_reset_id,
      'source_namespace_generation', journey_row.namespace_generation,
      'authorization_snapshot', workload
        || jsonb_build_object('allowed_handler', 'retention')
    )
  FROM public.backend_retention_deks AS dek
  WHERE dek.namespace_id = journey_row.namespace_id
    AND dek.data_mode = 'replay'
    AND dek.namespace_generation = journey_row.namespace_generation
    AND dek.subject_id = journey_row.subject_id
    AND dek.status IN ('active', 'retired');

  INSERT INTO public.backend_command_receipts (
    command_receipt_id, namespace_id, data_mode, namespace_generation,
    run_id, arm_id, service_principal_id, actor_id, subject_id,
    route_template, caller_idempotency_key, request_sha256, operation_id,
    authorization_snapshot_json, receipt_json, origin_kind,
    workload_authorization_snapshot_json
  ) VALUES (
    new_command_receipt_id, journey_row.namespace_id, 'replay',
    target_generation, target_run_id, target_arm_id, principal, NULL,
    journey_row.subject_id, '/demo/v1/reset',
    requested_caller_idempotency_key,
    requested_sha256, new_operation_id, NULL,
    jsonb_build_object(
      'schema_version', 'demo_reset_command_receipt.v1',
      'operation_id', new_operation_id,
      'generation', target_generation,
      'route_template', '/demo/v1/reset'
    ), 'system', workload
  );
  INSERT INTO public.sleep_domain_domain_outbox (
    event_id, namespace_id, data_mode, event_type, aggregate_type,
    aggregate_id, aggregate_version, per_aggregate_sequence, subject_id,
    operation_id, status, available_at, event_json, created_at,
    protocol_version, namespace_generation, run_id, arm_id
  ) VALUES (
    new_event_id, journey_row.namespace_id, 'replay', 'DEMO_RESET_ACCEPTED',
    'Operation', new_operation_id, 1, 1, journey_row.subject_id,
    new_operation_id, 'committed', clock_timestamp(),
    jsonb_build_object(
      'schema_version', 'committed_event.v2',
      'event_type', 'DEMO_RESET_ACCEPTED',
      'operation_id', new_operation_id,
      'generation', target_generation,
      'data_mode', 'replay', 'synthetic_non_release', TRUE
    ), clock_timestamp(), 2, target_generation, target_run_id, target_arm_id
  );
  INSERT INTO public.backend_authorization_audit (
    audit_id, namespace_id, data_mode, subject_id, principal_id, actor_id,
    binding_id, decision, reason_code, policy_sha256, authorization_epoch,
    privacy_epoch, retrieval_policy_epoch, audit_json, occurred_at
  ) VALUES (
    new_audit_id, journey_row.namespace_id, 'replay', journey_row.subject_id,
    principal, NULL, NULL, 'allow', 'demo_reset_generation_bumped',
    journey_row.policy_sha256, epoch_row.authorization_epoch,
    epoch_row.privacy_epoch, epoch_row.retrieval_policy_epoch,
    jsonb_build_object(
      'schema_version', 'demo_reset_audit.v1',
      'operation_id', new_operation_id,
      'source_generation', journey_row.namespace_generation,
      'target_generation', target_generation,
      'required_domain_key_count', required_count,
      'reason_code', 'replay_subject_forget'
    ), clock_timestamp()
  );
  RETURN QUERY SELECT new_operation_id, target_generation, FALSE;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_reserve_demo_reset(
  TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_prepare_demo_reset_key(
  target_retention_job_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  new_event_id TEXT
)
RETURNS TABLE (
  reset_id TEXT,
  source_namespace_generation BIGINT,
  retention_domain TEXT,
  dek_generation BIGINT,
  kek_key_id TEXT,
  wrapping_algorithm TEXT,
  wrapped_dek BYTEA,
  dek_sha256 TEXT,
  object_count BIGINT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  job_row public.backend_retention_jobs%ROWTYPE;
  reset_row public.backend_demo_resets_v2%ROWTYPE;
  dek_row public.backend_retention_deks%ROWTYPE;
  counted BIGINT;
BEGIN
  SELECT * INTO job_row
  FROM public.backend_retention_jobs AS job
  WHERE job.retention_job_id = target_retention_job_id
  FOR UPDATE;
  IF NOT FOUND OR job_row.job_kind <> 'subject_forget'
     OR NOT public.sleepagent_retention_fence_allows(
       job_row.retention_job_id, job_row.namespace_id, job_row.data_mode,
       job_row.namespace_generation, job_row.run_id, job_row.arm_id,
       job_row.subject_id, job_row.retention_domain, job_row.dek_generation,
       expected_lease_generation, expected_fencing_token
     ) THEN
    RAISE EXCEPTION 'stale or unauthorized demo reset key fence';
  END IF;
  SELECT * INTO STRICT reset_row
  FROM public.backend_demo_resets_v2 AS reset
  WHERE reset.reset_id = job_row.job_json ->> 'reset_id'
    AND reset.namespace_id = job_row.namespace_id
    AND reset.target_namespace_generation = job_row.namespace_generation
    AND reset.subject_id = job_row.subject_id
    AND reset.status = 'pending'
  FOR SHARE;
  SELECT * INTO STRICT dek_row
  FROM public.backend_retention_deks AS dek
  WHERE dek.namespace_id = job_row.namespace_id
    AND dek.data_mode = job_row.data_mode
    AND dek.namespace_generation = reset_row.source_namespace_generation
    AND dek.subject_id = job_row.subject_id
    AND dek.retention_domain = job_row.retention_domain
    AND dek.generation = job_row.dek_generation
    AND dek.status IN ('active', 'retired')
    AND dek.wrapped_dek IS NOT NULL
  FOR UPDATE;
  IF dek_row.status = 'active' THEN
    UPDATE public.backend_retention_deks AS target_dek
    SET status = 'retired', retired_at = clock_timestamp()
    WHERE target_dek.namespace_id = dek_row.namespace_id
      AND target_dek.data_mode = dek_row.data_mode
      AND target_dek.subject_id = dek_row.subject_id
      AND target_dek.retention_domain = dek_row.retention_domain
      AND target_dek.generation = dek_row.generation
      AND target_dek.status = 'active';
    IF NOT FOUND THEN RAISE EXCEPTION 'generation_fenced'; END IF;
  END IF;
  SELECT count(*) INTO counted
  FROM public.backend_retention_bindings AS binding
  WHERE binding.namespace_id = dek_row.namespace_id
    AND binding.data_mode = dek_row.data_mode
    AND binding.subject_id = dek_row.subject_id
    AND binding.retention_domain = dek_row.retention_domain
    AND binding.dek_generation = dek_row.generation;
  INSERT INTO public.backend_demo_reset_key_events_v2 (
    event_id, reset_id, retention_job_id, namespace_id, data_mode,
    namespace_generation, run_id, arm_id, subject_id, retention_domain,
    dek_generation, lease_generation, fencing_token, phase, event_json
  ) VALUES (
    new_event_id, reset_row.reset_id, job_row.retention_job_id,
    job_row.namespace_id, job_row.data_mode, job_row.namespace_generation,
    job_row.run_id, job_row.arm_id, job_row.subject_id,
    job_row.retention_domain, job_row.dek_generation,
    expected_lease_generation, expected_fencing_token,
    'provider_call_reserved', jsonb_build_object(
      'schema_version', 'demo_reset_key_event.v1',
      'phase', 'provider_call_reserved',
      'retention_domain', job_row.retention_domain,
      'dek_generation', job_row.dek_generation,
      'attempt', job_row.attempt_count
    )
  ) ON CONFLICT (retention_job_id, lease_generation, phase) DO NOTHING;
  RETURN QUERY SELECT reset_row.reset_id,
    reset_row.source_namespace_generation, dek_row.retention_domain,
    dek_row.generation, dek_row.kek_key_id, dek_row.wrapping_algorithm,
    dek_row.wrapped_dek, dek_row.dek_sha256, counted;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_prepare_demo_reset_key(
  TEXT, BIGINT, TEXT, TEXT
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_commit_demo_reset_key(
  target_retention_job_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  new_key_receipt_id TEXT,
  new_event_id TEXT,
  destruction_evidence_sha256 TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  job_row public.backend_retention_jobs%ROWTYPE;
  reset_row public.backend_demo_resets_v2%ROWTYPE;
  dek_row public.backend_retention_deks%ROWTYPE;
  counted BIGINT;
  receipt JSONB;
  finalized BOOLEAN;
BEGIN
  IF destruction_evidence_sha256 !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid destruction evidence';
  END IF;
  SELECT * INTO job_row
  FROM public.backend_retention_jobs AS job
  WHERE job.retention_job_id = target_retention_job_id
  FOR UPDATE;
  IF NOT FOUND OR job_row.job_kind <> 'subject_forget'
     OR NOT public.sleepagent_retention_fence_allows(
       job_row.retention_job_id, job_row.namespace_id, job_row.data_mode,
       job_row.namespace_generation, job_row.run_id, job_row.arm_id,
       job_row.subject_id, job_row.retention_domain, job_row.dek_generation,
       expected_lease_generation, expected_fencing_token
     ) THEN
    RETURN FALSE;
  END IF;
  SELECT * INTO STRICT reset_row
  FROM public.backend_demo_resets_v2 AS reset
  WHERE reset.reset_id = job_row.job_json ->> 'reset_id'
    AND reset.status = 'pending'
  FOR SHARE;
  SELECT * INTO STRICT dek_row
  FROM public.backend_retention_deks AS dek
  WHERE dek.namespace_id = job_row.namespace_id
    AND dek.data_mode = job_row.data_mode
    AND dek.namespace_generation = reset_row.source_namespace_generation
    AND dek.subject_id = job_row.subject_id
    AND dek.retention_domain = job_row.retention_domain
    AND dek.generation = job_row.dek_generation
    AND dek.status = 'retired' AND dek.wrapped_dek IS NOT NULL
  FOR UPDATE;
  SELECT count(*) INTO counted
  FROM public.backend_retention_bindings AS binding
  WHERE binding.namespace_id = dek_row.namespace_id
    AND binding.data_mode = dek_row.data_mode
    AND binding.subject_id = dek_row.subject_id
    AND binding.retention_domain = dek_row.retention_domain
    AND binding.dek_generation = dek_row.generation;
  receipt := jsonb_build_object(
    'schema_version', 'demo_reset_key_receipt.v1',
    'outcome', 'destroyed',
    'retention_domain', dek_row.retention_domain,
    'dek_generation', dek_row.generation,
    'object_count', counted,
    'reason_code', 'replay_subject_forget',
    'destruction_evidence_sha256', destruction_evidence_sha256
  );
  INSERT INTO public.backend_demo_reset_key_receipts_v2 (
    key_receipt_id, reset_id, retention_job_id, namespace_id, data_mode,
    namespace_generation, run_id, arm_id, subject_id,
    source_namespace_generation, retention_domain, dek_generation,
    lease_generation, fencing_token, object_count, receipt_json
  ) VALUES (
    new_key_receipt_id, reset_row.reset_id, job_row.retention_job_id,
    job_row.namespace_id, job_row.data_mode, job_row.namespace_generation,
    job_row.run_id, job_row.arm_id, job_row.subject_id,
    reset_row.source_namespace_generation, job_row.retention_domain,
    job_row.dek_generation, expected_lease_generation,
    expected_fencing_token, counted, receipt
  );
  UPDATE public.backend_retention_deks
  SET wrapped_dek = NULL, status = 'shredded',
      shredded_at = clock_timestamp()
  WHERE namespace_id = dek_row.namespace_id
    AND data_mode = dek_row.data_mode
    AND subject_id = dek_row.subject_id
    AND retention_domain = dek_row.retention_domain
    AND generation = dek_row.generation
    AND status = 'retired' AND wrapped_dek = dek_row.wrapped_dek;
  IF NOT FOUND THEN RAISE EXCEPTION 'generation_fenced'; END IF;
  INSERT INTO public.backend_demo_reset_key_events_v2 (
    event_id, reset_id, retention_job_id, namespace_id, data_mode,
    namespace_generation, run_id, arm_id, subject_id, retention_domain,
    dek_generation, lease_generation, fencing_token, phase, event_json
  ) VALUES (
    new_event_id, reset_row.reset_id, job_row.retention_job_id,
    job_row.namespace_id, job_row.data_mode, job_row.namespace_generation,
    job_row.run_id, job_row.arm_id, job_row.subject_id,
    job_row.retention_domain, job_row.dek_generation,
    expected_lease_generation, expected_fencing_token,
    'provider_call_committed', jsonb_build_object(
      'schema_version', 'demo_reset_key_event.v1',
      'phase', 'provider_call_committed',
      'retention_domain', job_row.retention_domain,
      'dek_generation', job_row.dek_generation,
      'key_receipt_id', new_key_receipt_id
    )
  );
  SELECT public.sleepagent_finalize_retention_job(
    job_row.retention_job_id, expected_lease_generation,
    expected_fencing_token, 'succeeded', NULL
  ) INTO finalized;
  RETURN finalized;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_commit_demo_reset_key(
  TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;

-- Waiting for independently claimed subject-forget jobs is a checkpoint, not
-- a failed root attempt.  Release the exact operation fence and refund the
-- claim's attempt so a slow provider or a restarted retention Worker cannot
-- exhaust the durable reset root while its children are still progressing.
CREATE OR REPLACE FUNCTION sleepagent_wait_demo_reset(
  target_operation_id TEXT,
  expected_cas_version BIGINT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_next_available_at TIMESTAMPTZ
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_next_available_at IS NULL THEN
    RAISE EXCEPTION 'demo reset wait requires next availability';
  END IF;
  UPDATE public.sleep_domain_operations AS operation
  SET status = 'retry',
      outcome_class = 'subject_forget_in_progress',
      available_at = requested_next_available_at,
      attempt_count = GREATEST(operation.attempt_count - 1, 0),
      cas_version = operation.cas_version + 1,
      lease_expires_at = NULL,
      lease_owner = NULL,
      worker_instance = NULL,
      heartbeat_at = NULL,
      updated_at = clock_timestamp()
  WHERE operation.operation_id = target_operation_id
    AND operation.operation_type = 'demo_reset'
    AND operation.queue_name = 'demo_reset'
    AND operation.status = 'running'
    AND operation.cas_version = expected_cas_version
    AND public.sleepagent_operation_fence_allows(
      operation.operation_id,
      expected_lease_generation,
      expected_fencing_token
    );
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_wait_demo_reset(
  TEXT, BIGINT, BIGINT, TEXT, TIMESTAMPTZ
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_complete_demo_reset(
  target_operation_id TEXT,
  expected_cas_version BIGINT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  new_reset_receipt_id TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  operation_row public.sleep_domain_operations%ROWTYPE;
  reset_row public.backend_demo_resets_v2%ROWTYPE;
  epoch_row public.backend_subject_epochs%ROWTYPE;
  outcomes JSONB;
  receipt JSONB;
  finalized BOOLEAN;
BEGIN
  IF NOT public.sleepagent_operation_fence_allows(
    target_operation_id, expected_lease_generation, expected_fencing_token
  ) THEN
    RAISE EXCEPTION 'generation_fenced';
  END IF;
  SELECT * INTO STRICT operation_row
  FROM public.sleep_domain_operations AS operation
  WHERE operation.operation_id = target_operation_id
    AND operation.operation_type = 'demo_reset'
    AND operation.queue_name = 'demo_reset'
    AND operation.cas_version = expected_cas_version
  FOR UPDATE;
  SELECT * INTO STRICT reset_row
  FROM public.backend_demo_resets_v2 AS reset
  WHERE reset.operation_id = target_operation_id
    AND reset.status = 'pending'
  FOR UPDATE;
  SELECT * INTO STRICT epoch_row
  FROM public.backend_subject_epochs AS epoch
  WHERE epoch.namespace_id = reset_row.namespace_id
    AND epoch.data_mode = reset_row.data_mode
    AND epoch.subject_id = reset_row.subject_id
  FOR SHARE;
  IF EXISTS (
    SELECT 1
    FROM public.backend_retention_jobs AS job
    WHERE job.job_json ->> 'reset_id' = reset_row.reset_id
      AND job.status <> 'succeeded'
  ) OR (
    SELECT count(*)
    FROM public.backend_demo_reset_key_receipts_v2 AS key_receipt
    WHERE key_receipt.reset_id = reset_row.reset_id
  ) <> reset_row.required_dek_count THEN
    RAISE EXCEPTION 'reset_retention_incomplete';
  END IF;
  SELECT COALESCE(jsonb_agg(domain_outcome.outcome_json ORDER BY
    domain_outcome.retention_domain, domain_outcome.dek_generation),
    '[]'::jsonb)
  INTO outcomes
  FROM (
    SELECT key_receipt.retention_domain, key_receipt.dek_generation,
      key_receipt.receipt_json AS outcome_json
    FROM public.backend_demo_reset_key_receipts_v2 AS key_receipt
    WHERE key_receipt.reset_id = reset_row.reset_id
    UNION ALL
    SELECT scheduled.retention_domain, scheduled.dek_generation,
      jsonb_build_object(
        'outcome', 'expired',
        'retention_domain', scheduled.retention_domain,
        'dek_generation', scheduled.dek_generation,
        'object_count', COALESCE(
          (scheduled.expired_json -> 0 ->> 'binding_count')::bigint, 0
        ),
        'reason_code', 'scheduled_expiry_already_completed'
      )
    FROM public.backend_shred_receipts AS scheduled
    WHERE scheduled.namespace_id = reset_row.namespace_id
      AND scheduled.data_mode = reset_row.data_mode
      AND scheduled.namespace_generation =
        reset_row.source_namespace_generation
      AND scheduled.subject_id = reset_row.subject_id
  ) AS domain_outcome;
  receipt := jsonb_build_object(
    'schema_version', 'demo_reset_receipt.v1',
    'source_generation', reset_row.source_namespace_generation,
    'target_generation', reset_row.target_namespace_generation,
    'domain_outcomes', outcomes || jsonb_build_array(
      jsonb_build_object(
        'outcome', 'expired', 'retention_domain', 'projection_visibility',
        'object_count', 0, 'reason_code', 'generation_fenced'
      ),
      jsonb_build_object(
        'outcome', 'retained-with-reason',
        'retention_domain', 'pseudonymized_audit',
        'object_count', 1, 'reason_code', 'minimal_audit_required'
      )
    ),
    'required_domain_key_count', reset_row.required_dek_count,
    'authority_epochs', jsonb_build_object(
      'authorization_epoch', epoch_row.authorization_epoch,
      'privacy_epoch', epoch_row.privacy_epoch,
      'retrieval_policy_epoch', epoch_row.retrieval_policy_epoch
    ),
    'reason_code', 'replay_subject_forget_completed'
  );
  INSERT INTO public.backend_demo_reset_receipts_v2 (
    reset_receipt_id, reset_id, operation_id, namespace_id, data_mode,
    namespace_generation, run_id, arm_id, subject_id,
    lease_generation, fencing_token, receipt_json
  ) VALUES (
    new_reset_receipt_id, reset_row.reset_id, target_operation_id,
    reset_row.namespace_id, reset_row.data_mode,
    reset_row.target_namespace_generation, reset_row.target_run_id,
    reset_row.target_arm_id, reset_row.subject_id,
    expected_lease_generation, expected_fencing_token, receipt
  );
  UPDATE public.backend_demo_resets_v2
  SET status = 'succeeded', completed_at = clock_timestamp()
  WHERE reset_id = reset_row.reset_id AND status = 'pending';
  UPDATE public.sleep_domain_operations
  SET operation_json = jsonb_set(
    operation_json, '{result}',
    jsonb_build_object(
      'schema_version', 'demo_reset_result.v1',
      'reset_receipt_id', new_reset_receipt_id,
      'target_generation', reset_row.target_namespace_generation,
      'receipt', receipt
    ), TRUE
  )
  WHERE operation_id = target_operation_id
    AND cas_version = expected_cas_version;
  SELECT public.sleepagent_finalize_operation(
    target_operation_id, expected_cas_version, expected_lease_generation,
    expected_fencing_token, 'succeeded', 'demo_reset_completed', NULL
  ) INTO finalized;
  IF NOT finalized THEN RAISE EXCEPTION 'generation_fenced'; END IF;
  RETURN receipt;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_complete_demo_reset(
  TEXT, BIGINT, BIGINT, TEXT, TEXT
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
  principal TEXT := NULLIF(
    current_setting('sleepagent.service_principal_id', TRUE), ''
  );
  journey_count INTEGER;
  command_count INTEGER;
BEGIN
  IF NOT public.sleepagent_demo_workload_allows()
     OR target_root_operation_id IS NULL
     OR target_root_operation_id = '' THEN
    RAISE EXCEPTION 'authorization_denied';
  END IF;
  SELECT count(*) INTO journey_count
  FROM public.backend_demo_journeys AS journey
  JOIN public.sleep_domain_operations AS operation
    ON operation.operation_id = journey.root_operation_id
  WHERE journey.root_operation_id = target_root_operation_id
    AND operation.service_principal_id = principal
    AND operation.origin_kind = 'system';
  SELECT count(*) INTO command_count
  FROM public.sleep_domain_operations AS operation
  WHERE operation.operation_id = target_root_operation_id
    AND operation.service_principal_id = principal
    AND operation.origin_kind = 'system'
    AND operation.operation_type IN ('demo_advance', 'demo_reset');
  IF journey_count + command_count = 0 THEN
    RAISE EXCEPTION 'operation_not_found';
  ELSIF journey_count + command_count <> 1 THEN
    RAISE EXCEPTION 'operation_authority_ambiguous';
  END IF;
  IF journey_count = 1 THEN
    RETURN QUERY
    SELECT journey.root_operation_id, journey.namespace_generation,
      journey.phase, journey.result_json, journey.error_code,
      journey.updated_at
    FROM public.backend_demo_journeys AS journey
    WHERE journey.root_operation_id = target_root_operation_id;
    RETURN;
  END IF;
  RETURN QUERY
  SELECT operation.operation_id, operation.namespace_generation,
    CASE operation.status
      WHEN 'pending' THEN 'accepted'
      WHEN 'retry' THEN 'accepted'
      WHEN 'running' THEN 'staging_input'
      WHEN 'succeeded' THEN 'succeeded'
      WHEN 'outcome_unknown' THEN 'reconciliation_required'
      WHEN 'reconciliation_required' THEN 'reconciliation_required'
      WHEN 'dead_letter' THEN 'failed'
      ELSE 'failed'
    END,
    CASE WHEN operation.status = 'succeeded'
      THEN operation.operation_json -> 'result' ELSE NULL END,
    CASE WHEN operation.status IN ('pending', 'retry', 'running', 'succeeded')
      THEN NULL ELSE COALESCE(
        operation.outcome_class, operation.operation_type || '_failed'
      ) END,
    operation.updated_at
  FROM public.sleep_domain_operations AS operation
  WHERE operation.operation_id = target_root_operation_id
    AND operation.service_principal_id = principal
    AND operation.operation_type IN ('demo_advance', 'demo_reset');
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_get_demo_operation(TEXT) FROM PUBLIC;
