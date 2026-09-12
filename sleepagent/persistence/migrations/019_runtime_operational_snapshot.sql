-- sleepagent:transactional=true

-- G7.2: extend the existing protected, aggregate-only operator snapshot.
-- No subject, namespace, device, provider-request, payload, or free-form error
-- identifier is returned.  Thresholds are derived from durable schedule policy
-- where available; nonzero terminal/uncertain work is always unhealthy.
CREATE OR REPLACE FUNCTION public.sleepagent_internal_operational_metrics()
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
    RAISE EXCEPTION 'invalid internal operational metrics context';
  END IF;

  WITH work AS (
    SELECT 'ingestion'::TEXT AS queue_name, work.status AS state,
      work.available_at AS ready_at, work.lease_expires_at,
      work.lease_generation
    FROM public.sleep_domain_normalization_work AS work
    WHERE work.data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    )
    UNION ALL
    SELECT CASE
        WHEN operation.queue_name IN (
          'fast_path', 'product_agent', 'sleep_command',
          'product_interaction', 'demo_advance', 'induction',
          'demo_reset', 'reconciliation',
          'perceptor.history_overlap_pull',
          'perceptor.sleep_report_pull', 'night.finalization_scan'
        ) THEN operation.queue_name
        ELSE 'unknown'
      END,
      operation.status, operation.available_at, operation.lease_expires_at,
      operation.lease_generation
    FROM public.sleep_domain_operations AS operation
    WHERE operation.data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    )
    UNION ALL
    SELECT 'replay_journey', journey.phase, journey.resume_at,
      journey.lease_expires_at, journey.lease_generation
    FROM public.backend_demo_journeys AS journey
    WHERE journey.data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    )
    UNION ALL
    SELECT 'delivery', intent.status, intent.available_at,
      intent.lease_expires_at, intent.lease_generation
    FROM public.backend_delivery_intents AS intent
    WHERE intent.data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    )
    UNION ALL
    SELECT 'retention', job.status, job.available_at, job.lease_expires_at,
      job.lease_generation
    FROM public.backend_retention_jobs AS job
    WHERE job.data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    )
  ), queue_metrics AS (
    SELECT queue_name,
      count(*) FILTER (
        WHERE state IN (
          'pending', 'retry', 'accepted', 'staging_input',
          'waiting_normalization', 'waiting_episode', 'waiting_fast_path',
          'waiting_product', 'verifying_views'
        ) AND ready_at <= clock_timestamp()
      ) AS depth,
      COALESCE(max(GREATEST(0, floor(extract(epoch FROM
        (clock_timestamp() - ready_at))))) FILTER (
          WHERE state IN (
            'pending', 'retry', 'accepted', 'staging_input',
            'waiting_normalization', 'waiting_episode', 'waiting_fast_path',
            'waiting_product', 'verifying_views'
          ) AND ready_at <= clock_timestamp()
        ), 0) AS oldest_ready_age_seconds,
      count(*) FILTER (
        WHERE state IN ('running', 'dispatching')
          AND lease_expires_at > clock_timestamp()
      ) AS active_lease_count,
      count(*) FILTER (
        WHERE state IN ('running', 'dispatching')
          AND lease_expires_at <= clock_timestamp()
      ) AS lease_reclaimable_count,
      COALESCE(sum(GREATEST(lease_generation - 1, 0)), 0)
        AS lease_reclaim_count,
      count(*) FILTER (WHERE state = 'retry') AS retry_count,
      count(*) FILTER (
        WHERE state IN ('dead_letter', 'failed', 'blocked')
      ) AS dead_letter_count,
      count(*) FILTER (
        WHERE state IN ('outcome_unknown', 'reconciliation_required')
      ) AS outcome_unknown_count
    FROM work
    GROUP BY queue_name
  ), queue_totals AS (
    SELECT COALESCE(sum(depth), 0) AS depth,
      COALESCE(max(oldest_ready_age_seconds), 0) AS oldest_ready_age_seconds,
      COALESCE(sum(active_lease_count), 0) AS active_lease_count,
      COALESCE(sum(lease_reclaimable_count), 0) AS lease_reclaimable_count,
      COALESCE(sum(lease_reclaim_count), 0) AS lease_reclaim_count,
      COALESCE(sum(retry_count), 0) AS retry_count,
      COALESCE(sum(dead_letter_count), 0) AS dead_letter_count,
      COALESCE(sum(outcome_unknown_count), 0) AS outcome_unknown_count
    FROM queue_metrics
  ), scheduler AS (
    SELECT count(*) FILTER (WHERE enabled) AS enabled_schedule_count,
      count(*) FILTER (
        WHERE enabled AND next_run_at <= clock_timestamp()
      ) AS due_schedule_count,
      COALESCE(max(GREATEST(0, floor(extract(epoch FROM
        (clock_timestamp() - next_run_at))))) FILTER (
          WHERE enabled AND next_run_at <= clock_timestamp()
      ), 0) AS due_lag_seconds,
      max(last_fire_at) AS last_fire_at,
      max(last_success_at) AS last_success_at,
      COALESCE(sum(consecutive_failures), 0) AS failure_count,
      count(*) FILTER (WHERE consecutive_failures > 0)
        AS recent_failure_schedule_count,
      count(*) FILTER (
        WHERE enabled AND next_run_at <= clock_timestamp()
          - make_interval(secs => cadence_seconds)
      ) AS degraded_late_schedule_count,
      count(*) FILTER (
        WHERE enabled AND next_run_at <= clock_timestamp()
          - make_interval(secs => cadence_seconds * 2)
      ) AS unhealthy_late_schedule_count,
      count(*) FILTER (
        WHERE consecutive_failures >= max_attempts
      ) AS exhausted_schedule_count
    FROM public.backend_acquisition_schedules
    WHERE data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    )
  ), acquisition AS (
    SELECT
      max(receipt.occurred_at) FILTER (
        WHERE raw.raw_metadata_json ->> 'endpoint' =
          '/vitalSigns/getHistoryData'
      ) AS last_history_pull_success_at,
      max(receipt.occurred_at) FILTER (
        WHERE raw.raw_metadata_json ->> 'endpoint' =
          '/vitalSigns/getSleepReport'
      ) AS last_sleep_report_pull_success_at,
      COALESCE(sum(jsonb_array_length(COALESCE(
        receipt.receipt_json #> '{reconciliation_summary,canonical_observation_ids}',
        '[]'::JSONB
      ))), 0) AS records_received,
      COALESCE(sum((COALESCE(
        receipt.receipt_json #>> '{reconciliation_summary,canonical_created_count}',
        '0'
      ))::BIGINT), 0) AS records_persisted,
      COALESCE(sum((COALESCE(
        receipt.receipt_json #>> '{reconciliation_summary,duplicate_count}',
        '0'
      ))::BIGINT), 0) AS deduplicated_count,
      count(*) FILTER (
        WHERE COALESCE((receipt.receipt_json #>>
          '{reconciliation_summary,no_data}')::BOOLEAN, FALSE)
      ) AS no_data_reconciliation_count
    FROM public.sleep_domain_processing_receipts AS receipt
    JOIN public.sleep_domain_raw_inbox AS raw
      ON raw.raw_ingress_record_id = receipt.raw_ingress_record_id
     AND raw.namespace_id = receipt.namespace_id
     AND raw.data_mode = receipt.data_mode
    WHERE receipt.data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    )
      AND receipt.stage = 'normalization'
      AND receipt.outcome = 'succeeded'
      AND receipt.receipt_json ->> 'schema_version' =
        'perceptor_pull_reconciliation_receipt.v1'
  ), nights AS (
    SELECT COALESCE(max(GREATEST(0, floor(extract(epoch FROM
        (clock_timestamp() - updated_at))))) FILTER (
          WHERE state = 'open'
        ), 0) AS oldest_open_age_seconds,
      COALESCE(max(GREATEST(0, floor(extract(epoch FROM
        (clock_timestamp() - updated_at))))) FILTER (
          WHERE state = 'soft_finalized'
        ), 0) AS oldest_soft_finalized_age_seconds,
      count(*) FILTER (WHERE state = 'reconciliation_required')
        AS reconciliation_required_count
    FROM public.sleep_domain_night_finalizations
    WHERE data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    )
  ), late_revisions AS (
    SELECT count(*) AS late_finalization_revision_count
    FROM public.sleep_domain_night_finalization_revisions
    WHERE data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    ) AND revision_cause = 'late_material_evidence'
  ), report AS (
    SELECT
      max(updated_at) FILTER (
        WHERE operation_type = 'product.report.run.v1'
          AND status = 'succeeded'
      ) AS latest_report_completion_at,
      max(updated_at) FILTER (
        WHERE operation_type = 'product.shared_analysis.v1'
          AND status = 'succeeded'
      ) AS latest_shared_analysis_completion_at
    FROM public.sleep_domain_operations
    WHERE data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    )
  ), product_metrics AS (
    SELECT attempt_state AS outcome, count(*) AS count
    FROM public.backend_product_attempts
    WHERE data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    )
    GROUP BY attempt_state
  ), safety_metrics AS (
    SELECT CASE
        WHEN COALESCE(
          risk_json ->> 'risk_state', risk_json ->> 'state', 'unknown'
        ) IN (
          'unknown', 'no_reviewed_signal', 'operational_review',
          'reviewed_signal'
        ) THEN COALESCE(
          risk_json ->> 'risk_state', risk_json ->> 'state', 'unknown'
        )
        ELSE 'unknown'
      END AS outcome,
      count(*) AS count
    FROM public.sleep_domain_risk_assessments
    WHERE data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    )
    GROUP BY 1
  )
  SELECT jsonb_build_object(
    'schema_version', 'sleepagent_durable_operational_metrics.v2',
    'status', CASE
      WHEN queue_totals.dead_letter_count > 0
        OR queue_totals.outcome_unknown_count > 0
        OR nights.reconciliation_required_count > 0
        OR scheduler.exhausted_schedule_count > 0
        OR scheduler.unhealthy_late_schedule_count > 0
        THEN 'unhealthy'
      WHEN queue_totals.depth > 0 OR queue_totals.retry_count > 0
        OR scheduler.failure_count > 0
        OR scheduler.degraded_late_schedule_count > 0
        OR nights.oldest_open_age_seconds > 86400
        OR nights.oldest_soft_finalized_age_seconds > 86400
        THEN 'degraded'
      ELSE 'healthy'
    END,
    'scheduler', jsonb_build_object(
      'enabled_schedule_count', scheduler.enabled_schedule_count,
      'due_schedule_count', scheduler.due_schedule_count,
      'due_lag_seconds', scheduler.due_lag_seconds,
      'last_fire_at', scheduler.last_fire_at,
      'last_success_at', scheduler.last_success_at,
      'failure_count', scheduler.failure_count,
      'recent_failure_schedule_count',
        scheduler.recent_failure_schedule_count,
      'degraded_late_schedule_count',
        scheduler.degraded_late_schedule_count,
      'unhealthy_late_schedule_count',
        scheduler.unhealthy_late_schedule_count,
      'exhausted_schedule_count', scheduler.exhausted_schedule_count
    ),
    'acquisition', jsonb_build_object(
      'last_history_pull_success_at', acquisition.last_history_pull_success_at,
      'last_sleep_report_pull_success_at',
        acquisition.last_sleep_report_pull_success_at,
      'records_received', acquisition.records_received,
      'records_persisted', acquisition.records_persisted,
      'deduplicated_count', acquisition.deduplicated_count,
      'no_data_reconciliation_count',
        acquisition.no_data_reconciliation_count
    ),
    'queue_totals', to_jsonb(queue_totals),
    'queues', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'queue', queue_name,
        'depth', depth,
        'oldest_ready_age_seconds', oldest_ready_age_seconds,
        'active_lease_count', active_lease_count,
        'lease_reclaimable_count', lease_reclaimable_count,
        'lease_reclaim_count', lease_reclaim_count,
        'retry_count', retry_count,
        'dead_letter_count', dead_letter_count,
        'outcome_unknown_count', outcome_unknown_count
      ) ORDER BY queue_name)
      FROM queue_metrics
    ), '[]'::JSONB),
    'night_finalization', jsonb_build_object(
      'oldest_open_age_seconds', nights.oldest_open_age_seconds,
      'oldest_soft_finalized_age_seconds',
        nights.oldest_soft_finalized_age_seconds,
      'reconciliation_required_count',
        nights.reconciliation_required_count,
      'late_finalization_revision_count',
        late_revisions.late_finalization_revision_count
    ),
    'report_pipeline', jsonb_build_object(
      'latest_report_completion_at', report.latest_report_completion_at,
      'latest_shared_analysis_completion_at',
        report.latest_shared_analysis_completion_at
    ),
    'policy', jsonb_build_object(
      'scheduler_degraded_lag', 'one_configured_cadence',
      'scheduler_unhealthy_lag', 'two_configured_cadences',
      'schedule_failure_unhealthy', 'configured_max_attempts_exhausted',
      'night_age_degraded_seconds', 86400,
      'terminal_or_uncertain_work_unhealthy', TRUE
    ),
    'product_attempts', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'outcome', outcome, 'count', count
      ) ORDER BY outcome)
      FROM product_metrics
    ), '[]'::JSONB),
    'safety', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'outcome', outcome, 'count', count
      ) ORDER BY outcome)
      FROM safety_metrics
    ), '[]'::JSONB)
  ) INTO result
  FROM queue_totals, scheduler, acquisition, nights, late_revisions, report;
  RETURN result;
END;
$$;

REVOKE ALL ON FUNCTION public.sleepagent_internal_operational_metrics()
  FROM PUBLIC;

-- G7.2: the terminal replay fence accepts the current shared-only report
-- chain while retaining the explicit legacy rollback contract.  The report
-- operation, referenced shared analysis, exact night revision, and all three
-- public role projections are revalidated in the same success transaction.
CREATE OR REPLACE FUNCTION sleepagent_succeed_demo_journey(
  target_journey_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_result JSONB
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  journey_row public.backend_demo_journeys%ROWTYPE;
  receipt_payload JSONB;
  match_count INTEGER;
  product_policy_sha256 TEXT;
BEGIN
  IF requested_result IS NULL
     OR jsonb_typeof(requested_result) <> 'object'
     OR NOT (requested_result ?& ARRAY[
       'night_episode_id', 'night_episode_revision_id',
       'fast_path_operation_id', 'product_operation_id',
       'analysis_revision_id', 'role_projection_ids', 'manifest_sha256'
     ])
     OR jsonb_typeof(requested_result -> 'role_projection_ids') <> 'object'
     OR (SELECT COALESCE(
           array_agg(role_name ORDER BY role_name), ARRAY[]::text[]
         )
         FROM jsonb_object_keys(
           requested_result -> 'role_projection_ids'
         ) AS roles(role_name))
       <> ARRAY['doctor', 'elder', 'family']::text[] THEN
    RAISE EXCEPTION 'role_projection_incomplete';
  END IF;

  SELECT journey.*
  INTO journey_row
  FROM public.backend_demo_journeys AS journey
  WHERE journey.journey_id = target_journey_id
    AND journey.phase = 'verifying_views'
    AND journey.lease_generation = expected_lease_generation
    AND journey.fencing_token = expected_fencing_token
    AND journey.worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND journey.lease_expires_at > clock_timestamp()
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      journey.namespace_id, journey.data_mode, journey.subject_id,
      journey.namespace_generation, journey.run_id, journey.arm_id
    )
  FOR UPDATE;
  IF NOT FOUND THEN
    RETURN FALSE;
  END IF;
  IF requested_result ->> 'manifest_sha256' <>
       journey_row.manifest_sha256 THEN
    RAISE EXCEPTION 'scenario_contract_invalid';
  END IF;

  SELECT count(*) INTO match_count
  FROM public.sleep_domain_night_episodes AS episode
  JOIN public.sleep_domain_night_episode_revisions AS revision
    ON revision.night_episode_revision_id = episode.current_revision_id
   AND revision.namespace_id = episode.namespace_id
   AND revision.data_mode = episode.data_mode
  WHERE episode.night_episode_id = requested_result ->> 'night_episode_id'
    AND episode.current_revision_id =
      requested_result ->> 'night_episode_revision_id'
    AND episode.namespace_id = journey_row.namespace_id
    AND episode.data_mode = journey_row.data_mode
    AND episode.namespace_generation = journey_row.namespace_generation
    AND episode.run_id = journey_row.run_id
    AND episode.arm_id = journey_row.arm_id
    AND episode.subject_id = journey_row.subject_id
    AND episode.protocol_version >= 2
    AND episode.date_state = 'finalized'
    AND episode.date_conflict = FALSE
    AND episode.assignment_basis = 'observed_wake'
    AND EXISTS (
      SELECT 1
      FROM public.sleep_domain_episode_observation_memberships AS member
      WHERE member.namespace_id = episode.namespace_id
        AND member.data_mode = episode.data_mode
        AND member.night_episode_id = episode.night_episode_id
    )
    AND (
      SELECT COALESCE(array_agg(member.observation_id
        ORDER BY member.observation_id), ARRAY[]::text[])
      FROM public.sleep_domain_episode_observation_memberships AS member
      WHERE member.namespace_id = episode.namespace_id
        AND member.data_mode = episode.data_mode
        AND member.night_episode_id = episode.night_episode_id
    ) = (
      SELECT COALESCE(array_agg(value ORDER BY value), ARRAY[]::text[])
      FROM jsonb_array_elements_text(
        revision.revision_json -> 'observation_ids'
      ) AS ids(value)
    );
  IF match_count <> 1 THEN
    RAISE EXCEPTION 'episode_not_committed';
  END IF;

  SELECT count(*) INTO match_count
  FROM public.sleep_domain_operations AS operation
  WHERE operation.operation_id =
      requested_result ->> 'fast_path_operation_id'
    AND operation.namespace_id = journey_row.namespace_id
    AND operation.data_mode = journey_row.data_mode
    AND operation.namespace_generation = journey_row.namespace_generation
    AND operation.run_id = journey_row.run_id
    AND operation.arm_id = journey_row.arm_id
    AND operation.subject_id = journey_row.subject_id
    AND operation.operation_type = 'fast_path'
    AND operation.target_resource_key =
      requested_result ->> 'night_episode_revision_id'
    AND operation.status = 'succeeded';
  IF match_count <> 1 THEN
    RAISE EXCEPTION 'episode_not_committed';
  END IF;

  SELECT count(*), max(candidate.policy_sha256)
  INTO match_count, product_policy_sha256
  FROM (
    SELECT operation.policy_sha256
    FROM public.sleep_domain_operations AS operation
    WHERE operation.operation_id =
        requested_result ->> 'product_operation_id'
      AND operation.namespace_id = journey_row.namespace_id
      AND operation.data_mode = journey_row.data_mode
      AND operation.namespace_generation = journey_row.namespace_generation
      AND operation.run_id = journey_row.run_id
      AND operation.arm_id = journey_row.arm_id
      AND operation.subject_id = journey_row.subject_id
      AND operation.operation_type = 'product_agent'
      AND operation.target_resource_key =
        requested_result ->> 'night_episode_revision_id'
      AND operation.status = 'succeeded'
      AND operation.operation_json #>> '{result,analysis_revision_id}' =
        requested_result ->> 'analysis_revision_id'
      AND operation.operation_json #>> '{result,night_episode_revision_id}' =
        requested_result ->> 'night_episode_revision_id'
      AND jsonb_array_length(
        operation.operation_json #> '{result,role_view_ids}'
      ) = 3
    UNION ALL
    SELECT shared_operation.policy_sha256
    FROM public.sleep_domain_operations AS report_operation
    JOIN public.sleep_domain_operations AS shared_operation
      ON shared_operation.operation_id =
        report_operation.operation_json #>>
          '{report_result,shared_operation_id}'
     AND shared_operation.namespace_id = report_operation.namespace_id
     AND shared_operation.data_mode = report_operation.data_mode
     AND shared_operation.namespace_generation =
       report_operation.namespace_generation
     AND shared_operation.run_id = report_operation.run_id
     AND shared_operation.arm_id = report_operation.arm_id
     AND shared_operation.subject_id = report_operation.subject_id
    WHERE report_operation.operation_id =
        requested_result ->> 'product_operation_id'
      AND report_operation.namespace_id = journey_row.namespace_id
      AND report_operation.data_mode = journey_row.data_mode
      AND report_operation.namespace_generation =
        journey_row.namespace_generation
      AND report_operation.run_id = journey_row.run_id
      AND report_operation.arm_id = journey_row.arm_id
      AND report_operation.subject_id = journey_row.subject_id
      AND report_operation.operation_type = 'product.report.run.v1'
      AND report_operation.target_resource_key =
        requested_result ->> 'night_episode_revision_id'
      AND report_operation.status = 'succeeded'
      AND shared_operation.operation_type =
        'product.shared_analysis.v1'
      AND shared_operation.status = 'succeeded'
      AND shared_operation.operation_json #>>
        '{result,analysis_revision_id}' =
          requested_result ->> 'analysis_revision_id'
      AND shared_operation.operation_json #>>
        '{result,night_episode_revision_id}' =
          requested_result ->> 'night_episode_revision_id'
      AND jsonb_array_length(
        shared_operation.operation_json #> '{result,role_view_ids}'
      ) = 3
  ) AS candidate;
  IF match_count <> 1 THEN
    RAISE EXCEPTION 'product_failed';
  END IF;

  SELECT count(*) INTO match_count
  FROM public.sleep_domain_analysis_revisions AS analysis
  WHERE analysis.namespace_id = journey_row.namespace_id
    AND analysis.data_mode = journey_row.data_mode
    AND analysis.subject_id = journey_row.subject_id
    AND analysis.night_episode_id =
      requested_result ->> 'night_episode_id'
    AND analysis.night_episode_revision_id =
      requested_result ->> 'night_episode_revision_id';
  IF match_count <> 1 THEN
    RAISE EXCEPTION 'product_failed';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM public.sleep_domain_analysis_revisions AS analysis
    WHERE analysis.analysis_revision_id =
        requested_result ->> 'analysis_revision_id'
      AND analysis.namespace_id = journey_row.namespace_id
      AND analysis.data_mode = journey_row.data_mode
      AND analysis.subject_id = journey_row.subject_id
      AND analysis.night_episode_id =
        requested_result ->> 'night_episode_id'
      AND analysis.night_episode_revision_id =
        requested_result ->> 'night_episode_revision_id'
  ) THEN
    RAISE EXCEPTION 'product_failed';
  END IF;

  SELECT count(*) INTO match_count
  FROM public.sleep_domain_analysis_role_views AS view
  JOIN public.backend_subject_epochs AS epoch_row
    ON epoch_row.namespace_id = view.namespace_id
   AND epoch_row.data_mode = view.data_mode
   AND epoch_row.subject_id = view.subject_id
  WHERE view.analysis_revision_id =
      requested_result ->> 'analysis_revision_id'
    AND view.namespace_id = journey_row.namespace_id
    AND view.data_mode = journey_row.data_mode
    AND view.namespace_generation = journey_row.namespace_generation
    AND view.run_id = journey_row.run_id
    AND view.arm_id = journey_row.arm_id
    AND view.subject_id = journey_row.subject_id
    AND view.night_episode_id =
      requested_result ->> 'night_episode_id'
    AND view.night_episode_revision_id =
      requested_result ->> 'night_episode_revision_id'
    AND view.role IN ('elder', 'family', 'doctor')
    AND view.role_view_id =
      requested_result -> 'role_projection_ids' ->> view.role
    AND view.status = 'ready'
    AND view.protocol_version >= 2
    AND view.policy_sha256 = product_policy_sha256
    AND view.authorization_epoch = epoch_row.authorization_epoch
    AND view.privacy_epoch = epoch_row.privacy_epoch
    AND view.retrieval_policy_epoch = epoch_row.retrieval_policy_epoch
    AND view.public_schema_version = 'product_sleep_today.v1'
    AND view.public_today_json ->> 'role' = view.role
    AND view.public_today_json ->> 'analysis_revision_id' =
      view.analysis_revision_id
    AND view.public_projection_sha256 ~ '^[0-9a-f]{64}$';
  IF match_count <> 3 THEN
    RAISE EXCEPTION 'role_projection_incomplete';
  END IF;

  receipt_payload := jsonb_build_object(
    'schema_version', 'demo_journey_terminal_receipt.v1',
    'journey_id', journey_row.journey_id,
    'root_operation_id', journey_row.root_operation_id,
    'outcome', 'succeeded',
    'result', requested_result,
    'manifest_sha256', journey_row.manifest_sha256,
    'data_mode', 'replay',
    'synthetic_non_release', TRUE
  );
  INSERT INTO public.backend_demo_journey_receipts (
    receipt_id, journey_id, root_operation_id, namespace_id, data_mode,
    namespace_generation, run_id, arm_id, subject_id, outcome,
    receipt_sha256, receipt_json, error_code
  ) VALUES (
    gen_random_uuid()::text, journey_row.journey_id,
    journey_row.root_operation_id, journey_row.namespace_id, 'replay',
    journey_row.namespace_generation, journey_row.run_id,
    journey_row.arm_id, journey_row.subject_id, 'succeeded',
    encode(digest(receipt_payload::text, 'sha256'), 'hex'),
    receipt_payload, NULL
  );

  UPDATE public.backend_demo_journeys
  SET phase = 'succeeded', result_json = requested_result,
      terminal_at = clock_timestamp(), worker_instance = NULL,
      fencing_token = NULL, lease_expires_at = NULL,
      heartbeat_at = NULL, version = version + 1,
      updated_at = clock_timestamp()
  WHERE journey_id = journey_row.journey_id
    AND lease_generation = journey_row.lease_generation
    AND fencing_token = journey_row.fencing_token;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'journey_fence_lost';
  END IF;

  UPDATE public.sleep_domain_operations
  SET status = 'succeeded', outcome_class = 'succeeded',
      operation_json = operation_json || jsonb_build_object(
        'result', requested_result,
        'terminal_at', clock_timestamp()
      ),
      cas_version = cas_version + 1,
      lease_owner = NULL, lease_expires_at = NULL,
      fencing_token = NULL, worker_instance = NULL,
      heartbeat_at = NULL, updated_at = clock_timestamp()
  WHERE operation_id = journey_row.root_operation_id
    AND status NOT IN ('succeeded', 'failed', 'outcome_unknown');
  IF NOT FOUND THEN
    RAISE EXCEPTION 'root_operation_terminal_conflict';
  END IF;

  INSERT INTO public.backend_demo_journey_events (
    event_id, journey_id, namespace_id, data_mode,
    namespace_generation, run_id, arm_id, subject_id,
    event_type, state, correlation_id, event_json
  ) VALUES (
    gen_random_uuid()::text, journey_row.journey_id,
    journey_row.namespace_id, 'replay', journey_row.namespace_generation,
    journey_row.run_id, journey_row.arm_id, journey_row.subject_id,
    'journey.succeeded', 'succeeded', journey_row.root_operation_id,
    receipt_payload
  );
  INSERT INTO public.sleep_domain_domain_outbox (
    event_id, namespace_id, data_mode, event_type,
    aggregate_type, aggregate_id, aggregate_version,
    per_aggregate_sequence, subject_id, operation_id,
    status, available_at, event_json, created_at,
    protocol_version, namespace_generation, run_id, arm_id
  ) VALUES (
    'outbox:' || encode(digest(
      journey_row.journey_id || E'\x1fjourney.terminal', 'sha256'
    ), 'hex'),
    journey_row.namespace_id, 'replay', 'DEMO_JOURNEY_TERMINAL',
    'ReplayJourney', journey_row.journey_id, journey_row.version + 1, 2,
    journey_row.subject_id, journey_row.root_operation_id,
    'committed', clock_timestamp(), receipt_payload, clock_timestamp(),
    2, journey_row.namespace_generation, journey_row.run_id,
    journey_row.arm_id
  );
  INSERT INTO public.backend_authorization_audit (
    audit_id, namespace_id, data_mode, subject_id,
    principal_id, actor_id, binding_id, decision,
    reason_code, policy_sha256, authorization_epoch,
    privacy_epoch, retrieval_policy_epoch, audit_json, occurred_at
  ) VALUES (
    'audit:' || encode(digest(
      journey_row.root_operation_id || E'\x1fdemo_journey_terminal',
      'sha256'
    ), 'hex'),
    journey_row.namespace_id, 'replay', journey_row.subject_id,
    NULLIF(current_setting('sleepagent.service_principal_id', TRUE), ''),
    NULL, NULL, 'allow', 'demo_journey_terminal',
    journey_row.policy_sha256,
    current_setting('sleepagent.authorization_epoch')::bigint,
    current_setting('sleepagent.privacy_epoch')::bigint,
    current_setting('sleepagent.retrieval_policy_epoch')::bigint,
    receipt_payload, clock_timestamp()
  );
  RETURN TRUE;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_succeed_demo_journey(
  TEXT, BIGINT, TEXT, JSONB
) FROM PUBLIC;
