-- sleepagent:transactional=true

-- B0 incident stabilization: the generic normalization claim remains the
-- backward-compatible authority.  This additive entry point lets explicitly
-- profiled workers claim one persisted normalizer class, so an old, expensive
-- History retry cannot occupy realtime Push capacity.

CREATE OR REPLACE FUNCTION public.sleepagent_claim_normalization_work_by_normalizer(
  requested_normalizer TEXT,
  claimant_worker_instance TEXT,
  requested_lease_seconds INTEGER
)
RETURNS TABLE(
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
  IF requested_normalizer NOT IN ('perceptor_push', 'perceptor_pull')
     OR claimant_worker_instance IS NULL
     OR claimant_worker_instance = ''
     OR requested_lease_seconds < 1
     OR requested_lease_seconds > 3600
     OR principal IS NULL
     OR deployment_mode <> 'live'
     OR request_purpose IS NULL
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'worker'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid or untrusted classified normalization claim context';
  END IF;

  PERFORM public.sleepagent_exhaust_authorized_reclaims_v2(
    'normalization', NULL, 1
  );

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
      AND work.work_json ->> 'normalizer' = requested_normalizer
      AND public.sleepagent_worker_claim_authority_v2(
        work.namespace_id, work.data_mode, work.namespace_generation,
        work.run_id, work.arm_id, work.subject_id,
        work.authorization_snapshot_json, 'normalization'
      )
      AND (
        (work.status IN ('pending', 'retry')
          AND work.attempt_count < work.max_attempts)
        OR (work.status = 'running'
          AND work.lease_expires_at <= clock_timestamp()
          AND work.execution_reclaim_count < work.max_execution_reclaims)
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
          AND grant_row.authorization_epoch = epoch_row.authorization_epoch
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
      work.created_at,
      work.work_id
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
        execution_reclaim_count = CASE
          WHEN candidate.starts_business_attempt
            THEN work.execution_reclaim_count
          ELSE work.execution_reclaim_count + 1
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

REVOKE ALL ON FUNCTION
  public.sleepagent_claim_normalization_work_by_normalizer(
    TEXT, TEXT, INTEGER
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
      'GRANT EXECUTE ON FUNCTION public.sleepagent_claim_normalization_work_by_normalizer(TEXT, TEXT, INTEGER) TO %I',
      worker_role
    );
  END LOOP;
END;
$$;

DO $$
DECLARE
  definition TEXT;
BEGIN
  definition := pg_get_functiondef(
    'public.sleepagent_claim_normalization_work_by_normalizer(text,text,integer)'::regprocedure
  );
  IF position('sleepagent_worker_claim_authority_v2' IN definition) = 0
     OR position('sleepagent_exhaust_authorized_reclaims_v2' IN definition) = 0
     OR position('work.work_json ->> ''normalizer'' = requested_normalizer' IN definition) = 0
     OR position('FOR UPDATE OF namespace_row, work SKIP LOCKED' IN definition) = 0 THEN
    RAISE EXCEPTION 'classified normalization claim authority is incomplete';
  END IF;
END;
$$;
