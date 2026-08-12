-- sleepagent:transactional=true
-- Stage 3: durable ScenarioClock commands. Product read models intentionally
-- query existing committed domain rows and require no parallel projection store.

ALTER TABLE backend_demo_seed_allowlist
  DROP CONSTRAINT backend_demo_seed_allowlist_v2_contract;
ALTER TABLE backend_demo_seed_allowlist
  ADD CONSTRAINT backend_demo_seed_allowlist_v3_contract CHECK (
    artifact_family IS NULL OR (
      artifact_family <> ''
      AND scenario_id IS NOT NULL AND scenario_id <> ''
      AND adapter_version IN (
        'replay_external_fact_adapter.v1',
        'replay_external_fact_adapter.v2'
      )
      AND manifest_schema_version = CASE adapter_version
        WHEN 'replay_external_fact_adapter.v1'
          THEN 'replay_ingress_manifest.v1'
        ELSE 'replay_ingress_manifest.v2'
      END
      AND expected_observation_count >= 1
      AND component_pins_sha256 ~ '^[0-9a-f]{64}$'
      AND canonical_sequence_sha256 ~ '^[0-9a-f]{64}$'
      AND manifest_sha256 ~ '^[0-9a-f]{64}$'
      AND first_received_at IS NOT NULL
      AND last_received_at >= first_received_at
      AND night_count BETWEEN 1 AND 15
    )
  );

ALTER TABLE backend_replay_ingress_manifests
  DROP CONSTRAINT backend_replay_ingress_manifests_schema_version_check;
ALTER TABLE backend_replay_ingress_manifests
  DROP CONSTRAINT backend_replay_ingress_manifests_night_count_check;
ALTER TABLE backend_replay_ingress_manifests
  ADD CONSTRAINT backend_replay_ingress_manifest_stage3_contract CHECK (
    (schema_version = 'replay_ingress_manifest.v1' AND night_count = 1)
    OR (
      schema_version = 'replay_ingress_manifest.v2'
      AND night_count BETWEEN 1 AND 15
      AND manifest_json ->> 'adapter_version' =
        'replay_external_fact_adapter.v2'
      AND (manifest_json ->> 'initial_observation_count')::integer
        BETWEEN 1 AND observation_count
      AND manifest_json ? 'initial_release_through'
    )
  );

-- These inherited CareFollowup tables predate the baseline RLS sweep.  Stage 3
-- makes them part of the Product read boundary, so scope them before granting
-- API/Worker access.
ALTER TABLE backend_episode_date_reconciliation
  ADD COLUMN protocol_version INTEGER NOT NULL DEFAULT 1,
  ADD COLUMN operation_id TEXT,
  ADD CONSTRAINT backend_episode_date_reconciliation_v2_contract CHECK (
    (protocol_version = 1 AND operation_id IS NULL)
    OR (protocol_version = 2 AND operation_id IS NOT NULL)
  ),
  ADD CONSTRAINT backend_episode_date_reconciliation_operation_fk
    FOREIGN KEY (operation_id)
    REFERENCES sleep_domain_operations(operation_id) ON DELETE RESTRICT,
  ADD CONSTRAINT backend_episode_date_reconciliation_operation_unique
    UNIQUE (operation_id);

CREATE INDEX idx_backend_episode_date_reconciliation_pending_v2
  ON backend_episode_date_reconciliation (
    namespace_id, data_mode, namespace_generation, status, created_at
  )
  WHERE protocol_version >= 2 AND status = 'reconciliation_required';

ALTER TABLE sleep_domain_care_followups
  ADD COLUMN namespace_generation BIGINT,
  ADD COLUMN run_id TEXT,
  ADD COLUMN arm_id TEXT,
  ADD CONSTRAINT sleep_domain_care_followup_generation_scope CHECK (
    (namespace_generation IS NULL AND run_id IS NULL AND arm_id IS NULL)
    OR (
      namespace_generation >= 1
      AND (
        (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
        OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
      )
    )
  );
ALTER TABLE sleep_domain_care_followups ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_care_followups FORCE ROW LEVEL SECURITY;
CREATE POLICY sleep_domain_care_followup_scope
  ON sleep_domain_care_followups
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE sleep_domain_care_followup_transition_receipts
  ADD COLUMN namespace_generation BIGINT,
  ADD COLUMN run_id TEXT,
  ADD COLUMN arm_id TEXT,
  ADD CONSTRAINT sleep_domain_care_followup_receipt_generation_scope CHECK (
    (namespace_generation IS NULL AND run_id IS NULL AND arm_id IS NULL)
    OR (
      namespace_generation >= 1
      AND (
        (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
        OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
      )
    )
  );
ALTER TABLE sleep_domain_care_followup_transition_receipts
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_care_followup_transition_receipts
  FORCE ROW LEVEL SECURITY;
CREATE POLICY sleep_domain_care_followup_receipt_scope
  ON sleep_domain_care_followup_transition_receipts
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

CREATE TABLE backend_replay_staged_facts (
  staged_fact_id TEXT PRIMARY KEY,
  journey_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT NOT NULL,
  arm_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  stream_key TEXT NOT NULL,
  stream_sequence BIGINT NOT NULL CHECK (stream_sequence >= 1),
  predecessor_sequence BIGINT,
  release_at TIMESTAMPTZ NOT NULL,
  fact_sha256 TEXT NOT NULL CHECK (fact_sha256 ~ '^[0-9a-f]{64}$'),
  fact_json JSONB NOT NULL CHECK (jsonb_typeof(fact_json) = 'object'),
  status TEXT NOT NULL DEFAULT 'staged' CHECK (status IN ('staged', 'released')),
  released_by_operation_id TEXT,
  release_lease_generation BIGINT,
  release_fencing_token TEXT,
  raw_ingress_record_id TEXT,
  released_at TIMESTAMPTZ,
  staged_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (journey_id, stream_sequence),
  UNIQUE (journey_id, fact_sha256),
  FOREIGN KEY (
    journey_id, namespace_id, data_mode, namespace_generation,
    run_id, arm_id, subject_id
  ) REFERENCES backend_demo_journeys (
    journey_id, namespace_id, data_mode, namespace_generation,
    run_id, arm_id, subject_id
  ) ON DELETE RESTRICT,
  FOREIGN KEY (released_by_operation_id)
    REFERENCES sleep_domain_operations(operation_id) ON DELETE RESTRICT,
  FOREIGN KEY (raw_ingress_record_id)
    REFERENCES sleep_domain_raw_inbox(raw_ingress_record_id) ON DELETE RESTRICT,
  CHECK (
    (stream_sequence = 1 AND predecessor_sequence IS NULL)
    OR predecessor_sequence = stream_sequence - 1
  ),
  CHECK (NOT (fact_json ?| ARRAY[
    'oracle', 'oracle_expected', 'expected_outcome', 'gold_answer'
  ])),
  CHECK (
    (status = 'staged'
      AND released_by_operation_id IS NULL
      AND release_lease_generation IS NULL
      AND release_fencing_token IS NULL
      AND raw_ingress_record_id IS NULL
      AND released_at IS NULL)
    OR
    (status = 'released'
      AND released_by_operation_id IS NOT NULL
      AND release_lease_generation >= 1
      AND release_fencing_token IS NOT NULL
      AND raw_ingress_record_id IS NOT NULL
      AND released_at IS NOT NULL)
  )
);

CREATE INDEX idx_backend_replay_staged_facts_due
  ON backend_replay_staged_facts (
    namespace_id, data_mode, namespace_generation, run_id, arm_id,
    status, release_at, stream_sequence
  );

CREATE OR REPLACE FUNCTION sleepagent_enforce_staged_fact_release()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'replay staged facts are append-only';
  END IF;
  IF OLD.status <> 'staged' OR NEW.status <> 'released'
     OR NEW.staged_fact_id <> OLD.staged_fact_id
     OR NEW.journey_id <> OLD.journey_id
     OR NEW.namespace_id <> OLD.namespace_id
     OR NEW.data_mode <> OLD.data_mode
     OR NEW.namespace_generation <> OLD.namespace_generation
     OR NEW.run_id <> OLD.run_id
     OR NEW.arm_id <> OLD.arm_id
     OR NEW.subject_id <> OLD.subject_id
     OR NEW.manifest_sha256 <> OLD.manifest_sha256
     OR NEW.stream_key <> OLD.stream_key
     OR NEW.stream_sequence <> OLD.stream_sequence
     OR NEW.predecessor_sequence IS DISTINCT FROM OLD.predecessor_sequence
     OR NEW.release_at <> OLD.release_at
     OR NEW.fact_sha256 <> OLD.fact_sha256
     OR NEW.fact_json <> OLD.fact_json
     OR NEW.staged_at <> OLD.staged_at
     OR NOT public.sleepagent_operation_fence_allows(
       NEW.released_by_operation_id,
       NEW.release_lease_generation,
       NEW.release_fencing_token
     ) THEN
    RAISE EXCEPTION 'replay staged fact release requires current operation fence';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER backend_replay_staged_fact_release_fence
BEFORE UPDATE OR DELETE ON backend_replay_staged_facts
FOR EACH ROW EXECUTE FUNCTION sleepagent_enforce_staged_fact_release();

ALTER TABLE backend_replay_staged_facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_replay_staged_facts FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_replay_staged_fact_scope
  ON backend_replay_staged_facts
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

CREATE TABLE backend_demo_advance_receipts (
  advance_receipt_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT NOT NULL,
  arm_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  from_scenario_time TIMESTAMPTZ NOT NULL,
  to_scenario_time TIMESTAMPTZ NOT NULL,
  from_clock_version BIGINT NOT NULL CHECK (from_clock_version >= 0),
  to_clock_version BIGINT NOT NULL CHECK (to_clock_version >= 1),
  released_fact_count BIGINT NOT NULL CHECK (released_fact_count >= 0),
  receipt_json JSONB NOT NULL CHECK (jsonb_typeof(receipt_json) = 'object'),
  committed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (operation_id),
  FOREIGN KEY (operation_id, namespace_id, data_mode, subject_id)
    REFERENCES sleep_domain_operations (
      operation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  CHECK (to_scenario_time > from_scenario_time),
  CHECK (to_clock_version = from_clock_version + 1)
);

CREATE TRIGGER backend_demo_advance_receipt_immutable
BEFORE UPDATE OR DELETE ON backend_demo_advance_receipts
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_demo_append_only_mutation();

ALTER TABLE backend_demo_advance_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_demo_advance_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_demo_advance_receipt_scope
  ON backend_demo_advance_receipts
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

CREATE OR REPLACE FUNCTION sleepagent_reserve_demo_advance(
  caller_idempotency_key TEXT,
  request_sha256 TEXT,
  requested_seconds INTEGER,
  new_operation_id TEXT,
  new_command_receipt_id TEXT,
  new_event_id TEXT,
  new_audit_id TEXT
)
RETURNS TABLE (
  operation_id TEXT,
  namespace_generation BIGINT,
  reused BOOLEAN
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  principal TEXT := NULLIF(
    current_setting('sleepagent.service_principal_id', TRUE), ''
  );
  journey_row public.backend_demo_journeys%ROWTYPE;
  epoch_row public.backend_subject_epochs%ROWTYPE;
  existing_receipt public.backend_command_receipts%ROWTYPE;
  worker_principal TEXT;
  worker_count INTEGER;
  visible_count INTEGER;
  policy_hash TEXT;
  semantic_key TEXT;
  workload JSONB;
  operation_payload JSONB;
BEGIN
  IF NOT public.sleepagent_demo_workload_allows()
     OR sleepagent_reserve_demo_advance.caller_idempotency_key IS NULL
     OR sleepagent_reserve_demo_advance.caller_idempotency_key = ''
     OR sleepagent_reserve_demo_advance.request_sha256 !~ '^[0-9a-f]{64}$'
     OR requested_seconds < 1 OR requested_seconds > 604800
     OR new_operation_id IS NULL OR new_operation_id = ''
     OR new_command_receipt_id IS NULL OR new_command_receipt_id = ''
     OR new_event_id IS NULL OR new_event_id = ''
     OR new_audit_id IS NULL OR new_audit_id = '' THEN
    RAISE EXCEPTION 'invalid_request';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
    principal || E'\x1f/demo/v1/advance\x1f' ||
      sleepagent_reserve_demo_advance.caller_idempotency_key,
    0
  ));
  SELECT * INTO existing_receipt
  FROM public.backend_command_receipts AS receipt
  WHERE receipt.service_principal_id = principal
    AND receipt.actor_id IS NULL
    AND receipt.route_template = '/demo/v1/advance'
    AND receipt.caller_idempotency_key =
      sleepagent_reserve_demo_advance.caller_idempotency_key
  FOR UPDATE;
  IF FOUND THEN
    IF existing_receipt.request_sha256 <>
       sleepagent_reserve_demo_advance.request_sha256 THEN
      RAISE EXCEPTION 'idempotency_conflict';
    END IF;
    RETURN QUERY SELECT existing_receipt.operation_id,
      existing_receipt.namespace_generation, TRUE;
    RETURN;
  END IF;

  SELECT count(*) INTO visible_count
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
    AND journey.phase = 'succeeded';
  IF visible_count = 0 THEN
    RAISE EXCEPTION 'operation_not_found';
  ELSIF visible_count <> 1 THEN
    RAISE EXCEPTION 'operation_authority_ambiguous';
  END IF;
  SELECT journey.* INTO STRICT journey_row
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
  FOR SHARE OF journey;
  SELECT * INTO STRICT epoch_row
  FROM public.backend_subject_epochs AS epoch
  WHERE epoch.namespace_id = journey_row.namespace_id
    AND epoch.data_mode = 'replay'
    AND epoch.subject_id = journey_row.subject_id
  FOR SHARE;
  SELECT count(*), min(worker.principal_id)
  INTO worker_count, worker_principal
  FROM public.backend_service_principals AS worker
  WHERE worker.principal_kind = 'worker' AND worker.status = 'active';
  IF worker_count <> 1 OR worker_principal IS NULL THEN
    RAISE EXCEPTION 'authorization_denied';
  END IF;
  SELECT seed.metadata_json ->> 'policy_sha256' INTO policy_hash
  FROM public.backend_demo_seed_allowlist AS seed
  WHERE seed.seed_id = journey_row.seed_id AND seed.active;
  IF policy_hash !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'scenario_contract_invalid';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM public.backend_principal_grants AS grant_row
    WHERE grant_row.principal_id = worker_principal
      AND grant_row.namespace_id = journey_row.namespace_id
      AND grant_row.data_mode = 'replay'
      AND grant_row.purpose = 'worker'
      AND grant_row.authorization_epoch = epoch_row.authorization_epoch
      AND grant_row.status = 'active'
      AND grant_row.allowed_handlers_json ? 'demo_advance'
      AND grant_row.valid_from <= clock_timestamp()
      AND (grant_row.valid_until IS NULL
        OR grant_row.valid_until > clock_timestamp())
  ) THEN
    RAISE EXCEPTION 'authorization_denied';
  END IF;

  workload := jsonb_build_object(
    'schema_version', 'workload_authorization_snapshot.v1',
    'workload_principal_id', worker_principal,
    'namespace_id', journey_row.namespace_id,
    'namespace_generation', journey_row.namespace_generation,
    'data_mode', 'replay',
    'run_id', journey_row.run_id,
    'arm_id', journey_row.arm_id,
    'subject_id', journey_row.subject_id,
    'purpose', 'worker',
    'allowed_handler', 'demo_advance',
    'authorization_epoch', epoch_row.authorization_epoch,
    'privacy_epoch', epoch_row.privacy_epoch,
    'retrieval_policy_epoch', epoch_row.retrieval_policy_epoch
  );
  semantic_key := encode(digest(
    journey_row.namespace_id || E'\x1f' ||
    journey_row.namespace_generation::text || E'\x1f' ||
    principal || E'\x1f' ||
      sleepagent_reserve_demo_advance.caller_idempotency_key,
    'sha256'
  ), 'hex');
  operation_payload := jsonb_build_object(
    'schema_version', 'backend_operation.v2',
    'command_type', 'demo.advance.v1',
    'payload', jsonb_build_object('seconds', requested_seconds),
    'correlation_id', new_operation_id,
    'generation', journey_row.namespace_generation,
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
    new_operation_id, journey_row.namespace_id, 'replay', 'demo_advance',
    journey_row.subject_id, principal, NULL, journey_row.namespace_id,
    'scenario-clock:' || journey_row.namespace_generation::text,
    sleepagent_reserve_demo_advance.caller_idempotency_key,
    sleepagent_reserve_demo_advance.request_sha256, 'pending', 0, 0,
    operation_payload, clock_timestamp(), clock_timestamp(), 2,
    journey_row.namespace_generation, journey_row.run_id, journey_row.arm_id,
    'uuidv7', 'system', semantic_key, 'demo_advance', 0,
    clock_timestamp(), 5, workload, policy_hash
  );
  INSERT INTO public.backend_command_receipts (
    command_receipt_id, namespace_id, data_mode, namespace_generation,
    run_id, arm_id, service_principal_id, actor_id, subject_id,
    route_template, caller_idempotency_key, request_sha256, operation_id,
    authorization_snapshot_json, receipt_json, origin_kind,
    workload_authorization_snapshot_json
  ) VALUES (
    new_command_receipt_id, journey_row.namespace_id, 'replay',
    journey_row.namespace_generation, journey_row.run_id, journey_row.arm_id,
    principal, NULL, journey_row.subject_id, '/demo/v1/advance',
    sleepagent_reserve_demo_advance.caller_idempotency_key,
    sleepagent_reserve_demo_advance.request_sha256, new_operation_id, NULL,
    jsonb_build_object(
      'schema_version', 'command_receipt.v2',
      'command_receipt_id', new_command_receipt_id,
      'operation_id', new_operation_id,
      'route_template', '/demo/v1/advance',
      'request_sha256', sleepagent_reserve_demo_advance.request_sha256
    ), 'system', workload
  );
  INSERT INTO public.sleep_domain_domain_outbox (
    event_id, namespace_id, data_mode, event_type, aggregate_type,
    aggregate_id, aggregate_version, per_aggregate_sequence, subject_id,
    operation_id, status, available_at, event_json, created_at,
    protocol_version, namespace_generation, run_id, arm_id
  ) VALUES (
    new_event_id, journey_row.namespace_id, 'replay',
    'DEMO_ADVANCE_ACCEPTED', 'Operation', new_operation_id, 1, 1,
    journey_row.subject_id, new_operation_id, 'committed', clock_timestamp(),
    jsonb_build_object(
      'schema_version', 'committed_event.v2',
      'event_type', 'DEMO_ADVANCE_ACCEPTED',
      'operation_id', new_operation_id,
      'generation', journey_row.namespace_generation,
      'data_mode', 'replay', 'synthetic_non_release', TRUE
    ), clock_timestamp(), 2, journey_row.namespace_generation,
    journey_row.run_id, journey_row.arm_id
  );
  INSERT INTO public.backend_authorization_audit (
    audit_id, namespace_id, data_mode, subject_id, principal_id, actor_id,
    binding_id, decision, reason_code, policy_sha256, authorization_epoch,
    privacy_epoch, retrieval_policy_epoch, audit_json, occurred_at
  ) VALUES (
    new_audit_id, journey_row.namespace_id, 'replay', journey_row.subject_id,
    principal, NULL, NULL, 'allow', 'demo_advance_reserved', policy_hash,
    epoch_row.authorization_epoch, epoch_row.privacy_epoch,
    epoch_row.retrieval_policy_epoch,
    jsonb_build_object(
      'operation_id', new_operation_id,
      'route', '/demo/v1/advance',
      'generation', journey_row.namespace_generation
    ), clock_timestamp()
  );
  RETURN QUERY SELECT new_operation_id, journey_row.namespace_generation, FALSE;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_reserve_demo_advance(
  TEXT, TEXT, INTEGER, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_stage3_advance_authority_allows(
  requested_operation_id TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT EXISTS (
    SELECT 1
    FROM public.sleep_domain_operations AS operation
    JOIN public.backend_namespaces AS namespace_row
      ON namespace_row.namespace_id = operation.namespace_id
     AND namespace_row.data_mode = operation.data_mode
     AND namespace_row.current_generation = operation.namespace_generation
     AND namespace_row.status = 'active'
    JOIN public.backend_subject_epochs AS epoch_row
      ON epoch_row.namespace_id = operation.namespace_id
     AND epoch_row.data_mode = operation.data_mode
     AND epoch_row.subject_id = operation.subject_id
    JOIN public.backend_principal_grants AS grant_row
      ON grant_row.principal_id = NULLIF(
        current_setting('sleepagent.service_principal_id', TRUE), ''
      )
     AND grant_row.namespace_id = operation.namespace_id
     AND grant_row.data_mode = operation.data_mode
     AND grant_row.purpose = NULLIF(
        current_setting('sleepagent.purpose', TRUE), ''
      )
     AND grant_row.authorization_epoch = epoch_row.authorization_epoch
     AND grant_row.status = 'active'
    WHERE operation.operation_id = requested_operation_id
      AND operation.operation_type = 'demo_advance'
      AND operation.queue_name = 'demo_advance'
      AND operation.origin_kind = 'system'
      AND operation.namespace_id = NULLIF(
        current_setting('sleepagent.namespace_id', TRUE), ''
      )
      AND operation.namespace_generation::text = NULLIF(
        current_setting('sleepagent.namespace_generation', TRUE), ''
      )
      AND operation.subject_id = NULLIF(
        current_setting('sleepagent.subject_id', TRUE), ''
      )
      AND operation.workload_authorization_snapshot_json ->>
        'authorization_epoch' = epoch_row.authorization_epoch::text
      AND operation.workload_authorization_snapshot_json ->>
        'privacy_epoch' = epoch_row.privacy_epoch::text
      AND operation.workload_authorization_snapshot_json ->>
        'retrieval_policy_epoch' = epoch_row.retrieval_policy_epoch::text
      AND grant_row.allowed_handlers_json ? 'demo_advance'
      AND grant_row.valid_from <= clock_timestamp()
      AND (grant_row.valid_until IS NULL
        OR grant_row.valid_until > clock_timestamp())
  )
$$;

REVOKE ALL ON FUNCTION sleepagent_stage3_advance_authority_allows(TEXT)
  FROM PUBLIC;

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
  advance_count INTEGER;
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
  SELECT count(*) INTO advance_count
  FROM public.sleep_domain_operations AS operation
  WHERE operation.operation_id = target_root_operation_id
    AND operation.service_principal_id = principal
    AND operation.origin_kind = 'system'
    AND operation.operation_type = 'demo_advance';
  IF journey_count + advance_count = 0 THEN
    RAISE EXCEPTION 'operation_not_found';
  ELSIF journey_count + advance_count <> 1 THEN
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
      THEN NULL ELSE COALESCE(operation.outcome_class, 'demo_advance_failed') END,
    operation.updated_at
  FROM public.sleep_domain_operations AS operation
  WHERE operation.operation_id = target_root_operation_id
    AND operation.service_principal_id = principal
    AND operation.operation_type = 'demo_advance';
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_get_demo_operation(TEXT) FROM PUBLIC;
