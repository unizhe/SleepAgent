-- sleepagent:transactional=true
-- Separate logical/business attempt accounting from execution ownership.
-- A fresh pending/retry claim consumes one business attempt.  Reclaiming an
-- expired execution preserves that attempt while rotating its lease fence.

CREATE OR REPLACE FUNCTION sleepagent_claim_normalization_work(
  claimant_worker_instance TEXT,
  requested_lease_seconds INTEGER
)
RETURNS TABLE (
  work_id TEXT,
  namespace_id TEXT,
  data_mode TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT,
  raw_ingress_record_id TEXT,
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
BEGIN
  IF claimant_worker_instance IS NULL OR claimant_worker_instance = ''
     OR requested_lease_seconds < 1 OR requested_lease_seconds > 3600
     OR principal IS NULL OR deployment_mode NOT IN ('live', 'replay')
     OR request_purpose IS NULL
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <>
       'worker'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid or untrusted normalization claim context';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT work.work_id,
      work.status IN ('pending', 'retry') AS starts_business_attempt,
      epoch_row.authorization_epoch AS claim_authorization_epoch,
      epoch_row.privacy_epoch AS claim_privacy_epoch,
      epoch_row.retrieval_policy_epoch AS claim_retrieval_policy_epoch
    FROM public.sleep_domain_normalization_work AS work
    JOIN public.backend_namespaces AS namespace_row
      ON namespace_row.namespace_id = work.namespace_id
     AND namespace_row.data_mode = work.data_mode
    JOIN public.backend_subject_epochs AS epoch_row
      ON epoch_row.namespace_id = work.namespace_id
     AND epoch_row.data_mode = work.data_mode
     AND epoch_row.subject_id = work.subject_id
    WHERE work.protocol_version >= 2
      AND work.data_mode = deployment_mode
      AND (
        (work.status IN ('pending', 'retry')
          AND work.attempt_count < work.max_attempts)
        OR (work.status = 'running'
          AND work.lease_expires_at <= clock_timestamp())
      )
      AND work.available_at <= clock_timestamp()
      AND (
        work.predecessor_work_id IS NULL
        OR EXISTS (
          SELECT 1
          FROM public.sleep_domain_normalization_work AS predecessor
          WHERE predecessor.work_id = work.predecessor_work_id
            AND predecessor.status = 'succeeded'
        )
      )
      AND namespace_row.status = 'active'
      AND public.sleepagent_try_reserve_namespace_capacity(
        work.namespace_id, work.data_mode
      )
      AND work.authorization_snapshot_json ->> 'authorization_epoch' =
        epoch_row.authorization_epoch::text
      AND work.authorization_snapshot_json ->> 'privacy_epoch' =
        epoch_row.privacy_epoch::text
      AND work.authorization_snapshot_json ->> 'retrieval_policy_epoch' =
        epoch_row.retrieval_policy_epoch::text
      AND EXISTS (
        SELECT 1
        FROM public.backend_principal_grants AS grant_row
        WHERE grant_row.principal_id = principal
          AND grant_row.namespace_id = work.namespace_id
          AND grant_row.data_mode = work.data_mode
          AND grant_row.purpose = request_purpose
          AND grant_row.authorization_epoch =
            epoch_row.authorization_epoch
          AND grant_row.status = 'active'
          AND grant_row.valid_from <= clock_timestamp()
          AND (
            grant_row.valid_until IS NULL
            OR grant_row.valid_until > clock_timestamp()
          )
          AND grant_row.allowed_handlers_json ? 'normalization'
      )
    ORDER BY public.sleepagent_namespace_last_claimed_at(
        'ingestion', work.namespace_id, work.data_mode
      ),
      work.available_at,
      COALESCE(work.stream_sequence, 9223372036854775807),
      work.created_at, work.work_id
    FOR UPDATE OF namespace_row, work SKIP LOCKED
    LIMIT 1
  ), claimed AS (
    UPDATE public.sleep_domain_normalization_work AS work
    SET status = 'running',
        attempt_count = CASE
          WHEN candidate.starts_business_attempt
            THEN work.attempt_count + 1
          ELSE work.attempt_count
        END,
        lease_generation = work.lease_generation + 1,
        fencing_token = gen_random_uuid()::text,
        worker_instance = claimant_worker_instance,
        lease_owner = claimant_worker_instance,
        heartbeat_at = clock_timestamp(),
        lease_expires_at = clock_timestamp()
          + make_interval(secs => requested_lease_seconds),
        updated_at = clock_timestamp()
    FROM candidate
    WHERE work.work_id = candidate.work_id
    RETURNING work.work_id, work.namespace_id, work.data_mode,
      work.namespace_generation, work.run_id, work.arm_id,
      work.subject_id, work.raw_ingress_record_id,
      candidate.claim_authorization_epoch,
      candidate.claim_privacy_epoch,
      candidate.claim_retrieval_policy_epoch,
      work.lease_generation, work.fencing_token
  )
  SELECT claimed.work_id, claimed.namespace_id, claimed.data_mode,
    claimed.namespace_generation, claimed.run_id, claimed.arm_id,
    claimed.subject_id, claimed.raw_ingress_record_id,
    claimed.claim_authorization_epoch, claimed.claim_privacy_epoch,
    claimed.claim_retrieval_policy_epoch, claimed.lease_generation,
    claimed.fencing_token
  FROM claimed;
END;
$$;

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
      op.status IN ('pending', 'retry') AS starts_business_attempt,
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
        (op.status IN ('pending', 'retry')
          AND op.attempt_count < op.max_attempts)
        OR (op.status = 'running'
          AND op.lease_expires_at <= clock_timestamp())
      )
      AND op.available_at <= clock_timestamp()
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
      AND public.sleepagent_try_reserve_namespace_capacity(
        op.namespace_id, op.data_mode
      )
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
    ORDER BY public.sleepagent_namespace_last_claimed_at(
        requested_queue, op.namespace_id, op.data_mode
      ),
      op.priority DESC, op.available_at, op.created_at, op.operation_id
    FOR UPDATE OF ns, op SKIP LOCKED
    LIMIT 1
  ), claimed AS (
    UPDATE public.sleep_domain_operations AS op
    SET status = 'running',
        attempt_count = CASE
          WHEN candidate.starts_business_attempt
            THEN op.attempt_count + 1
          ELSE op.attempt_count
        END,
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

CREATE OR REPLACE FUNCTION sleepagent_claim_delivery(
  requested_destination TEXT,
  claimant_worker_instance TEXT,
  requested_lease_seconds INTEGER
)
RETURNS TABLE (
  delivery_intent_id TEXT,
  namespace_id TEXT,
  data_mode TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT,
  handler_name TEXT,
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
BEGIN
  IF requested_destination IS NULL OR requested_destination = ''
     OR claimant_worker_instance IS NULL OR claimant_worker_instance = ''
     OR requested_lease_seconds < 1 OR requested_lease_seconds > 3600
     OR principal IS NULL OR deployment_mode NOT IN ('live', 'replay')
     OR request_purpose IS NULL
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <>
       'worker'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid or untrusted delivery claim context';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT intent.delivery_intent_id,
      intent.status IN ('pending', 'retry') AS starts_business_attempt,
      epoch_row.authorization_epoch AS claim_authorization_epoch,
      epoch_row.privacy_epoch AS claim_privacy_epoch,
      epoch_row.retrieval_policy_epoch AS claim_retrieval_policy_epoch
    FROM public.backend_delivery_intents AS intent
    JOIN public.backend_namespaces AS namespace_row
      ON namespace_row.namespace_id = intent.namespace_id
     AND namespace_row.data_mode = intent.data_mode
    JOIN public.backend_subject_epochs AS epoch_row
      ON epoch_row.namespace_id = intent.namespace_id
     AND epoch_row.data_mode = intent.data_mode
     AND epoch_row.subject_id = intent.subject_id
    WHERE intent.data_mode = deployment_mode
      AND intent.destination = requested_destination
      AND (
        (intent.status IN ('pending', 'retry')
          AND intent.attempt_count < intent.max_attempts)
        OR (intent.status IN ('running', 'dispatching')
          AND intent.lease_expires_at <= clock_timestamp())
      )
      AND intent.available_at <= clock_timestamp()
      AND namespace_row.status = 'active'
      AND public.sleepagent_try_reserve_namespace_capacity(
        intent.namespace_id, intent.data_mode
      )
      AND intent.authorization_snapshot_json ->> 'authorization_epoch' =
        epoch_row.authorization_epoch::text
      AND intent.authorization_snapshot_json ->> 'privacy_epoch' =
        epoch_row.privacy_epoch::text
      AND intent.authorization_snapshot_json ->>
        'retrieval_policy_epoch' = epoch_row.retrieval_policy_epoch::text
      AND (
        intent.predecessor_sequence IS NULL
        OR EXISTS (
          SELECT 1
          FROM public.backend_delivery_intents AS predecessor
          WHERE predecessor.destination = intent.destination
            AND predecessor.aggregate_type = intent.aggregate_type
            AND predecessor.aggregate_id = intent.aggregate_id
            AND predecessor.aggregate_sequence =
              intent.predecessor_sequence
            AND predecessor.status = 'delivered'
        )
      )
      AND EXISTS (
        SELECT 1
        FROM public.backend_principal_grants AS grant_row
        WHERE grant_row.principal_id = principal
          AND grant_row.namespace_id = intent.namespace_id
          AND grant_row.data_mode = intent.data_mode
          AND grant_row.purpose = request_purpose
          AND grant_row.authorization_epoch =
            epoch_row.authorization_epoch
          AND grant_row.status = 'active'
          AND grant_row.valid_from <= clock_timestamp()
          AND (
            grant_row.valid_until IS NULL
            OR grant_row.valid_until > clock_timestamp()
          )
          AND grant_row.allowed_handlers_json ? intent.handler_name
      )
    ORDER BY public.sleepagent_namespace_last_claimed_at(
        requested_destination, intent.namespace_id, intent.data_mode
      ),
      intent.priority DESC, intent.available_at,
      intent.created_at, intent.delivery_intent_id
    FOR UPDATE OF namespace_row, intent SKIP LOCKED
    LIMIT 1
  ), claimed AS (
    UPDATE public.backend_delivery_intents AS intent
    SET status = 'running',
        attempt_count = CASE
          WHEN candidate.starts_business_attempt
            THEN intent.attempt_count + 1
          ELSE intent.attempt_count
        END,
        lease_generation = intent.lease_generation + 1,
        fencing_token = gen_random_uuid()::text,
        worker_instance = claimant_worker_instance,
        lease_expires_at = clock_timestamp()
          + make_interval(secs => requested_lease_seconds),
        dispatch_permit_at = NULL,
        updated_at = clock_timestamp()
    FROM candidate
    WHERE intent.delivery_intent_id = candidate.delivery_intent_id
    RETURNING intent.delivery_intent_id, intent.namespace_id,
      intent.data_mode, intent.namespace_generation, intent.run_id,
      intent.arm_id, intent.subject_id, intent.handler_name,
      candidate.claim_authorization_epoch,
      candidate.claim_privacy_epoch,
      candidate.claim_retrieval_policy_epoch,
      intent.lease_generation, intent.fencing_token
  )
  SELECT claimed.delivery_intent_id, claimed.namespace_id,
    claimed.data_mode, claimed.namespace_generation, claimed.run_id,
    claimed.arm_id, claimed.subject_id, claimed.handler_name,
    claimed.claim_authorization_epoch, claimed.claim_privacy_epoch,
    claimed.claim_retrieval_policy_epoch, claimed.lease_generation,
    claimed.fencing_token
  FROM claimed;
END;
$$;

CREATE OR REPLACE FUNCTION sleepagent_claim_retention_job(
  claimant_worker_instance TEXT,
  requested_lease_seconds INTEGER
)
RETURNS TABLE (
  retention_job_id TEXT,
  namespace_id TEXT,
  data_mode TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT,
  retention_domain TEXT,
  dek_generation BIGINT,
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
      'trusted retention principal/data mode/purpose context is required';
  END IF;
  IF claimant_worker_instance IS NULL OR claimant_worker_instance = ''
     OR requested_lease_seconds < 1 OR requested_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'invalid retention claim parameters';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT job.retention_job_id,
      job.status IN ('pending', 'retry') AS starts_business_attempt,
      epoch_row.authorization_epoch AS claim_authorization_epoch,
      epoch_row.privacy_epoch AS claim_privacy_epoch,
      epoch_row.retrieval_policy_epoch AS claim_retrieval_policy_epoch
    FROM public.backend_retention_jobs AS job
    JOIN public.backend_namespaces AS ns
     ON ns.namespace_id = job.namespace_id
     AND ns.data_mode = job.data_mode
    JOIN public.backend_subject_epochs AS epoch_row
      ON epoch_row.namespace_id = job.namespace_id
     AND epoch_row.data_mode = job.data_mode
     AND epoch_row.subject_id = job.subject_id
    WHERE job.data_mode = deployment_mode
      AND (
        (job.status IN ('pending', 'retry')
          AND job.attempt_count < job.max_attempts)
        OR (job.status = 'running'
          AND job.lease_expires_at <= clock_timestamp())
      )
      AND job.available_at <= clock_timestamp()
      AND ns.status = 'active'
      AND public.sleepagent_try_reserve_namespace_capacity(
        job.namespace_id, job.data_mode
      )
      AND job.authorization_snapshot_json ->> 'authorization_epoch' =
        epoch_row.authorization_epoch::text
      AND job.authorization_snapshot_json ->> 'privacy_epoch' =
        epoch_row.privacy_epoch::text
      AND job.authorization_snapshot_json ->> 'retrieval_policy_epoch' =
        epoch_row.retrieval_policy_epoch::text
      AND EXISTS (
        SELECT 1
        FROM public.backend_principal_grants AS grant_row
        WHERE grant_row.principal_id = principal
          AND grant_row.namespace_id = job.namespace_id
          AND grant_row.data_mode = job.data_mode
          AND grant_row.purpose = request_purpose
          AND grant_row.authorization_epoch =
            epoch_row.authorization_epoch
          AND grant_row.status = 'active'
          AND grant_row.valid_from <= clock_timestamp()
          AND (
            grant_row.valid_until IS NULL
            OR grant_row.valid_until > clock_timestamp()
          )
          AND grant_row.allowed_handlers_json ? 'retention'
          AND EXISTS (
            SELECT 1
            FROM public.backend_service_principals AS service_principal
            WHERE service_principal.principal_id = principal
              AND service_principal.status = 'active'
          )
      )
    ORDER BY public.sleepagent_namespace_last_claimed_at(
        'retention', job.namespace_id, job.data_mode
      ),
      job.priority DESC, job.available_at, job.created_at,
      job.retention_job_id
    FOR UPDATE OF ns, job SKIP LOCKED
    LIMIT 1
  ), claimed AS (
    UPDATE public.backend_retention_jobs AS job
    SET status = 'running',
        attempt_count = CASE
          WHEN candidate.starts_business_attempt
            THEN job.attempt_count + 1
          ELSE job.attempt_count
        END,
        lease_generation = job.lease_generation + 1,
        fencing_token = gen_random_uuid()::text,
        worker_instance = claimant_worker_instance,
        heartbeat_at = clock_timestamp(),
        lease_expires_at = clock_timestamp()
          + make_interval(secs => requested_lease_seconds),
        updated_at = clock_timestamp()
    FROM candidate
    WHERE job.retention_job_id = candidate.retention_job_id
    RETURNING job.retention_job_id, job.namespace_id, job.data_mode,
      job.namespace_generation, job.run_id, job.arm_id, job.subject_id,
      job.retention_domain, job.dek_generation,
      candidate.claim_authorization_epoch, candidate.claim_privacy_epoch,
      candidate.claim_retrieval_policy_epoch, job.lease_generation,
      job.fencing_token
  )
  SELECT claimed.retention_job_id, claimed.namespace_id, claimed.data_mode,
    claimed.namespace_generation, claimed.run_id, claimed.arm_id,
    claimed.subject_id, claimed.retention_domain, claimed.dek_generation,
    claimed.claim_authorization_epoch, claimed.claim_privacy_epoch,
    claimed.claim_retrieval_policy_epoch,
    claimed.lease_generation, claimed.fencing_token
  FROM claimed;
END;
$$;
