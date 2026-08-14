-- sleepagent:transactional=true
-- P3 adds one read-only, replay-only projection over existing durable records.
-- It does not create a second tracing authority or expose model prompts/secrets.

CREATE OR REPLACE FUNCTION sleepagent_read_demo_technical_trace(
  target_root_operation_id TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  journey_row public.backend_demo_journeys%ROWTYPE;
  attempt_rows JSONB;
  invocation_rows JSONB;
  habit_rows JSONB;
  memory_rows JSONB;
  receipt_rows JSONB;
  product_operation_count BIGINT;
  product_attempt_count BIGINT;
  fast_path_succeeded_count BIGINT;
BEGIN
  IF NOT public.sleepagent_demo_workload_allows()
     OR target_root_operation_id IS NULL
     OR target_root_operation_id = '' THEN
    RAISE EXCEPTION 'authorization_denied';
  END IF;

  SELECT journey.*
  INTO journey_row
  FROM public.backend_demo_journeys AS journey
  JOIN public.sleep_domain_operations AS operation
    ON operation.operation_id = journey.root_operation_id
  WHERE journey.root_operation_id = target_root_operation_id
    AND operation.service_principal_id = NULLIF(
      current_setting('sleepagent.service_principal_id', TRUE), ''
    );
  IF NOT FOUND THEN
    RAISE EXCEPTION 'operation_not_found';
  END IF;

  SELECT COALESCE(jsonb_agg(
    jsonb_build_object(
      'product_attempt_id', attempt.product_attempt_id,
      'operation_id', attempt.operation_id,
      'attempt_state', attempt.attempt_state,
      'attempt_sha256', attempt.attempt_sha256,
      'prepared_at', attempt.prepared_at,
      'committed_at', attempt.committed_at,
      'analysis', attempt.attempt_json -> 'analysis',
      'role_runs', COALESCE((
        SELECT jsonb_agg(
          jsonb_build_object(
            'role', role_run.value ->> 'role',
            'product_episode_id', role_run.value ->> 'product_episode_id',
            'fact_snapshot', jsonb_build_object(
              'fact_snapshot_id', role_run.value #>> '{fact_snapshot,fact_snapshot_id}',
              'fact_snapshot_hash', role_run.value #>> '{fact_snapshot,fact_snapshot_hash}',
              'source_refs', role_run.value #> '{fact_snapshot,source_refs}',
              'habit_profile_version', role_run.value #> '{fact_snapshot,habit_profile_version}',
              'habit_profile_hash', role_run.value #> '{fact_snapshot,habit_profile_hash}',
              'memory_read_receipt_refs', role_run.value #> '{fact_snapshot,memory_read_receipt_refs}'
            ),
            'personalization', role_run.value -> 'personalization',
            'receipt', role_run.value #> '{result,receipt}',
            'publication', role_run.value #> '{result,publication}',
            'agent_invocations', role_run.value #> '{result,agent_invocations}',
            'tool_receipts', role_run.value #> '{result,tool_receipts}',
            'accepted_work_products', role_run.value #> '{result,accepted_work_products}',
            'role_view', role_run.value -> 'role_view'
          ) ORDER BY role_run.ordinality
        )
        FROM jsonb_array_elements(attempt.attempt_json -> 'role_runs')
          WITH ORDINALITY AS role_run(value, ordinality)
      ), '[]'::jsonb)
    ) ORDER BY attempt.committed_at, attempt.product_attempt_id
  ), '[]'::jsonb)
  INTO attempt_rows
  FROM public.backend_product_attempts AS attempt
  WHERE attempt.namespace_id = journey_row.namespace_id
    AND attempt.data_mode = journey_row.data_mode
    AND attempt.namespace_generation = journey_row.namespace_generation
    AND attempt.run_id = journey_row.run_id
    AND attempt.arm_id = journey_row.arm_id
    AND attempt.subject_id = journey_row.subject_id
    AND attempt.query_visible;

  SELECT COALESCE(jsonb_agg(jsonb_build_object(
    'invocation_id', invocation.invocation_id,
    'operation_id', invocation.operation_id,
    'invocation_kind', invocation.invocation_kind,
    'invocation_key', invocation.invocation_key,
    'request_sha256', invocation.request_sha256,
    'response_sha256', invocation.response_sha256,
    'provider_request_id', invocation.provider_request_id,
    'state', invocation.current_state,
    'reserved_at', invocation.reserved_at,
    'updated_at', invocation.updated_at
  ) ORDER BY invocation.reserved_at, invocation.invocation_id), '[]'::jsonb)
  INTO invocation_rows
  FROM public.backend_invocations AS invocation
  WHERE invocation.namespace_id = journey_row.namespace_id
    AND invocation.data_mode = journey_row.data_mode
    AND invocation.namespace_generation = journey_row.namespace_generation
    AND invocation.run_id = journey_row.run_id
    AND invocation.arm_id = journey_row.arm_id
    AND invocation.subject_id = journey_row.subject_id;

  SELECT COALESCE(jsonb_agg(jsonb_build_object(
    'profile_version', revision.profile_version,
    'fact_id', revision.fact_id,
    'concept_id', revision.concept_id,
    'operation', revision.operation,
    'fact_hash', revision.fact_sha256,
    'value', revision.fact_json -> 'value',
    'origin', revision.fact_json #> '{evidence,origin}',
    'source_actor_role', revision.fact_json #> '{evidence,role}',
    'confirmation_ref', revision.confirmation_ref,
    'committed_at', revision.committed_at
  ) ORDER BY revision.profile_version), '[]'::jsonb)
  INTO habit_rows
  FROM public.backend_habit_profile_revisions_v2 AS revision
  WHERE revision.namespace_id = journey_row.namespace_id
    AND revision.data_mode = journey_row.data_mode
    AND revision.namespace_generation = journey_row.namespace_generation
    AND revision.run_id = journey_row.run_id
    AND revision.arm_id = journey_row.arm_id
    AND revision.subject_id = journey_row.subject_id;

  SELECT COALESCE(jsonb_agg(jsonb_build_object(
    'state_version', revision.state_version,
    'revision_ref', revision.revision_ref,
    'memory_id', revision.memory_id,
    'concept_id', revision.concept_id,
    'status', revision.status,
    'revision_hash', revision.revision_sha256,
    'typed_value', revision.revision_json -> 'typed_value',
    'confirmation_ref', revision.confirmation_ref,
    'committed_at', revision.committed_at
  ) ORDER BY revision.state_version), '[]'::jsonb)
  INTO memory_rows
  FROM public.backend_governed_memory_revisions_v2 AS revision
  WHERE revision.namespace_id = journey_row.namespace_id
    AND revision.data_mode = journey_row.data_mode
    AND revision.namespace_generation = journey_row.namespace_generation
    AND revision.run_id = journey_row.run_id
    AND revision.arm_id = journey_row.arm_id
    AND revision.subject_id = journey_row.subject_id;

  SELECT COALESCE(jsonb_agg(jsonb_build_object(
    'receipt_id', receipt.receipt_id,
    'product_episode_id', receipt.product_episode_id,
    'requesting_agent', receipt.requesting_agent,
    'purpose', receipt.purpose,
    'query_hash', receipt.query_sha256,
    'result_hash', receipt.result_sha256,
    'receipt_hash', receipt.receipt_sha256,
    'items', receipt.receipt_json -> 'items',
    'completed_at', receipt.completed_at
  ) ORDER BY receipt.completed_at, receipt.receipt_id), '[]'::jsonb)
  INTO receipt_rows
  FROM public.backend_memory_read_receipts_v2 AS receipt
  WHERE receipt.namespace_id = journey_row.namespace_id
    AND receipt.data_mode = journey_row.data_mode
    AND receipt.namespace_generation = journey_row.namespace_generation
    AND receipt.run_id = journey_row.run_id
    AND receipt.arm_id = journey_row.arm_id
    AND receipt.subject_id = journey_row.subject_id;

  SELECT count(*) FILTER (WHERE operation.operation_type = 'product_agent'),
         count(*) FILTER (
           WHERE operation.operation_type = 'fast_path'
             AND operation.status = 'succeeded'
         )
  INTO product_operation_count, fast_path_succeeded_count
  FROM public.sleep_domain_operations AS operation
  WHERE operation.namespace_id = journey_row.namespace_id
    AND operation.data_mode = journey_row.data_mode
    AND operation.namespace_generation = journey_row.namespace_generation
    AND operation.run_id = journey_row.run_id
    AND operation.arm_id = journey_row.arm_id
    AND operation.subject_id = journey_row.subject_id;

  SELECT count(*) INTO product_attempt_count
  FROM public.backend_product_attempts AS attempt
  WHERE attempt.namespace_id = journey_row.namespace_id
    AND attempt.data_mode = journey_row.data_mode
    AND attempt.namespace_generation = journey_row.namespace_generation
    AND attempt.run_id = journey_row.run_id
    AND attempt.arm_id = journey_row.arm_id
    AND attempt.subject_id = journey_row.subject_id;

  RETURN jsonb_build_object(
    'schema_version', 'demo_technical_trace.v1',
    'data_mode', 'replay',
    'synthetic_non_release', TRUE,
    'root_operation_id', journey_row.root_operation_id,
    'namespace_generation', journey_row.namespace_generation,
    'run_id', journey_row.run_id,
    'arm_id', journey_row.arm_id,
    'subject_id', journey_row.subject_id,
    'journey_state', journey_row.phase,
    'journey_error_code', journey_row.error_code,
    'journey_result', journey_row.result_json,
    'product_operation_count', product_operation_count,
    'product_attempt_count', product_attempt_count,
    'fast_path_succeeded_count', fast_path_succeeded_count,
    'product_attempts', attempt_rows,
    'durable_invocations', invocation_rows,
    'habit_revisions', habit_rows,
    'memory_revisions', memory_rows,
    'memory_read_receipts', receipt_rows
  );
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_read_demo_technical_trace(TEXT) FROM PUBLIC;
