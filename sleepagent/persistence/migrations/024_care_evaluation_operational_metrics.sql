-- sleepagent:transactional=true

-- C2: aggregate-only visibility for the durable HARD-qualified Care evaluator.
-- The generic product queue already drives overall health; this projection
-- distinguishes the C2 operation without returning source or subject identity.
CREATE OR REPLACE FUNCTION public.sleepagent_care_evaluation_operational_metrics_v1()
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  result JSONB;
BEGIN
  IF NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'api'
     OR NULLIF(current_setting('sleepagent.purpose', TRUE), '') <>
       'internal_status'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid care evaluation metrics context';
  END IF;

  SELECT jsonb_build_object(
    'schema_version', 'care_evaluation_operational_metrics.v1',
    'pending_care_evaluations', count(*) FILTER (
      WHERE status IN ('pending', 'retry')
    ),
    'oldest_pending_care_evaluation_age_seconds', COALESCE(
      extract(epoch FROM (
        clock_timestamp() - min(created_at) FILTER (
          WHERE status IN ('pending', 'retry')
        )
      ))::BIGINT,
      0
    ),
    'care_evaluations_succeeded', count(*) FILTER (
      WHERE status = 'succeeded'
    ),
    'care_evaluations_deduplicated', COALESCE(sum(
      (operation_json ->> 'deduplicated_trigger_count')::BIGINT
    ), 0),
    'care_evaluations_failed_or_conflicted', count(*) FILTER (
      WHERE status IN (
        'failed', 'dead_letter', 'blocked', 'outcome_unknown',
        'reconciliation_required'
      ) OR outcome_class = 'conflicted'
    )
  ) INTO result
  FROM public.sleep_domain_operations
  WHERE data_mode = NULLIF(current_setting('sleepagent.data_mode', TRUE), '')
    AND operation_type = 'care.evaluate.on_hard.v1';
  RETURN result;
END;
$$;

REVOKE ALL ON FUNCTION
  public.sleepagent_care_evaluation_operational_metrics_v1()
  FROM PUBLIC;
