-- sleepagent:transactional=true
-- P4-D: one authenticated external-service ingress function.  It preserves
-- exact encrypted bytes in the existing immutable inbox, resolves subject
-- authority only from database-owned DeviceBinding rows, and creates only the
-- existing normalization work item.  No Product/Agent operation is emitted.

CREATE OR REPLACE FUNCTION sleepagent_ingest_perceptor_push(
  target_namespace_id TEXT,
  target_namespace_generation BIGINT,
  target_provider_account_id TEXT,
  asserted_client_id_sha256 TEXT,
  provider_device_id TEXT,
  provider_device_name TEXT,
  push_event_type TEXT,
  safe_message_identity TEXT,
  request_signed_at TIMESTAMPTZ,
  received_at TIMESTAMPTZ,
  requested_idempotency_identity TEXT,
  payload_sha256 TEXT,
  encrypted_payload BYTEA,
  encryption_key_id TEXT,
  content_type TEXT,
  payload_size_bytes INTEGER,
  retention_until TIMESTAMPTZ,
  signature_profile TEXT,
  request_is_fresh BOOLEAN,
  event_is_supported BOOLEAN,
  proposed_raw_ingress_record_id TEXT,
  proposed_normalization_work_id TEXT,
  proposed_intake_receipt_id TEXT,
  proposed_quarantine_receipt_id TEXT,
  proposed_quarantine_id TEXT,
  safe_metadata_json JSONB
)
RETURNS TABLE (
  disposition TEXT,
  raw_ingress_record_id TEXT,
  normalization_work_id TEXT,
  subject_id TEXT,
  device_binding_id TEXT,
  duplicate BOOLEAN
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  account_matches INTEGER;
  existing_raw_id TEXT;
  existing_payload_sha256 TEXT;
  existing_work_id TEXT;
  existing_subject_id TEXT;
  existing_binding_id TEXT;
  conflict_identity TEXT;
  binding_matches INTEGER;
  resolved_binding_id TEXT;
  resolved_subject_id TEXT;
  resolved_authorization_epoch BIGINT;
  resolved_privacy_epoch BIGINT;
  resolved_retrieval_policy_epoch BIGINT;
  normalization_worker_matches INTEGER;
  normalization_worker_principal TEXT;
  normalization_worker_purpose TEXT;
  quarantine_reason TEXT;
  quarantine_disposition TEXT;
BEGIN
  IF NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'api'
     OR NULLIF(current_setting('sleepagent.purpose', TRUE), '') <>
       'perceptor_ingress'
     OR target_namespace_id IS NULL
     OR target_namespace_generation < 1
     OR target_provider_account_id IS NULL
     OR asserted_client_id_sha256 !~ '^[0-9a-f]{64}$'
     OR push_event_type IS NULL
     OR requested_idempotency_identity IS NULL
     OR payload_sha256 !~ '^[0-9a-f]{64}$'
     OR encrypted_payload IS NULL
     OR encryption_key_id IS NULL
     OR content_type IS NULL
     OR payload_size_bytes < 1
     OR retention_until <= received_at
     OR signature_profile IS NULL
     OR proposed_raw_ingress_record_id IS NULL
     OR proposed_normalization_work_id IS NULL
     OR proposed_intake_receipt_id IS NULL
     OR proposed_quarantine_receipt_id IS NULL
     OR proposed_quarantine_id IS NULL
     OR safe_metadata_json IS NULL
     OR jsonb_typeof(safe_metadata_json) <> 'object'
     OR target_namespace_id <> NULLIF(
       current_setting('sleepagent.namespace_id', TRUE), ''
     )
     OR target_namespace_generation::text <> NULLIF(
       current_setting('sleepagent.namespace_generation', TRUE), ''
     )
     OR NOT public.sleepagent_namespace_scope_allows(
       target_namespace_id, 'live'
     ) THEN
    RAISE EXCEPTION 'invalid or untrusted Perceptor ingress context';
  END IF;

  SELECT count(*) INTO account_matches
  FROM public.sleep_domain_provider_accounts AS account
  WHERE account.namespace_id = target_namespace_id
    AND account.data_mode = 'live'
    AND account.provider_account_id = target_provider_account_id
    AND account.provider_id = 'perceptor'
    AND account.status = 'active'
    AND account.account_metadata_json ->> 'client_id_sha256' =
      asserted_client_id_sha256;
  IF account_matches <> 1 THEN
    RAISE EXCEPTION 'Perceptor provider account binding mismatch';
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    target_namespace_id || chr(31) || target_provider_account_id || chr(31) ||
      requested_idempotency_identity,
    0
  ));

  SELECT raw.raw_ingress_record_id,
         raw.pre_normalization_payload_sha256,
         work.work_id,
         raw.subject_id,
         work.work_json ->> 'device_binding_id'
  INTO existing_raw_id, existing_payload_sha256, existing_work_id,
       existing_subject_id, existing_binding_id
  FROM public.sleep_domain_raw_inbox AS raw
  LEFT JOIN public.sleep_domain_normalization_work AS work
    ON work.raw_ingress_record_id = raw.raw_ingress_record_id
   AND work.namespace_id = raw.namespace_id
   AND work.data_mode = raw.data_mode
   AND work.work_generation = 1
  WHERE raw.namespace_id = target_namespace_id
    AND raw.data_mode = 'live'
    AND raw.provider_account_id = target_provider_account_id
    AND raw.idempotency_identity = requested_idempotency_identity;

  IF existing_raw_id IS NOT NULL
     AND existing_payload_sha256 = payload_sha256 THEN
    RETURN QUERY SELECT
      'duplicate'::TEXT, existing_raw_id, existing_work_id,
      existing_subject_id, existing_binding_id, TRUE;
    RETURN;
  END IF;

  conflict_identity := 'perceptor-conflict.v1:' || encode(digest(
    requested_idempotency_identity || chr(31) || payload_sha256,
    'sha256'
  ), 'hex');
  IF existing_raw_id IS NOT NULL THEN
    SELECT raw.raw_ingress_record_id
    INTO raw_ingress_record_id
    FROM public.sleep_domain_raw_inbox AS raw
    WHERE raw.namespace_id = target_namespace_id
      AND raw.data_mode = 'live'
      AND raw.provider_account_id = target_provider_account_id
      AND raw.idempotency_identity = conflict_identity
      AND raw.pre_normalization_payload_sha256 = payload_sha256;
    IF raw_ingress_record_id IS NOT NULL THEN
      RETURN QUERY SELECT
        'quarantined_conflict'::TEXT, raw_ingress_record_id, NULL::TEXT,
        NULL::TEXT, NULL::TEXT, TRUE;
      RETURN;
    END IF;
  END IF;

  IF NOT request_is_fresh THEN
    RETURN QUERY SELECT
      'stale'::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, FALSE;
    RETURN;
  END IF;

  SELECT count(*), min(binding.device_binding_id), min(binding.subject_id)
  INTO binding_matches, resolved_binding_id, resolved_subject_id
  FROM public.sleep_domain_device_bindings AS binding
  WHERE binding.namespace_id = target_namespace_id
    AND binding.data_mode = 'live'
    AND binding.provider_id = 'perceptor'
    AND binding.provider_account_id = target_provider_account_id
    AND binding.status = 'active'
    AND binding.effective_from <= received_at
    AND (binding.effective_until IS NULL OR binding.effective_until > received_at)
    AND (
      (provider_device_id IS NOT NULL AND provider_device_id <> ''
        AND binding.binding_json #>> '{provider_device,provider_device_id}' =
          provider_device_id)
      OR
      ((provider_device_id IS NULL OR provider_device_id = '')
        AND provider_device_name IS NOT NULL AND provider_device_name <> ''
        AND binding.binding_json #>> '{provider_device,provider_device_name}' =
          provider_device_name)
    );

  IF binding_matches = 1 THEN
    SELECT epoch.authorization_epoch, epoch.privacy_epoch,
           epoch.retrieval_policy_epoch
    INTO resolved_authorization_epoch, resolved_privacy_epoch,
         resolved_retrieval_policy_epoch
    FROM public.backend_subject_epochs AS epoch
    WHERE epoch.namespace_id = target_namespace_id
      AND epoch.data_mode = 'live'
      AND epoch.subject_id = resolved_subject_id;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'bound Perceptor subject has no governance epochs';
    END IF;
    SELECT count(*), min(grant_row.principal_id), min(grant_row.purpose)
    INTO normalization_worker_matches, normalization_worker_principal,
         normalization_worker_purpose
    FROM public.backend_principal_grants AS grant_row
    JOIN public.backend_service_principals AS principal
      ON principal.principal_id = grant_row.principal_id
     AND principal.principal_kind = 'worker'
     AND principal.status = 'active'
    WHERE grant_row.namespace_id = target_namespace_id
      AND grant_row.data_mode = 'live'
      AND grant_row.authorization_epoch = resolved_authorization_epoch
      AND grant_row.status = 'active'
      AND grant_row.valid_from <= clock_timestamp()
      AND (grant_row.valid_until IS NULL
        OR grant_row.valid_until > clock_timestamp())
      AND grant_row.allowed_handlers_json ? 'normalization';
    IF normalization_worker_matches <> 1 THEN
      RAISE EXCEPTION 'Perceptor normalization requires one exact worker grant';
    END IF;
  ELSE
    resolved_binding_id := NULL;
    resolved_subject_id := NULL;
  END IF;

  IF existing_raw_id IS NOT NULL THEN
    requested_idempotency_identity := conflict_identity;
    quarantine_reason := 'message_id_collision';
    quarantine_disposition := 'quarantined_conflict';
    resolved_binding_id := NULL;
    resolved_subject_id := NULL;
  ELSIF binding_matches = 0 THEN
    quarantine_reason := 'device_unbound';
    quarantine_disposition := 'quarantined_unbound';
  ELSIF binding_matches > 1 THEN
    quarantine_reason := 'device_binding_ambiguous';
    quarantine_disposition := 'quarantined_unbound';
    resolved_binding_id := NULL;
    resolved_subject_id := NULL;
  ELSIF NOT event_is_supported THEN
    quarantine_reason := 'unknown_format';
    quarantine_disposition := 'quarantined_unknown';
  END IF;

  INSERT INTO public.sleep_domain_raw_inbox (
    raw_ingress_record_id, namespace_id, data_mode,
    provider_id, provider_account_id, event_type, message_id,
    request_signed_at, measurement_at, event_occurred_at,
    received_at, signature_profile, signature_verification,
    idempotency_identity, idempotency_version,
    pre_normalization_payload_sha256, encrypted_payload,
    encryption_key_id, encrypted_at, content_type,
    payload_size_bytes, retention_until, raw_metadata_json,
    scope_protocol_version, namespace_generation, run_id,
    arm_id, subject_id, encryption_protocol_version
  ) VALUES (
    proposed_raw_ingress_record_id, target_namespace_id, 'live',
    'perceptor', target_provider_account_id, push_event_type,
    safe_message_identity, request_signed_at, NULL, NULL,
    received_at, signature_profile, 'verified',
    requested_idempotency_identity, 'perceptor-push.v1', payload_sha256,
    encrypted_payload, encryption_key_id, received_at, content_type,
    payload_size_bytes, retention_until,
    safe_metadata_json || jsonb_build_object(
      'schema_version', 'perceptor_raw_ingress_metadata.v1',
      'device_binding_resolved', resolved_binding_id IS NOT NULL,
      'quarantine_reason', quarantine_reason
    ),
    CASE WHEN quarantine_reason IS NULL THEN 2 ELSE 1 END,
    CASE WHEN quarantine_reason IS NULL THEN target_namespace_generation ELSE NULL END,
    NULL, NULL,
    CASE WHEN quarantine_reason IS NULL THEN resolved_subject_id ELSE NULL END,
    1
  );

  INSERT INTO public.sleep_domain_processing_receipts (
    receipt_id, namespace_id, data_mode, raw_ingress_record_id,
    stage, outcome, receipt_json, occurred_at
  ) VALUES (
    proposed_intake_receipt_id, target_namespace_id, 'live',
    proposed_raw_ingress_record_id, 'intake', 'accepted',
    jsonb_build_object(
      'schema_version', 'processing_receipt.v2',
      'receipt_id', proposed_intake_receipt_id,
      'raw_ingress_record_id', proposed_raw_ingress_record_id,
      'stage', 'intake',
      'outcome', 'accepted',
      'signature_verified', TRUE
    ),
    received_at
  );

  IF quarantine_reason IS NOT NULL THEN
    INSERT INTO public.sleep_domain_processing_receipts (
      receipt_id, namespace_id, data_mode, raw_ingress_record_id,
      stage, outcome, quarantine_reason, receipt_json, occurred_at
    ) VALUES (
      proposed_quarantine_receipt_id, target_namespace_id, 'live',
      proposed_raw_ingress_record_id, 'normalization', 'quarantined',
      quarantine_reason,
      jsonb_build_object(
        'schema_version', 'processing_receipt.v2',
        'receipt_id', proposed_quarantine_receipt_id,
        'raw_ingress_record_id', proposed_raw_ingress_record_id,
        'stage', 'normalization',
        'outcome', 'quarantined',
        'quarantine_reason', quarantine_reason
      ),
      received_at
    );
    INSERT INTO public.sleep_domain_quarantine (
      quarantine_id, namespace_id, data_mode, raw_ingress_record_id,
      reason, detail_code, receipt_id, quarantine_json, quarantined_at
    ) VALUES (
      proposed_quarantine_id, target_namespace_id, 'live',
      proposed_raw_ingress_record_id, quarantine_reason,
      quarantine_disposition, proposed_quarantine_receipt_id,
      jsonb_build_object(
        'schema_version', 'perceptor_quarantine.v1',
        'reason', quarantine_reason,
        'detail_code', quarantine_disposition,
        'raw_ingress_record_id', proposed_raw_ingress_record_id
      ),
      received_at
    );
    RETURN QUERY SELECT
      quarantine_disposition, proposed_raw_ingress_record_id, NULL::TEXT,
      NULL::TEXT, NULL::TEXT, FALSE;
    RETURN;
  END IF;

  INSERT INTO public.sleep_domain_normalization_work (
    work_id, namespace_id, data_mode, raw_ingress_record_id,
    work_generation, status, attempt_count, available_at,
    work_json, created_at, updated_at, protocol_version,
    namespace_generation, run_id, arm_id, subject_id,
    authorization_snapshot_json, max_attempts, lease_generation
  ) VALUES (
    proposed_normalization_work_id, target_namespace_id, 'live',
    proposed_raw_ingress_record_id, 1, 'pending', 0, received_at,
    jsonb_build_object(
      'schema_version', 'normalization_work.v2',
      'normalizer', 'perceptor_push',
      'raw_ingress_record_id', proposed_raw_ingress_record_id,
      'payload_sha256', payload_sha256,
      'device_binding_id', resolved_binding_id
    ),
    received_at, received_at, 2, target_namespace_generation,
    NULL, NULL, resolved_subject_id,
    jsonb_build_object(
      'schema_version', 'workload_authorization_snapshot.v1',
      'workload_principal_id', normalization_worker_principal,
      'namespace_id', target_namespace_id,
      'namespace_generation', target_namespace_generation,
      'data_mode', 'live',
      'run_id', NULL,
      'arm_id', NULL,
      'subject_id', resolved_subject_id,
      'purpose', normalization_worker_purpose,
      'allowed_handler', 'normalization',
      'authorization_epoch', resolved_authorization_epoch,
      'privacy_epoch', resolved_privacy_epoch,
      'retrieval_policy_epoch', resolved_retrieval_policy_epoch
    ),
    8, 0
  );

  RETURN QUERY SELECT
    'accepted'::TEXT, proposed_raw_ingress_record_id,
    proposed_normalization_work_id, resolved_subject_id,
    resolved_binding_id, FALSE;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_ingest_perceptor_push(
  TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT,
  TIMESTAMPTZ, TIMESTAMPTZ, TEXT, TEXT, BYTEA, TEXT, TEXT, INTEGER,
  TIMESTAMPTZ, TEXT, BOOLEAN, BOOLEAN, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB
) FROM PUBLIC;
