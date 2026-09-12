\set ON_ERROR_STOP on
\pset pager off
\pset format unaligned
\pset tuples_only on

BEGIN READ ONLY;
SET LOCAL statement_timeout = '30s';
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

\if :list_mode
WITH recent AS (
  SELECT episode.night_episode_id,
         coalesce(episode.wake_local_date, episode.episode_local_date) AS wake_date,
         episode.state AS episode_state,
         CASE
           WHEN episode.wake_at IS NULL THEN NULL
           ELSE to_char(
             episode.wake_at AT TIME ZONE coalesce(episode.timezone_name, 'Asia/Shanghai'),
             'HH24:MI'
           )
         END AS wake,
         finalization.state AS finalization_state,
         CASE
           WHEN finalization_revision.source_report_version_id IS NOT NULL
             THEN 'available'
           ELSE 'unavailable'
         END AS report_state,
         episode.updated_at
  FROM public.sleep_domain_night_episodes AS episode
  LEFT JOIN public.sleep_domain_night_finalizations AS finalization
    ON finalization.namespace_id = episode.namespace_id
   AND finalization.data_mode = episode.data_mode
   AND finalization.night_episode_id = episode.night_episode_id
  LEFT JOIN public.sleep_domain_night_finalization_revisions AS finalization_revision
    ON finalization_revision.night_finalization_id =
       finalization.night_finalization_id
   AND finalization_revision.night_finalization_revision_id =
       finalization.current_finalization_revision_id
  WHERE episode.namespace_id = current_setting('sleepagent.namespace_id')
    AND episode.data_mode = current_setting('sleepagent.data_mode')
    AND episode.subject_id = current_setting('sleepagent.subject_id')
    AND coalesce(episode.wake_local_date, episode.episode_local_date) IS NOT NULL
  ORDER BY wake_date DESC, episode.updated_at DESC
  LIMIT 30
)
SELECT jsonb_build_object(
  'schema_version', 'sleepagent.nightly_list.v1',
  'transaction_read_only', current_setting('transaction_read_only')::boolean,
  'nights', coalesce(jsonb_agg(jsonb_build_object(
    'wake_date', wake_date,
    'episode_state', episode_state,
    'wake', wake,
    'finalization_state', coalesce(finalization_state, 'absent'),
    'report_state', report_state
  ) ORDER BY wake_date DESC, updated_at DESC), '[]'::jsonb)
)::text
FROM recent;
\else
WITH target_episode AS (
  SELECT episode.*
  FROM public.sleep_domain_night_episodes AS episode
  WHERE episode.namespace_id = current_setting('sleepagent.namespace_id')
    AND episode.data_mode = current_setting('sleepagent.data_mode')
    AND episode.subject_id = current_setting('sleepagent.subject_id')
    AND coalesce(episode.wake_local_date, episode.episode_local_date) = :'wake_date'::date
  ORDER BY (episode.wake_local_date = :'wake_date'::date) DESC,
           episode.updated_at DESC
  LIMIT 1
), night_window AS (
  SELECT (:'wake_date'::date - 1 + time '12:00') AT TIME ZONE 'Asia/Shanghai' AS starts_at,
         (:'wake_date'::date + time '12:00') AT TIME ZONE 'Asia/Shanghai' AS ends_at
), raw_evidence AS MATERIALIZED (
  SELECT raw.raw_ingress_record_id,
         raw.event_type,
         raw.received_at,
         raw.raw_metadata_json
  FROM public.sleep_domain_raw_inbox AS raw
  CROSS JOIN night_window AS nw
  WHERE raw.namespace_id = current_setting('sleepagent.namespace_id')
    AND raw.data_mode = current_setting('sleepagent.data_mode')
    AND raw.subject_id = current_setting('sleepagent.subject_id')
    AND raw.provider_id = 'perceptor'
    AND (
      (
        raw.event_type = 'VitalSignsDataEvent'
        AND raw.received_at >= nw.starts_at
        AND raw.received_at < nw.ends_at
      )
      OR (
        raw.raw_metadata_json ->> 'endpoint' = '/vitalSigns/getHistoryData'
        AND coalesce(
          (raw.raw_metadata_json ->> 'requested_window_end')::timestamptz,
          raw.received_at
        ) > nw.starts_at
        AND coalesce(
          (raw.raw_metadata_json ->> 'requested_window_start')::timestamptz,
          raw.received_at
        ) < nw.ends_at
      )
      OR (
        raw.raw_metadata_json ->> 'endpoint' = '/vitalSigns/getSleepReport'
        AND raw.raw_metadata_json ->> 'requested_report_date' = :'wake_date'
      )
    )
), push AS (
  SELECT count(*) AS raw_count,
         min(raw.received_at) AS first_received_at,
         max(raw.received_at) AS last_received_at
  FROM raw_evidence AS raw
  WHERE raw.event_type = 'VitalSignsDataEvent'
), history_raw AS (
  SELECT raw.raw_ingress_record_id
  FROM raw_evidence AS raw
  WHERE raw.raw_metadata_json ->> 'endpoint' = '/vitalSigns/getHistoryData'
), history AS (
  SELECT count(*) AS raw_count
  FROM history_raw
), report_raw AS (
  SELECT count(*) AS raw_count
  FROM raw_evidence AS raw
  WHERE raw.raw_metadata_json ->> 'endpoint' = '/vitalSigns/getSleepReport'
), report_provenance AS (
  SELECT revision.source_report_version_id,
         revision.created_at AS reconciled_at
  FROM target_episode AS episode
  JOIN public.sleep_domain_night_finalizations AS item
    ON item.namespace_id = episode.namespace_id
   AND item.data_mode = episode.data_mode
   AND item.night_episode_id = episode.night_episode_id
  JOIN public.sleep_domain_night_finalization_revisions AS revision
    ON revision.night_finalization_id = item.night_finalization_id
   AND revision.night_finalization_revision_id =
       item.current_finalization_revision_id
), confirmed_wake_revision AS (
  SELECT revision.revision_number,
         revision.revision_json ->> 'revision_cause' AS revision_cause,
         revision.created_at AS confirmed_at
  FROM target_episode AS episode
  JOIN public.sleep_domain_night_episode_revisions AS revision
    ON revision.namespace_id = episode.namespace_id
   AND revision.data_mode = episode.data_mode
   AND revision.night_episode_id = episode.night_episode_id
  WHERE revision.revision_json ->> 'revision_cause'
        LIKE 'confirmed_observed_wake%'
  ORDER BY revision.revision_number DESC
  LIMIT 1
), finalization AS (
  SELECT item.state, item.current_revision_number, item.updated_at,
         revision.coverage_status,
         revision.revision_cause
  FROM target_episode AS episode
  JOIN public.sleep_domain_night_finalizations AS item
    ON item.namespace_id = episode.namespace_id
   AND item.data_mode = episode.data_mode
   AND item.night_episode_id = episode.night_episode_id
  LEFT JOIN public.sleep_domain_night_finalization_revisions AS revision
    ON revision.night_finalization_id = item.night_finalization_id
   AND revision.night_finalization_revision_id = item.current_finalization_revision_id
), product_operations AS (
  SELECT operation.operation_type, operation.status, count(*) AS operation_count
  FROM target_episode AS episode
  JOIN public.sleep_domain_operations AS operation
    ON operation.namespace_id = episode.namespace_id
   AND operation.data_mode = episode.data_mode
   AND operation.subject_id = episode.subject_id
   AND operation.operation_type IN ('product.report.run.v1', 'product.shared_analysis.v1')
   AND (operation.target_resource_id = episode.night_episode_id
        OR operation.target_resource_key IN (
          episode.night_episode_id, episode.current_revision_id
        ))
  GROUP BY operation.operation_type, operation.status
), product AS (
  SELECT (SELECT count(*) FROM public.sleep_domain_analysis_revisions AS analysis, target_episode AS episode
          WHERE analysis.namespace_id = episode.namespace_id
            AND analysis.data_mode = episode.data_mode
            AND analysis.night_episode_id = episode.night_episode_id) AS analysis_revision_count,
         coalesce(jsonb_object_agg(operation_type || ':' || status, operation_count), '{}'::jsonb) AS operation_statuses
  FROM product_operations
)
SELECT jsonb_build_object(
  'schema_version', 'sleepagent.nightly_summary.v1',
  'wake_date', :'wake_date',
  'night_found', EXISTS (SELECT 1 FROM target_episode),
  'transaction_read_only', current_setting('transaction_read_only')::boolean,
  'acquisition', jsonb_build_object(
    'push', jsonb_build_object(
      'status', CASE WHEN push.raw_count > 0 THEN 'stored' ELSE 'not_observed' END,
      'raw_receipt_count', push.raw_count,
      'first_received_at', push.first_received_at,
      'last_received_at', push.last_received_at
    ),
    'history', jsonb_build_object(
      'status', CASE
        WHEN history.raw_count = 0 THEN 'not_observed'
        ELSE 'stored_raw'
      END,
      'raw_response_count', history.raw_count,
      'normalization_statuses', NULL
    ),
    'sleep_report', jsonb_build_object(
      'status', CASE
        WHEN report_provenance.source_report_version_id IS NOT NULL
          THEN 'available'
        WHEN report_raw.raw_count > 0 THEN 'unknown'
        ELSE 'not_observed'
      END,
      'raw_response_count', report_raw.raw_count,
      'source_version_count', CASE
        WHEN report_provenance.source_report_version_id IS NULL THEN NULL
        ELSE 1
      END,
      'current_version', NULL,
      'fetched_at', CASE
        WHEN report_provenance.source_report_version_id IS NULL THEN NULL
        ELSE report_provenance.reconciled_at
      END
    )
  ),
  'night_episode', CASE WHEN episode.night_episode_id IS NULL THEN NULL ELSE jsonb_build_object(
    'episode_id', episode.night_episode_id,
    'current_revision', episode.current_revision_number,
    'state', episode.state,
    'collection_start_at', episode.collection_start_at,
    'bed_at', episode.bed_at,
    'wake_at', episode.wake_at,
    'boundary_policy_version', episode.boundary_policy_version,
    'date_state', episode.date_state,
    'date_conflict', episode.date_conflict
  ) END,
  'wake_confirmation', CASE WHEN episode.night_episode_id IS NULL THEN NULL ELSE jsonb_build_object(
    'candidate_wake_at', episode.episode_json -> 'candidate_wake_at',
    'latest_bed_presence_at', episode.episode_json -> 'latest_bed_presence_at',
    'confirmed_revision', confirmed_wake_revision.revision_number,
    'confirmed_at', confirmed_wake_revision.confirmed_at,
    'confirmed_revision_cause', confirmed_wake_revision.revision_cause
  ) END,
  'data', jsonb_build_object(
    'canonical_observation_count', NULL,
    'observation_type_counts', NULL
  ),
  'finalization', CASE WHEN finalization.state IS NULL THEN NULL ELSE jsonb_build_object(
    'state', finalization.state,
    'current_revision', finalization.current_revision_number,
    'coverage_status', finalization.coverage_status,
    'revision_cause', finalization.revision_cause,
    'updated_at', finalization.updated_at
  ) END,
  'product', jsonb_build_object(
    'analysis_revision_count', product.analysis_revision_count,
    'operation_statuses', product.operation_statuses
  )
)::text
FROM push
CROSS JOIN history
CROSS JOIN report_raw
CROSS JOIN product
LEFT JOIN target_episode AS episode ON TRUE
LEFT JOIN report_provenance ON TRUE
LEFT JOIN confirmed_wake_revision ON TRUE
LEFT JOIN finalization ON TRUE;
\endif

COMMIT;
