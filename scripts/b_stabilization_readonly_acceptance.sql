\set ON_ERROR_STOP on
\pset pager off
BEGIN READ ONLY;
SET LOCAL statement_timeout = '8s';
\o /dev/null
SELECT set_config('sleepagent.namespace_id', 'live:p4e1-r2-20260824', true);
SELECT set_config('sleepagent.data_mode', 'live', true);
SELECT set_config('sleepagent.namespace_generation', '1', true);
SELECT set_config('sleepagent.run_id', '', true);
SELECT set_config('sleepagent.arm_id', '', true);
SELECT set_config('sleepagent.subject_id', 'p4d-real-acceptance-subject', true);
SELECT set_config('sleepagent.actor_id', 'p4e1-r2-product-cli-elder', true);
SELECT set_config('sleepagent.actor_role', 'elder', true);
SELECT set_config('sleepagent.service_principal_id', 'sleepagent-p4e1-r2-api', true);
SELECT set_config('sleepagent.process_role', 'api', true);
SELECT set_config('sleepagent.purpose', 'sleep_care', true);
SELECT set_config('sleepagent.authorization_epoch', '1', true);
SELECT set_config('sleepagent.privacy_epoch', '1', true);
SELECT set_config('sleepagent.retrieval_policy_epoch', '1', true);
SELECT set_config('sleepagent.worker_instance', '', true);
\o

SELECT now() AS observed_at,
       current_setting('TimeZone') AS database_timezone,
       current_setting('transaction_read_only') AS transaction_read_only,
       :'wake_date'::date AS target_wake_date,
       (SELECT max(version) FROM public.sleepagent_schema_migrations) AS schema_version;

SELECT device_binding_id, binding_version, status, provider_id,
       provider_account_id, timezone_name
FROM public.sleep_domain_device_bindings
WHERE namespace_id = current_setting('sleepagent.namespace_id')
  AND data_mode = current_setting('sleepagent.data_mode')
  AND subject_id = current_setting('sleepagent.subject_id')
  AND status = 'active';

WITH received AS (
  SELECT received_at,
         lag(received_at) OVER (ORDER BY received_at) AS previous_received_at
  FROM public.sleep_domain_raw_inbox
  WHERE namespace_id = current_setting('sleepagent.namespace_id')
    AND data_mode = current_setting('sleepagent.data_mode')
    AND subject_id = current_setting('sleepagent.subject_id')
    AND provider_id = 'perceptor'
    AND event_type = 'VitalSignsDataEvent'
    AND received_at >= (:'wake_date'::date - 1 + time '12:00') AT TIME ZONE 'Asia/Shanghai'
    AND received_at < (:'wake_date'::date + time '12:00') AT TIME ZONE 'Asia/Shanghai'
)
SELECT count(*) AS push_receipt_count,
       min(received_at) AS first_push_received_at,
       max(received_at) AS last_push_received_at,
       max(received_at - previous_received_at) AS maximum_push_receipt_gap
FROM received;

WITH received AS (
  SELECT received_at,
         lag(received_at) OVER (ORDER BY received_at) AS previous_received_at
  FROM public.sleep_domain_raw_inbox
  WHERE namespace_id = current_setting('sleepagent.namespace_id')
    AND data_mode = current_setting('sleepagent.data_mode')
    AND subject_id = current_setting('sleepagent.subject_id')
    AND provider_id = 'perceptor'
    AND event_type = 'VitalSignsDataEvent'
    AND received_at >= (:'wake_date'::date - 1 + time '12:00') AT TIME ZONE 'Asia/Shanghai'
    AND received_at < (:'wake_date'::date + time '12:00') AT TIME ZONE 'Asia/Shanghai'
)
SELECT previous_received_at AS gap_start,
       received_at AS gap_end,
       received_at - previous_received_at AS receipt_gap,
       CASE WHEN received_at - previous_received_at >= interval '5 minutes'
            THEN 'REPAIR_THRESHOLD_CROSSED'
            ELSE 'WARNING_THRESHOLD_CROSSED' END AS detector_class
FROM received
WHERE received_at - previous_received_at >= interval '3 minutes'
ORDER BY previous_received_at;

-- Perceptor Push and Pull create normalization work atomically with raw ingress:
-- work.created_at = raw.received_at, while the raw measurement/event columns are
-- null.  Bound that already-authorized work provenance first so forced RLS does
-- not scan the full encrypted raw inbox and then repeat one protected work lookup
-- per matching raw row.  The original effective-time predicate remains below as
-- a semantic guard.
WITH acceptance_window AS (
  SELECT (:'wake_date'::date - 1 + time '12:00')
           AT TIME ZONE 'Asia/Shanghai' AS starts_at,
         (:'wake_date'::date + time '12:00')
           AT TIME ZONE 'Asia/Shanghai' AS ends_at
), bounded_normalization_work AS MATERIALIZED (
  SELECT work.namespace_id, work.data_mode, work.raw_ingress_record_id,
         work.work_json, work.status, work.available_at, work.updated_at
  FROM public.sleep_domain_normalization_work AS work
  CROSS JOIN acceptance_window AS acceptance
  WHERE work.namespace_id = current_setting('sleepagent.namespace_id')
    AND work.data_mode = current_setting('sleepagent.data_mode')
    AND work.subject_id = current_setting('sleepagent.subject_id')
    AND work.created_at >= acceptance.starts_at
    AND work.created_at < acceptance.ends_at
)
SELECT raw.event_type,
       coalesce(work.work_json ->> 'normalizer', '') AS normalizer,
       coalesce(work.work_json ->> 'endpoint', '') AS endpoint,
       work.status, count(*) AS work_count,
       min(work.available_at) AS oldest_available_at,
       max(work.updated_at) AS newest_updated_at
FROM bounded_normalization_work AS work
JOIN public.sleep_domain_raw_inbox AS raw
  ON raw.namespace_id = work.namespace_id
 AND raw.data_mode = work.data_mode
 AND raw.raw_ingress_record_id = work.raw_ingress_record_id
CROSS JOIN acceptance_window AS acceptance
WHERE coalesce(raw.measurement_at, raw.event_occurred_at, raw.received_at)
      >= acceptance.starts_at
  AND coalesce(raw.measurement_at, raw.event_occurred_at, raw.received_at)
      < acceptance.ends_at
GROUP BY raw.event_type, normalizer, endpoint, work.status
ORDER BY raw.event_type, normalizer, work.status;

WITH bounded_pull_work AS MATERIALIZED (
  SELECT work.namespace_id, work.data_mode, work.raw_ingress_record_id,
         work.status, work.attempt_count, work.work_json,
         work.created_at, work.updated_at
  FROM public.sleep_domain_normalization_work AS work
  WHERE work.namespace_id = current_setting('sleepagent.namespace_id')
    AND work.data_mode = current_setting('sleepagent.data_mode')
    AND work.subject_id = current_setting('sleepagent.subject_id')
    AND work.work_json ->> 'normalizer' = 'perceptor_pull'
    AND work.created_at >= (:'wake_date'::date - 2)::timestamp
        AT TIME ZONE 'Asia/Shanghai'
    AND work.created_at < (:'wake_date'::date + 1)::timestamp
        AT TIME ZONE 'Asia/Shanghai'
)
SELECT raw.event_type, work.status, work.attempt_count,
       work.work_json ->> 'requested_window_start' AS requested_window_start,
       work.work_json ->> 'requested_window_end' AS requested_window_end,
       work.created_at, work.updated_at
FROM bounded_pull_work AS work
JOIN public.sleep_domain_raw_inbox AS raw
  ON raw.namespace_id = work.namespace_id
 AND raw.data_mode = work.data_mode
 AND raw.raw_ingress_record_id = work.raw_ingress_record_id
WHERE raw.event_type LIKE 'PerceptorPullResponse:%'
ORDER BY work.created_at;

SELECT job_type, scheduled_for, status, attempt_count, last_error_code,
       fired_at, completed_at
FROM public.backend_acquisition_schedule_fires
WHERE namespace_id = current_setting('sleepagent.namespace_id')
  AND data_mode = current_setting('sleepagent.data_mode')
  AND subject_id = current_setting('sleepagent.subject_id')
  AND scheduled_for >= (:'wake_date'::date - 1)::timestamp AT TIME ZONE 'Asia/Shanghai'
  AND scheduled_for < (:'wake_date'::date + 1)::timestamp AT TIME ZONE 'Asia/Shanghai'
ORDER BY scheduled_for;

SELECT night_episode_id, state, collection_start_at,
       deterministic_close_deadline_at, bed_at, wake_at,
       wake_local_date, episode_local_date, date_state, date_conflict
FROM public.sleep_domain_night_episodes
WHERE namespace_id = current_setting('sleepagent.namespace_id')
  AND data_mode = current_setting('sleepagent.data_mode')
  AND subject_id = current_setting('sleepagent.subject_id')
  AND coalesce(wake_local_date, episode_local_date) = :'wake_date'::date
ORDER BY collection_start_at;

SELECT finalization.night_finalization_id, finalization.night_episode_id,
       finalization.state, finalization.current_revision_number,
       finalization.updated_at
FROM public.sleep_domain_night_finalizations AS finalization
JOIN public.sleep_domain_night_episodes AS episode
  ON episode.night_episode_id = finalization.night_episode_id
 AND episode.namespace_id = finalization.namespace_id
 AND episode.data_mode = finalization.data_mode
WHERE finalization.namespace_id = current_setting('sleepagent.namespace_id')
  AND finalization.data_mode = current_setting('sleepagent.data_mode')
  AND finalization.subject_id = current_setting('sleepagent.subject_id')
  AND coalesce(episode.wake_local_date, episode.episode_local_date) = :'wake_date'::date;

SELECT operation_type, status, count(*) AS operation_count,
       min(updated_at) AS oldest_updated_at, max(updated_at) AS newest_updated_at
FROM public.sleep_domain_operations
WHERE namespace_id = current_setting('sleepagent.namespace_id')
  AND data_mode = current_setting('sleepagent.data_mode')
  AND subject_id = current_setting('sleepagent.subject_id')
  AND operation_type IN (
    'perceptor.history_overlap_pull', 'perceptor.sleep_report_pull',
    'night.finalization_scan', 'product.report.run.v1',
    'product.shared_analysis.v1'
  )
  AND (
    (
      operation_type IN (
        'perceptor.history_overlap_pull', 'perceptor.sleep_report_pull',
        'night.finalization_scan'
      )
      AND created_at >= (:'wake_date'::date - 1)::timestamp
          AT TIME ZONE 'Asia/Shanghai'
      AND created_at < (:'wake_date'::date + 1)::timestamp
          AT TIME ZONE 'Asia/Shanghai'
    )
    OR (
      operation_type IN (
        'product.report.run.v1', 'product.shared_analysis.v1'
      )
      AND EXISTS (
        SELECT 1
        FROM public.sleep_domain_night_episodes AS episode
        WHERE episode.namespace_id = sleep_domain_operations.namespace_id
          AND episode.data_mode = sleep_domain_operations.data_mode
          AND episode.subject_id = sleep_domain_operations.subject_id
          AND coalesce(episode.wake_local_date, episode.episode_local_date) =
              :'wake_date'::date
          AND (
            sleep_domain_operations.target_resource_id =
              episode.night_episode_id
            OR sleep_domain_operations.target_resource_key IN (
              episode.night_episode_id, episode.current_revision_id
            )
          )
      )
    )
  )
GROUP BY operation_type, status
ORDER BY operation_type, status;

COMMIT;
