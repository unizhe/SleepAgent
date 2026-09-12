-- sleepagent:transactional=true

-- A valid Pull response can be quarantined by a normalizer defect and then
-- remain the stable target of later semantic duplicates.  Reprocessing is an
-- explicit actor-authorized transition: preserve the raw/quarantine/receipt
-- evidence, append the existing quarantine-reprocess audit contract, and put
-- only the exact fenced work item back on the normal normalization queue.

ALTER TABLE public.sleep_domain_quarantine_reprocess_audit
  ADD COLUMN IF NOT EXISTS target_work_id TEXT;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_constraint
    WHERE conrelid =
      'public.sleep_domain_quarantine_reprocess_audit'::regclass
      AND conname = 'sleep_domain_quarantine_reprocess_target_work_nonempty'
  ) THEN
    ALTER TABLE public.sleep_domain_quarantine_reprocess_audit
      ADD CONSTRAINT sleep_domain_quarantine_reprocess_target_work_nonempty
      CHECK (target_work_id IS NULL OR target_work_id <> '');
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.sleepagent_requeue_perceptor_pull_quarantine(
  requested_quarantine_id TEXT,
  requested_work_id TEXT,
  requested_request_id TEXT,
  requested_actor_id TEXT,
  requested_authorization_id TEXT,
  requested_reason TEXT,
  requested_at TIMESTAMPTZ
)
RETURNS TABLE (
  work_id TEXT,
  raw_ingress_record_id TEXT,
  status TEXT,
  attempt_count INTEGER
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  namespace_value TEXT := NULLIF(
    current_setting('sleepagent.namespace_id', TRUE), ''
  );
  mode_value TEXT := NULLIF(
    current_setting('sleepagent.data_mode', TRUE), ''
  );
  generation_value BIGINT := NULLIF(
    current_setting('sleepagent.namespace_generation', TRUE), ''
  )::BIGINT;
  subject_value TEXT := NULLIF(
    current_setting('sleepagent.subject_id', TRUE), ''
  );
  actor_value TEXT := NULLIF(
    current_setting('sleepagent.actor_id', TRUE), ''
  );
  purpose_value TEXT := NULLIF(
    current_setting('sleepagent.purpose', TRUE), ''
  );
  audit_row public.sleep_domain_quarantine_reprocess_audit%ROWTYPE;
  target_work public.sleep_domain_normalization_work%ROWTYPE;
  request_payload JSONB;
BEGIN
  IF NOT public.sleepagent_principal_context_allows()
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'api'
     OR mode_value <> 'live'
     OR namespace_value IS NULL
     OR generation_value IS NULL
     OR subject_value IS NULL
     OR actor_value IS DISTINCT FROM requested_actor_id
     OR purpose_value <> 'device_binding_management'
     OR requested_quarantine_id IS NULL OR requested_quarantine_id = ''
     OR requested_work_id IS NULL OR requested_work_id = ''
     OR requested_request_id IS NULL OR requested_request_id = ''
     OR requested_authorization_id IS NULL
     OR requested_authorization_id = ''
     OR requested_reason IS NULL OR requested_reason = ''
     OR requested_at IS NULL
     OR requested_at < clock_timestamp() - INTERVAL '5 minutes'
     OR requested_at > clock_timestamp() + INTERVAL '30 seconds'
     OR NOT public.sleepagent_subject_scope_allows(
       namespace_value, mode_value, subject_value
     )
     OR NOT EXISTS (
       SELECT 1
       FROM public.backend_actor_subject_bindings AS authority
       WHERE authority.binding_id = requested_authorization_id
         AND authority.namespace_id = namespace_value
         AND authority.data_mode = mode_value
         AND authority.actor_id = requested_actor_id
         AND authority.subject_id = subject_value
         AND authority.role = NULLIF(
           current_setting('sleepagent.actor_role', TRUE), ''
         )
         AND authority.status = 'active'
         AND authority.authorization_epoch::TEXT = NULLIF(
           current_setting('sleepagent.authorization_epoch', TRUE), ''
         )
         AND authority.purpose_json ? 'device_binding_management'
         AND authority.valid_from <= clock_timestamp()
         AND (
           authority.valid_until IS NULL
           OR authority.valid_until > clock_timestamp()
         )
     ) THEN
    RAISE EXCEPTION 'invalid or unauthorized Pull quarantine reprocess request';
  END IF;

  request_payload := jsonb_build_object(
    'schema_version', 'quarantine_reprocess_audit.v1',
    'request_id', requested_request_id,
    'data_mode', mode_value,
    'quarantine_ids', jsonb_build_array(requested_quarantine_id),
    'actor_id', requested_actor_id,
    'authorization_id', requested_authorization_id,
    'requested_at', requested_at,
    'reason', requested_reason
  );

  SELECT audit.* INTO audit_row
  FROM public.sleep_domain_quarantine_reprocess_audit AS audit
  WHERE audit.request_id = requested_request_id;
  IF FOUND THEN
    IF audit_row.namespace_id IS DISTINCT FROM namespace_value
       OR audit_row.data_mode IS DISTINCT FROM mode_value
       OR audit_row.actor_id IS DISTINCT FROM requested_actor_id
       OR audit_row.authorization_id IS DISTINCT FROM requested_authorization_id
       OR audit_row.target_work_id IS DISTINCT FROM requested_work_id
       OR audit_row.request_json - 'requested_at'
          IS DISTINCT FROM request_payload - 'requested_at' THEN
      RAISE EXCEPTION 'Pull quarantine reprocess request id conflicts';
    END IF;
    RETURN QUERY
    SELECT work.work_id, work.raw_ingress_record_id,
      work.status, work.attempt_count
    FROM public.sleep_domain_normalization_work AS work
    WHERE work.work_id = requested_work_id
      AND work.namespace_id = namespace_value
      AND work.data_mode = mode_value
      AND work.namespace_generation = generation_value
      AND work.subject_id = subject_value;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'audited Pull quarantine work is unavailable';
    END IF;
    RETURN;
  END IF;

  SELECT work.*
  INTO target_work
  FROM public.sleep_domain_quarantine AS quarantine
  JOIN public.sleep_domain_normalization_work AS work
    ON work.raw_ingress_record_id = quarantine.raw_ingress_record_id
   AND work.namespace_id = quarantine.namespace_id
   AND work.data_mode = quarantine.data_mode
  WHERE quarantine.quarantine_id = requested_quarantine_id
    AND quarantine.namespace_id = namespace_value
    AND quarantine.data_mode = mode_value
    AND quarantine.reason = 'malformed_payload'
    AND quarantine.detail_code = 'ValidationError'
    AND work.work_id = requested_work_id
    AND work.namespace_generation = generation_value
    AND work.subject_id = subject_value
    AND work.status = 'quarantined'
    AND work.last_error_code = 'perceptor_pull_normalization_quarantined'
    AND work.work_json ->> 'normalizer' = 'perceptor_pull'
    AND work.work_json ->> 'endpoint' = '/vitalSigns/getSleepReport'
    AND work.attempt_count < work.max_attempts
    AND work.authorization_snapshot_json ->> 'authorization_epoch' =
      NULLIF(current_setting('sleepagent.authorization_epoch', TRUE), '')
    AND work.authorization_snapshot_json ->> 'privacy_epoch' =
      NULLIF(current_setting('sleepagent.privacy_epoch', TRUE), '')
    AND work.authorization_snapshot_json ->> 'retrieval_policy_epoch' =
      NULLIF(current_setting('sleepagent.retrieval_policy_epoch', TRUE), '')
    AND NOT EXISTS (
      SELECT 1
      FROM public.sleep_domain_processing_receipts AS receipt
      WHERE receipt.namespace_id = work.namespace_id
        AND receipt.data_mode = work.data_mode
        AND receipt.raw_ingress_record_id = work.raw_ingress_record_id
        AND receipt.stage = 'normalization'
        AND receipt.outcome = 'succeeded'
    )
    AND NOT EXISTS (
      SELECT 1
      FROM public.sleep_domain_quarantine AS later_quarantine
      WHERE later_quarantine.namespace_id = quarantine.namespace_id
        AND later_quarantine.data_mode = quarantine.data_mode
        AND later_quarantine.raw_ingress_record_id =
          quarantine.raw_ingress_record_id
        AND later_quarantine.quarantined_at > quarantine.quarantined_at
    )
  FOR UPDATE OF work;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Pull quarantine is not eligible for reprocessing';
  END IF;

  INSERT INTO public.sleep_domain_quarantine_reprocess_audit (
    request_id, namespace_id, data_mode, actor_id, authorization_id,
    request_json, requested_at, target_work_id
  ) VALUES (
    requested_request_id, namespace_value, mode_value,
    requested_actor_id, requested_authorization_id,
    request_payload, requested_at, requested_work_id
  );

  UPDATE public.sleep_domain_normalization_work AS work
  SET status = 'pending',
      available_at = clock_timestamp(),
      last_error_code = NULL,
      lease_owner = NULL,
      lease_expires_at = NULL,
      fencing_token = NULL,
      worker_instance = NULL,
      heartbeat_at = NULL,
      work_json = work.work_json || jsonb_build_object(
        'quarantine_reprocess', jsonb_build_object(
          'schema_version', 'perceptor_pull_quarantine_reprocess.v1',
          'request_id', requested_request_id,
          'quarantine_id', requested_quarantine_id,
          'requested_at', requested_at
        )
      ),
      updated_at = clock_timestamp()
  WHERE work.work_id = target_work.work_id
    AND work.namespace_id = namespace_value
    AND work.data_mode = mode_value
    AND work.status = 'quarantined';
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Pull quarantine reprocess transition lost its fence';
  END IF;

  RETURN QUERY
  SELECT work.work_id, work.raw_ingress_record_id,
    work.status, work.attempt_count
  FROM public.sleep_domain_normalization_work AS work
  WHERE work.work_id = target_work.work_id
    AND work.namespace_id = namespace_value
    AND work.data_mode = mode_value;
END;
$$;

REVOKE ALL ON FUNCTION
  public.sleepagent_requeue_perceptor_pull_quarantine(
    TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TIMESTAMPTZ
  ) FROM PUBLIC;

DO $$
DECLARE
  definition TEXT;
BEGIN
  definition := pg_get_functiondef(
    'public.sleepagent_requeue_perceptor_pull_quarantine(text,text,text,text,text,text,timestamptz)'::regprocedure
  );
  IF position('sleepagent_principal_context_allows' IN definition) = 0
     OR position('sleepagent_subject_scope_allows' IN definition) = 0
     OR position('quarantine_reprocess_audit.v1' IN definition) = 0
     OR position('backend_actor_subject_bindings' IN definition) = 0
     OR position('target_work_id' IN definition) = 0
     OR position('FOR UPDATE OF work' IN definition) = 0
     OR position('work.status = ''quarantined''' IN definition) = 0
     OR position('receipt.outcome = ''succeeded''' IN definition) = 0 THEN
    RAISE EXCEPTION 'Pull quarantine reprocess authority is incomplete';
  END IF;
END;
$$;
