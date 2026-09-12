-- sleepagent:transactional=true

-- A repeated scheduler interval that is already covered by the durable
-- history cursor is a successful no-op.  Keep the existing function identity
-- and five-column least-privilege timestamp contract so deployed grants remain
-- valid.  A no-op is encoded as window_end_at = checkpoint_cursor_at; an
-- advancing plan continues to end at the caller's requested bound.
CREATE OR REPLACE FUNCTION sleepagent_plan_perceptor_history(
  target_namespace_id TEXT,
  target_namespace_generation BIGINT,
  target_provider_account_id TEXT,
  target_device_binding_id TEXT,
  target_binding_version INTEGER,
  target_subject_id TEXT,
  requested_window_start TIMESTAMPTZ,
  requested_window_end TIMESTAMPTZ
)
RETURNS TABLE (
  window_start_at TIMESTAMPTZ,
  window_end_at TIMESTAMPTZ,
  checkpoint_cursor_at TIMESTAMPTZ,
  lateness_watermark_at TIMESTAMPTZ,
  resumed_from_checkpoint BOOLEAN
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  caller_matches INTEGER;
  generation_matches INTEGER;
  account_matches INTEGER;
  binding_matches INTEGER;
  checkpoint_matches INTEGER;
  resolved_protocol_version INTEGER;
  resolved_overlap_seconds INTEGER;
  resolved_checkpoint_cursor TIMESTAMPTZ;
  resolved_lateness_watermark TIMESTAMPTZ;
  resolved_window_start TIMESTAMPTZ;
  resolved_window_end TIMESTAMPTZ;
BEGIN
  IF NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'api'
     OR NULLIF(current_setting('sleepagent.purpose', TRUE), '') <>
       'perceptor_ingress'
     OR NULLIF(current_setting('sleepagent.data_mode', TRUE), '') <> 'live'
     OR NULLIF(current_setting('sleepagent.authorization_epoch', TRUE), '')
       !~ '^[0-9]+$'
     OR target_namespace_id IS NULL
     OR target_namespace_generation IS NULL
     OR target_namespace_generation < 1
     OR NULLIF(target_provider_account_id, '') IS NULL
     OR NULLIF(target_device_binding_id, '') IS NULL
     OR target_binding_version IS NULL
     OR target_binding_version < 1
     OR NULLIF(target_subject_id, '') IS NULL
     OR requested_window_start IS NULL
     OR requested_window_end IS NULL
     OR date_trunc('second', requested_window_start) <>
       requested_window_start
     OR date_trunc('second', requested_window_end) <> requested_window_end
     OR requested_window_end <= requested_window_start
     OR requested_window_end - requested_window_start > INTERVAL '1 hour'
     OR target_namespace_id <> NULLIF(
       current_setting('sleepagent.namespace_id', TRUE), ''
     )
     OR target_namespace_generation::TEXT <> NULLIF(
       current_setting('sleepagent.namespace_generation', TRUE), ''
     )
     OR NOT public.sleepagent_namespace_scope_allows(
       target_namespace_id, 'live'
     ) THEN
    RAISE EXCEPTION 'invalid Perceptor history planning context';
  END IF;

  SELECT count(*) INTO caller_matches
  FROM public.backend_service_principals AS principal
  JOIN public.backend_principal_grants AS grant_row
    ON grant_row.principal_id = principal.principal_id
   AND grant_row.namespace_id = target_namespace_id
   AND grant_row.data_mode = 'live'
   AND grant_row.purpose = 'perceptor_ingress'
   AND grant_row.authorization_epoch =
     current_setting('sleepagent.authorization_epoch', TRUE)::BIGINT
   AND grant_row.status = 'active'
   AND grant_row.valid_from <= clock_timestamp()
   AND (
     grant_row.valid_until IS NULL
     OR grant_row.valid_until > clock_timestamp()
   )
  WHERE principal.principal_id = NULLIF(
      current_setting('sleepagent.service_principal_id', TRUE), ''
    )
    AND principal.database_role_name::TEXT = session_user::TEXT
    AND principal.principal_kind IN ('bff', 'external_service')
    AND principal.status = 'active';
  IF caller_matches <> 1 THEN
    RAISE EXCEPTION 'Perceptor history planner authority mismatch';
  END IF;

  SELECT count(*) INTO generation_matches
  FROM public.backend_namespaces AS namespace_row
  JOIN public.backend_namespace_generations AS generation_row
    ON generation_row.namespace_id = namespace_row.namespace_id
   AND generation_row.data_mode = namespace_row.data_mode
   AND generation_row.generation = namespace_row.current_generation
  WHERE namespace_row.namespace_id = target_namespace_id
    AND namespace_row.data_mode = 'live'
    AND namespace_row.current_generation = target_namespace_generation
    AND namespace_row.status = 'active'
    AND generation_row.status = 'active';
  IF generation_matches <> 1 THEN
    RAISE EXCEPTION 'Perceptor history planner generation mismatch';
  END IF;

  SELECT count(*) INTO account_matches
  FROM public.sleep_domain_provider_accounts AS account
  WHERE account.namespace_id = target_namespace_id
    AND account.data_mode = 'live'
    AND account.provider_account_id = target_provider_account_id
    AND account.provider_id = 'perceptor'
    AND account.status = 'active';
  IF account_matches <> 1 THEN
    RAISE EXCEPTION 'Perceptor history planner account mismatch';
  END IF;

  SELECT count(*), min(checkpoint.protocol_version),
         min(checkpoint.overlap_seconds), min(checkpoint.cursor_at),
         min(checkpoint.lateness_watermark_at)
  INTO checkpoint_matches, resolved_protocol_version,
       resolved_overlap_seconds, resolved_checkpoint_cursor,
       resolved_lateness_watermark
  FROM public.sleep_domain_pull_checkpoints AS checkpoint
  WHERE checkpoint.namespace_id = target_namespace_id
    AND checkpoint.data_mode = 'live'
    AND checkpoint.namespace_generation = target_namespace_generation
    AND checkpoint.run_id IS NULL
    AND checkpoint.arm_id IS NULL
    AND checkpoint.provider_id = 'perceptor'
    AND checkpoint.provider_account_id = target_provider_account_id
    AND checkpoint.subject_id = target_subject_id
    AND checkpoint.device_binding_id = target_device_binding_id
    AND checkpoint.binding_version = target_binding_version
    AND checkpoint.data_surface = 'history';
  IF checkpoint_matches > 1 THEN
    RAISE EXCEPTION 'ambiguous Perceptor history checkpoint';
  ELSIF checkpoint_matches = 1 THEN
    IF resolved_protocol_version < 2
       OR resolved_overlap_seconds <> 3
       OR resolved_checkpoint_cursor IS NULL
       OR resolved_lateness_watermark IS DISTINCT FROM
         resolved_checkpoint_cursor - INTERVAL '3 seconds' THEN
      RAISE EXCEPTION 'invalid durable Perceptor history checkpoint';
    END IF;
    resolved_window_start := resolved_lateness_watermark;
    IF requested_window_end <= resolved_checkpoint_cursor THEN
      resolved_window_end := resolved_checkpoint_cursor;
    ELSE
      resolved_window_end := requested_window_end;
    END IF;
  ELSE
    resolved_window_start := requested_window_start;
    resolved_window_end := requested_window_end;
  END IF;

  IF resolved_window_end <= resolved_window_start
     OR resolved_window_end - resolved_window_start > INTERVAL '1 hour' THEN
    RAISE EXCEPTION 'Perceptor history continuation exceeds one hour';
  END IF;

  SELECT count(*) INTO binding_matches
  FROM public.sleep_domain_device_bindings AS binding
  JOIN public.backend_subjects AS subject_row
    ON subject_row.namespace_id = binding.namespace_id
   AND subject_row.data_mode = binding.data_mode
   AND subject_row.subject_id = binding.subject_id
   AND subject_row.status = 'active'
  WHERE binding.device_binding_id = target_device_binding_id
    AND binding.namespace_id = target_namespace_id
    AND binding.data_mode = 'live'
    AND binding.binding_version = target_binding_version
    AND binding.subject_id = target_subject_id
    AND binding.provider_id = 'perceptor'
    AND binding.provider_account_id = target_provider_account_id
    AND binding.status = 'active'
    AND binding.effective_from <= resolved_window_start
    AND (
      binding.effective_until IS NULL
      OR binding.effective_until > resolved_window_end
    );
  IF binding_matches <> 1 THEN
    RAISE EXCEPTION 'Perceptor history planner binding mismatch';
  END IF;

  RETURN QUERY SELECT
    resolved_window_start,
    resolved_window_end,
    CASE WHEN checkpoint_matches = 1
      THEN resolved_checkpoint_cursor ELSE NULL::TIMESTAMPTZ END,
    CASE WHEN checkpoint_matches = 1
      THEN resolved_lateness_watermark ELSE NULL::TIMESTAMPTZ END,
    checkpoint_matches = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_plan_perceptor_history(
  TEXT, BIGINT, TEXT, TEXT, INTEGER, TEXT, TIMESTAMPTZ, TIMESTAMPTZ
) FROM PUBLIC;
