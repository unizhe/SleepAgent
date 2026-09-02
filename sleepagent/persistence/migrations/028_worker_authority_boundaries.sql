-- sleepagent:transactional=true

-- Final Source Audit P0-B: make reclaim exhaustion use the same durable
-- row authority as claim, preserve external-effect ambiguity, and close the
-- provider-account/quarantine bounded-worker table boundary.

CREATE OR REPLACE FUNCTION public.sleepagent_worker_claim_authority_v2(
  row_namespace_id TEXT,
  row_data_mode TEXT,
  row_namespace_generation BIGINT,
  row_run_id TEXT,
  row_arm_id TEXT,
  row_subject_id TEXT,
  row_authorization_snapshot JSONB,
  requested_handler TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT
    public.sleepagent_principal_context_allows()
    AND NULLIF(current_setting('sleepagent.process_role', TRUE), '') = 'worker'
    AND NULLIF(current_setting('sleepagent.data_mode', TRUE), '') = row_data_mode
    AND requested_handler IS NOT NULL
    AND requested_handler <> ''
    AND row_authorization_snapshot IS NOT NULL
    AND jsonb_typeof(row_authorization_snapshot) = 'object'
    AND row_authorization_snapshot ?& ARRAY[
      'authorization_epoch', 'privacy_epoch', 'retrieval_policy_epoch'
    ]
    AND EXISTS (
      SELECT 1
      FROM public.backend_namespaces AS namespace_row
      JOIN public.backend_namespace_generations AS generation_row
        ON generation_row.namespace_id = namespace_row.namespace_id
       AND generation_row.data_mode = namespace_row.data_mode
       AND generation_row.generation = row_namespace_generation
      JOIN public.backend_subject_epochs AS epoch_row
        ON epoch_row.namespace_id = namespace_row.namespace_id
       AND epoch_row.data_mode = namespace_row.data_mode
       AND epoch_row.subject_id = row_subject_id
      WHERE namespace_row.namespace_id = row_namespace_id
        AND namespace_row.data_mode = row_data_mode
        AND namespace_row.status = 'active'
        AND generation_row.status IN ('active', 'sealed')
        AND row_authorization_snapshot ->> 'authorization_epoch' =
          epoch_row.authorization_epoch::text
        AND row_authorization_snapshot ->> 'privacy_epoch' =
          epoch_row.privacy_epoch::text
        AND row_authorization_snapshot ->> 'retrieval_policy_epoch' =
          epoch_row.retrieval_policy_epoch::text
    )
    AND (
      (row_data_mode = 'live'
        AND row_run_id IS NULL
        AND row_arm_id IS NULL)
      OR
      (row_data_mode = 'replay'
        AND row_run_id IS NOT NULL
        AND row_arm_id IS NOT NULL
        AND EXISTS (
          SELECT 1
          FROM public.backend_replay_arms AS replay_arm
          WHERE replay_arm.namespace_id = row_namespace_id
            AND replay_arm.data_mode = row_data_mode
            AND replay_arm.namespace_generation = row_namespace_generation
            AND replay_arm.run_id = row_run_id
            AND replay_arm.arm_id = row_arm_id
        ))
    )
    AND EXISTS (
      SELECT 1
      FROM public.backend_principal_grants AS grant_row
      WHERE grant_row.principal_id = NULLIF(
          current_setting('sleepagent.service_principal_id', TRUE), ''
        )
        AND grant_row.namespace_id = row_namespace_id
        AND grant_row.data_mode = row_data_mode
        AND grant_row.purpose = NULLIF(
          current_setting('sleepagent.purpose', TRUE), ''
        )
        AND grant_row.authorization_epoch::text =
          row_authorization_snapshot ->> 'authorization_epoch'
        AND grant_row.status = 'active'
        AND grant_row.valid_from <= clock_timestamp()
        AND (
          grant_row.valid_until IS NULL
          OR grant_row.valid_until > clock_timestamp()
        )
        AND grant_row.allowed_handlers_json ? requested_handler
    )
$$;

REVOKE ALL ON FUNCTION public.sleepagent_worker_claim_authority_v2(
  TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT, JSONB, TEXT
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.sleepagent_ensure_delivery_reconciliation_v2(
  requested_delivery_intent_id TEXT,
  requested_error_code TEXT
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  source_row RECORD;
  workload JSONB;
  payload JSONB;
  semantic_identity TEXT;
  reconciliation_semantic_key TEXT;
  operation_id TEXT;
BEGIN
  SELECT intent.namespace_id, intent.data_mode,
         intent.namespace_generation, intent.run_id, intent.arm_id,
         intent.subject_id, intent.destination, intent.handler_name,
         intent.semantic_effect_key, intent.payload_sha256,
         intent.authorization_snapshot_json, event.operation_id AS source_operation_id,
         operation.policy_sha256
  INTO source_row
  FROM public.backend_delivery_intents AS intent
  JOIN public.sleep_domain_domain_outbox AS event
    ON event.event_id = intent.source_event_id
   AND event.namespace_id = intent.namespace_id
   AND event.data_mode = intent.data_mode
   AND event.subject_id = intent.subject_id
  JOIN public.sleep_domain_operations AS operation
    ON operation.operation_id = event.operation_id
   AND operation.namespace_id = intent.namespace_id
   AND operation.data_mode = intent.data_mode
   AND operation.subject_id = intent.subject_id
  WHERE intent.delivery_intent_id = requested_delivery_intent_id
    AND intent.status = 'outcome_unknown'
  FOR UPDATE OF intent;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'delivery reconciliation authority is unavailable';
  END IF;

  IF requested_error_code IS NULL
     OR requested_error_code = ''
     OR source_row.policy_sha256 !~ '^[0-9a-f]{64}$'
     OR source_row.payload_sha256 !~ '^[0-9a-f]{64}$'
     OR NOT public.sleepagent_worker_claim_authority_v2(
       source_row.namespace_id,
       source_row.data_mode,
       source_row.namespace_generation,
       source_row.run_id,
       source_row.arm_id,
       source_row.subject_id,
       source_row.authorization_snapshot_json,
       source_row.handler_name
     ) THEN
    RAISE EXCEPTION 'delivery reconciliation authority is unavailable';
  END IF;

  workload := jsonb_build_object(
    'schema_version', 'workload_authorization_snapshot.v1',
    'workload_principal_id', NULLIF(
      current_setting('sleepagent.service_principal_id', TRUE), ''
    ),
    'namespace_id', source_row.namespace_id,
    'namespace_generation', source_row.namespace_generation,
    'data_mode', source_row.data_mode,
    'run_id', source_row.run_id,
    'arm_id', source_row.arm_id,
    'subject_id', source_row.subject_id,
    'purpose', NULLIF(current_setting('sleepagent.purpose', TRUE), ''),
    'allowed_handler', 'reconciliation',
    'authorization_epoch',
      (source_row.authorization_snapshot_json ->> 'authorization_epoch')::BIGINT,
    'privacy_epoch',
      (source_row.authorization_snapshot_json ->> 'privacy_epoch')::BIGINT,
    'retrieval_policy_epoch',
      (source_row.authorization_snapshot_json ->> 'retrieval_policy_epoch')::BIGINT
  );
  payload := jsonb_build_object(
    'schema_version', 'delivery_reconciliation_operation.v1',
    'delivery_intent_id', requested_delivery_intent_id,
    'source_operation_id', source_row.source_operation_id,
    'destination', source_row.destination,
    'handler_name', source_row.handler_name,
    'semantic_effect_key', source_row.semantic_effect_key,
    'payload_sha256', source_row.payload_sha256,
    'invocation_key', 'replay-delivery:' || source_row.semantic_effect_key || ':v1',
    'trigger_error_code', requested_error_code,
    'authorization_snapshot', workload
  );
  semantic_identity := format(
    '{"delivery_intent_id":%s,"operation_type":"delivery_reconciliation"}',
    to_json(requested_delivery_intent_id)::text
  );
  reconciliation_semantic_key := encode(
    digest(convert_to(semantic_identity, 'UTF8'), 'sha256'),
    'hex'
  );
  operation_id := overlay(
    gen_random_uuid()::text placing '7' from 15 for 1
  );

  INSERT INTO public.sleep_domain_operations (
    operation_id, namespace_id, data_mode, operation_type, subject_id,
    service_principal_id, actor_id, target_resource_id,
    target_resource_key, idempotency_key, request_sha256, status,
    cas_version, operation_json, created_at, updated_at,
    protocol_version, namespace_generation, run_id, arm_id, id_scheme,
    origin_kind, semantic_key, queue_name, priority, available_at,
    max_attempts, workload_authorization_snapshot_json, policy_sha256
  ) VALUES (
    operation_id, source_row.namespace_id, source_row.data_mode,
    'delivery_reconciliation', source_row.subject_id,
    NULLIF(current_setting('sleepagent.service_principal_id', TRUE), ''),
    NULL, requested_delivery_intent_id,
    'delivery:' || requested_delivery_intent_id,
    'delivery-reconciliation:' || requested_delivery_intent_id,
    encode(digest(convert_to(payload::text, 'UTF8'), 'sha256'), 'hex'),
    'pending', 0, payload, clock_timestamp(), clock_timestamp(), 2,
    source_row.namespace_generation, source_row.run_id, source_row.arm_id, 'uuidv7',
    'system', reconciliation_semantic_key, 'reconciliation', 80,
    clock_timestamp(), 5,
    workload, source_row.policy_sha256
  ) ON CONFLICT (
    namespace_id, data_mode, namespace_generation,
    (COALESCE(run_id, '')), (COALESCE(arm_id, '')),
    operation_type, semantic_key
  ) WHERE protocol_version >= 2 DO NOTHING;

  SELECT operation.operation_id INTO operation_id
  FROM public.sleep_domain_operations AS operation
  WHERE operation.namespace_id = source_row.namespace_id
    AND operation.data_mode = source_row.data_mode
    AND operation.namespace_generation = source_row.namespace_generation
    AND COALESCE(operation.run_id, '') = COALESCE(source_row.run_id, '')
    AND COALESCE(operation.arm_id, '') = COALESCE(source_row.arm_id, '')
    AND operation.operation_type = 'delivery_reconciliation'
    AND operation.semantic_key = reconciliation_semantic_key;
  IF operation_id IS NULL THEN
    RAISE EXCEPTION 'delivery reconciliation operation was not persisted';
  END IF;
  RETURN operation_id;
END;
$$;

REVOKE ALL ON FUNCTION public.sleepagent_ensure_delivery_reconciliation_v2(
  TEXT, TEXT
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.sleepagent_exhaust_authorized_reclaims_v2(
  requested_kind TEXT,
  requested_selector TEXT DEFAULT NULL,
  requested_limit INTEGER DEFAULT 1
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  deployment_mode TEXT := NULLIF(
    current_setting('sleepagent.data_mode', TRUE), ''
  );
  affected INTEGER := 0;
  delivery_row RECORD;
BEGIN
  IF deployment_mode NOT IN ('live', 'replay')
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'worker'
     OR NOT public.sleepagent_principal_context_allows()
     OR requested_limit < 1
     OR requested_limit > 32 THEN
    RAISE EXCEPTION 'trusted bounded worker context is required for reclaim exhaustion';
  END IF;

  IF requested_kind = 'normalization' THEN
    WITH candidate AS (
      SELECT work.work_id
      FROM public.sleep_domain_normalization_work AS work
      WHERE work.protocol_version >= 2
        AND work.data_mode = deployment_mode
        AND work.status = 'running'
        AND work.available_at <= clock_timestamp()
        AND work.lease_expires_at <= clock_timestamp()
        AND work.execution_reclaim_count >= work.max_execution_reclaims
        AND (
          work.predecessor_work_id IS NULL
          OR EXISTS (
            SELECT 1
            FROM public.sleep_domain_normalization_work AS predecessor
            WHERE predecessor.work_id = work.predecessor_work_id
              AND predecessor.status = 'succeeded'
          )
        )
        AND public.sleepagent_worker_claim_authority_v2(
          work.namespace_id, work.data_mode, work.namespace_generation,
          work.run_id, work.arm_id, work.subject_id,
          work.authorization_snapshot_json, 'normalization'
        )
      ORDER BY work.updated_at, work.work_id
      FOR UPDATE OF work SKIP LOCKED
      LIMIT requested_limit
    )
    UPDATE public.sleep_domain_normalization_work AS work
    SET status = 'quarantined',
        last_error_code = 'execution_reclaim_exhausted',
        lease_owner = NULL, worker_instance = NULL, fencing_token = NULL,
        heartbeat_at = NULL, lease_expires_at = NULL,
        updated_at = clock_timestamp()
    FROM candidate
    WHERE work.work_id = candidate.work_id;
    GET DIAGNOSTICS affected = ROW_COUNT;

  ELSIF requested_kind = 'operation' THEN
    WITH candidate AS (
      SELECT operation.operation_id
      FROM public.sleep_domain_operations AS operation
      WHERE operation.protocol_version >= 2
        AND operation.data_mode = deployment_mode
        AND operation.queue_name = requested_selector
        AND operation.status = 'running'
        AND operation.available_at <= clock_timestamp()
        AND operation.lease_expires_at <= clock_timestamp()
        AND operation.execution_reclaim_count >=
          operation.max_execution_reclaims
        AND (
          operation.operation_type <> 'fast_path'
          OR operation.data_mode <> 'replay'
          OR NOT EXISTS (
            SELECT 1
            FROM public.sleep_domain_normalization_work AS pending_normalization
            WHERE pending_normalization.namespace_id = operation.namespace_id
              AND pending_normalization.data_mode = operation.data_mode
              AND pending_normalization.namespace_generation =
                operation.namespace_generation
              AND pending_normalization.subject_id = operation.subject_id
              AND COALESCE(pending_normalization.run_id, '') =
                COALESCE(operation.run_id, '')
              AND COALESCE(pending_normalization.arm_id, '') =
                COALESCE(operation.arm_id, '')
              AND pending_normalization.status <> 'succeeded'
          )
        )
        AND public.sleepagent_worker_claim_authority_v2(
          operation.namespace_id, operation.data_mode,
          operation.namespace_generation, operation.run_id, operation.arm_id,
          operation.subject_id, COALESCE(
            operation.authorization_snapshot_json,
            operation.workload_authorization_snapshot_json
          ), requested_selector
        )
      ORDER BY operation.updated_at, operation.operation_id
      FOR UPDATE OF operation SKIP LOCKED
      LIMIT requested_limit
    )
    UPDATE public.sleep_domain_operations AS operation
    SET status = 'dead_letter', outcome_class = 'terminal_failure',
        dead_lettered_at = clock_timestamp(), lease_owner = NULL,
        worker_instance = NULL, fencing_token = NULL, heartbeat_at = NULL,
        lease_expires_at = NULL, cas_version = operation.cas_version + 1,
        updated_at = clock_timestamp()
    FROM candidate
    WHERE operation.operation_id = candidate.operation_id;
    GET DIAGNOSTICS affected = ROW_COUNT;

  ELSIF requested_kind = 'delivery' THEN
    FOR delivery_row IN
      WITH candidate AS (
        SELECT intent.delivery_intent_id,
          CASE
            WHEN EXISTS (
              SELECT 1
              FROM public.sleep_domain_domain_outbox AS event
              JOIN public.backend_invocations AS invocation
                ON invocation.operation_id = event.operation_id
               AND invocation.namespace_id = intent.namespace_id
               AND invocation.data_mode = intent.data_mode
               AND invocation.subject_id = intent.subject_id
               AND invocation.invocation_kind = 'external_sink'
               AND invocation.invocation_key =
                 'replay-delivery:' || intent.semantic_effect_key || ':v1'
              WHERE event.event_id = intent.source_event_id
                AND event.namespace_id = intent.namespace_id
                AND event.data_mode = intent.data_mode
                AND event.subject_id = intent.subject_id
                AND invocation.current_state = 'response_received'
            ) THEN 'delivered'
            WHEN EXISTS (
              SELECT 1
              FROM public.sleep_domain_domain_outbox AS event
              JOIN public.backend_invocations AS invocation
                ON invocation.operation_id = event.operation_id
               AND invocation.namespace_id = intent.namespace_id
               AND invocation.data_mode = intent.data_mode
               AND invocation.subject_id = intent.subject_id
               AND invocation.invocation_kind = 'external_sink'
               AND invocation.invocation_key =
                 'replay-delivery:' || intent.semantic_effect_key || ':v1'
              WHERE event.event_id = intent.source_event_id
                AND event.namespace_id = intent.namespace_id
                AND event.data_mode = intent.data_mode
                AND event.subject_id = intent.subject_id
                AND invocation.current_state IN (
                  'send_started', 'outcome_possible', 'outcome_unknown'
                )
            ) THEN 'outcome_unknown'
            ELSE 'dead_letter'
          END AS terminal_status
        FROM public.backend_delivery_intents AS intent
        WHERE intent.data_mode = deployment_mode
          AND intent.destination = requested_selector
          AND intent.status IN ('running', 'dispatching')
          AND intent.available_at <= clock_timestamp()
          AND intent.lease_expires_at <= clock_timestamp()
          AND intent.execution_reclaim_count >= intent.max_execution_reclaims
          AND (
            intent.predecessor_sequence IS NULL
            OR EXISTS (
              SELECT 1
              FROM public.backend_delivery_intents AS predecessor
              WHERE predecessor.destination = intent.destination
                AND predecessor.aggregate_type = intent.aggregate_type
                AND predecessor.aggregate_id = intent.aggregate_id
                AND predecessor.aggregate_sequence = intent.predecessor_sequence
                AND predecessor.status = 'delivered'
            )
          )
          AND public.sleepagent_worker_claim_authority_v2(
            intent.namespace_id, intent.data_mode, intent.namespace_generation,
            intent.run_id, intent.arm_id, intent.subject_id,
            intent.authorization_snapshot_json, intent.handler_name
          )
        ORDER BY intent.updated_at, intent.delivery_intent_id
        FOR UPDATE OF intent SKIP LOCKED
        LIMIT requested_limit
      ), exhausted AS (
        UPDATE public.backend_delivery_intents AS intent
        SET status = candidate.terminal_status,
            delivered_at = CASE
              WHEN candidate.terminal_status = 'delivered'
                THEN COALESCE(intent.delivered_at, clock_timestamp())
              ELSE intent.delivered_at
            END,
            worker_instance = NULL, fencing_token = NULL,
            lease_expires_at = NULL, dispatch_permit_at = NULL,
            updated_at = clock_timestamp()
        FROM candidate
        WHERE intent.delivery_intent_id = candidate.delivery_intent_id
        RETURNING intent.delivery_intent_id, intent.status
      )
      SELECT * FROM exhausted
    LOOP
      affected := affected + 1;
      IF delivery_row.status = 'outcome_unknown' THEN
        PERFORM public.sleepagent_ensure_delivery_reconciliation_v2(
          delivery_row.delivery_intent_id,
          'execution_reclaim_exhausted_after_send_started'
        );
      END IF;
    END LOOP;

  ELSIF requested_kind = 'retention' THEN
    WITH candidate AS (
      SELECT job.retention_job_id
      FROM public.backend_retention_jobs AS job
      WHERE job.data_mode = deployment_mode
        AND job.status = 'running'
        AND job.available_at <= clock_timestamp()
        AND job.lease_expires_at <= clock_timestamp()
        AND job.execution_reclaim_count >= job.max_execution_reclaims
        AND public.sleepagent_worker_claim_authority_v2(
          job.namespace_id, job.data_mode, job.namespace_generation,
          job.run_id, job.arm_id, job.subject_id,
          job.authorization_snapshot_json, 'retention'
        )
      ORDER BY job.updated_at, job.retention_job_id
      FOR UPDATE OF job SKIP LOCKED
      LIMIT requested_limit
    )
    UPDATE public.backend_retention_jobs AS job
    SET status = 'dead_letter', worker_instance = NULL, fencing_token = NULL,
        heartbeat_at = NULL, lease_expires_at = NULL,
        updated_at = clock_timestamp()
    FROM candidate
    WHERE job.retention_job_id = candidate.retention_job_id;
    GET DIAGNOSTICS affected = ROW_COUNT;
  ELSE
    RAISE EXCEPTION 'unsupported reclaim kind';
  END IF;
  RETURN affected;
END;
$$;

REVOKE ALL ON FUNCTION public.sleepagent_exhaust_authorized_reclaims_v2(
  TEXT, TEXT, INTEGER
) FROM PUBLIC;

-- Replace the only four active indirect callers of the mode-wide v1 helper
-- and make ordinary claim selection share the exact row-authority predicate.
DO $$
DECLARE
  definition TEXT;
  original TEXT;
BEGIN
  definition := pg_get_functiondef(
    'public.sleepagent_claim_normalization_work(text,integer)'::regprocedure
  );
  original := definition;
  definition := replace(
    definition,
    'PERFORM public.sleepagent_exhaust_expired_reclaims_v1(''normalization'', NULL);',
    'PERFORM public.sleepagent_exhaust_authorized_reclaims_v2(''normalization'', NULL, 1);'
  );
  definition := replace(
    definition,
    'AND work.data_mode = deployment_mode',
    'AND work.data_mode = deployment_mode' || chr(10) ||
    '      AND public.sleepagent_worker_claim_authority_v2(' || chr(10) ||
    '        work.namespace_id, work.data_mode, work.namespace_generation,' || chr(10) ||
    '        work.run_id, work.arm_id, work.subject_id,' || chr(10) ||
    '        work.authorization_snapshot_json, ''normalization''' || chr(10) ||
    '      )'
  );
  IF definition = original
     OR position('sleepagent_exhaust_expired_reclaims_v1' IN definition) > 0
     OR position('sleepagent_exhaust_authorized_reclaims_v2' IN definition) = 0
     OR position('sleepagent_worker_claim_authority_v2' IN definition) = 0 THEN
    RAISE EXCEPTION 'normalization bounded exhaustion patch drifted';
  END IF;
  EXECUTE definition;

  definition := pg_get_functiondef(
    'public.sleepagent_claim_operation(text,text,integer)'::regprocedure
  );
  original := definition;
  definition := replace(
    definition,
    'PERFORM public.sleepagent_exhaust_expired_reclaims_v1(''operation'', requested_queue);',
    'PERFORM public.sleepagent_exhaust_authorized_reclaims_v2(''operation'', requested_queue, 1);'
  );
  definition := replace(
    definition,
    'AND op.queue_name = requested_queue',
    'AND op.queue_name = requested_queue' || chr(10) ||
    '      AND public.sleepagent_worker_claim_authority_v2(' || chr(10) ||
    '        op.namespace_id, op.data_mode, op.namespace_generation,' || chr(10) ||
    '        op.run_id, op.arm_id, op.subject_id,' || chr(10) ||
    '        COALESCE(op.authorization_snapshot_json,' || chr(10) ||
    '          op.workload_authorization_snapshot_json), requested_queue' || chr(10) ||
    '      )'
  );
  IF definition = original
     OR position('sleepagent_exhaust_expired_reclaims_v1' IN definition) > 0
     OR position('sleepagent_exhaust_authorized_reclaims_v2' IN definition) = 0
     OR position('sleepagent_worker_claim_authority_v2' IN definition) = 0 THEN
    RAISE EXCEPTION 'operation bounded exhaustion patch drifted';
  END IF;
  EXECUTE definition;

  definition := pg_get_functiondef(
    'public.sleepagent_claim_delivery(text,text,integer)'::regprocedure
  );
  original := definition;
  definition := replace(
    definition,
    'PERFORM public.sleepagent_exhaust_expired_reclaims_v1(''delivery'', requested_destination);',
    'PERFORM public.sleepagent_exhaust_authorized_reclaims_v2(''delivery'', requested_destination, 1);'
  );
  definition := replace(
    definition,
    'AND intent.destination = requested_destination',
    'AND intent.destination = requested_destination' || chr(10) ||
    '      AND public.sleepagent_worker_claim_authority_v2(' || chr(10) ||
    '        intent.namespace_id, intent.data_mode, intent.namespace_generation,' || chr(10) ||
    '        intent.run_id, intent.arm_id, intent.subject_id,' || chr(10) ||
    '        intent.authorization_snapshot_json, intent.handler_name' || chr(10) ||
    '      )'
  );
  IF definition = original
     OR position('sleepagent_exhaust_expired_reclaims_v1' IN definition) > 0
     OR position('sleepagent_exhaust_authorized_reclaims_v2' IN definition) = 0
     OR position('sleepagent_worker_claim_authority_v2' IN definition) = 0 THEN
    RAISE EXCEPTION 'delivery bounded exhaustion patch drifted';
  END IF;
  EXECUTE definition;

  definition := pg_get_functiondef(
    'public.sleepagent_claim_retention_job(text,integer)'::regprocedure
  );
  original := definition;
  definition := replace(
    definition,
    'PERFORM public.sleepagent_exhaust_expired_reclaims_v1(''retention'', NULL);',
    'PERFORM public.sleepagent_exhaust_authorized_reclaims_v2(''retention'', NULL, 1);'
  );
  definition := replace(
    definition,
    'WHERE job.data_mode = deployment_mode',
    'WHERE job.data_mode = deployment_mode' || chr(10) ||
    '      AND public.sleepagent_worker_claim_authority_v2(' || chr(10) ||
    '        job.namespace_id, job.data_mode, job.namespace_generation,' || chr(10) ||
    '        job.run_id, job.arm_id, job.subject_id,' || chr(10) ||
    '        job.authorization_snapshot_json, ''retention''' || chr(10) ||
    '      )'
  );
  IF definition = original
     OR position('sleepagent_exhaust_expired_reclaims_v1' IN definition) > 0
     OR position('sleepagent_exhaust_authorized_reclaims_v2' IN definition) = 0
     OR position('sleepagent_worker_claim_authority_v2' IN definition) = 0 THEN
    RAISE EXCEPTION 'retention bounded exhaustion patch drifted';
  END IF;
  EXECUTE definition;
END;
$$;

-- Historical v1 remains in immutable migration history, but no active claim
-- calls it and no bounded runtime principal may execute it directly.
REVOKE ALL ON FUNCTION public.sleepagent_exhaust_expired_reclaims_v1(TEXT, TEXT)
  FROM PUBLIC;

DO $$
DECLARE
  worker_role NAME;
BEGIN
  FOR worker_role IN
    SELECT DISTINCT principal.database_role_name
    FROM public.backend_service_principals AS principal
    WHERE principal.principal_kind = 'worker'
  LOOP
    EXECUTE format(
      'REVOKE ALL ON FUNCTION public.sleepagent_exhaust_expired_reclaims_v1(TEXT, TEXT) FROM %I',
      worker_role
    );
    EXECUTE format(
      'REVOKE SELECT ON TABLE public.sleep_domain_provider_accounts FROM %I',
      worker_role
    );
    EXECUTE format(
      'REVOKE SELECT ON TABLE public.sleep_domain_quarantine FROM %I',
      worker_role
    );
  END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION public.sleepagent_quarantine_scope_allows_v2(
  row_quarantine_namespace_id TEXT,
  row_quarantine_data_mode TEXT,
  row_raw_ingress_record_id TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT
    public.sleepagent_namespace_scope_allows(
      row_quarantine_namespace_id, row_quarantine_data_mode
    )
    AND EXISTS (
      SELECT 1
      FROM public.sleep_domain_raw_inbox AS raw
      WHERE raw.raw_ingress_record_id = row_raw_ingress_record_id
        AND raw.namespace_id = row_quarantine_namespace_id
        AND raw.data_mode = row_quarantine_data_mode
        AND raw.scope_protocol_version >= 2
        AND public.sleepagent_subject_generation_scope_allows(
          raw.namespace_id, raw.data_mode, raw.subject_id,
          raw.namespace_generation, raw.run_id, raw.arm_id
        )
    )
$$;

REVOKE ALL ON FUNCTION public.sleepagent_quarantine_scope_allows_v2(
  TEXT, TEXT, TEXT
) FROM PUBLIC;

DO $$
DECLARE
  worker_role NAME;
BEGIN
  FOR worker_role IN
    SELECT DISTINCT principal.database_role_name
    FROM public.backend_service_principals AS principal
    WHERE principal.principal_kind = 'worker'
  LOOP
    EXECUTE format(
      'GRANT EXECUTE ON FUNCTION public.sleepagent_quarantine_scope_allows_v2(TEXT, TEXT, TEXT) TO %I',
      worker_role
    );
  END LOOP;
END;
$$;

ALTER TABLE public.sleep_domain_provider_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sleep_domain_provider_accounts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_provider_account_scope
  ON public.sleep_domain_provider_accounts;
CREATE POLICY sleep_domain_provider_account_scope
  ON public.sleep_domain_provider_accounts
  USING (public.sleepagent_namespace_scope_allows(namespace_id, data_mode))
  WITH CHECK (public.sleepagent_namespace_scope_allows(namespace_id, data_mode));

ALTER TABLE public.sleep_domain_quarantine ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sleep_domain_quarantine FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_quarantine_scope
  ON public.sleep_domain_quarantine;
CREATE POLICY sleep_domain_quarantine_scope
  ON public.sleep_domain_quarantine
  USING (public.sleepagent_quarantine_scope_allows_v2(
    namespace_id, data_mode, raw_ingress_record_id
  ))
  WITH CHECK (public.sleepagent_quarantine_scope_allows_v2(
    namespace_id, data_mode, raw_ingress_record_id
  ));

-- Migration-time assertions keep the superseding boundary fail-closed.
DO $$
DECLARE
  claim_name TEXT;
  definition TEXT;
BEGIN
  FOREACH claim_name IN ARRAY ARRAY[
    'sleepagent_claim_normalization_work(text,integer)',
    'sleepagent_claim_operation(text,text,integer)',
    'sleepagent_claim_delivery(text,text,integer)',
    'sleepagent_claim_retention_job(text,integer)'
  ]
  LOOP
    definition := pg_get_functiondef(('public.' || claim_name)::regprocedure);
    IF position('sleepagent_exhaust_expired_reclaims_v1' IN definition) > 0
       OR position('sleepagent_exhaust_authorized_reclaims_v2' IN definition) = 0
       OR position('sleepagent_worker_claim_authority_v2' IN definition) = 0 THEN
      RAISE EXCEPTION 'active claim authority patch is incomplete: %', claim_name;
    END IF;
  END LOOP;
  IF NOT (
    SELECT relrowsecurity AND relforcerowsecurity
    FROM pg_class
    WHERE oid = 'public.sleep_domain_provider_accounts'::regclass
  ) OR NOT (
    SELECT relrowsecurity AND relforcerowsecurity
    FROM pg_class
    WHERE oid = 'public.sleep_domain_quarantine'::regclass
  ) THEN
    RAISE EXCEPTION 'provider/quarantine FORCE RLS is incomplete';
  END IF;
END;
$$;
