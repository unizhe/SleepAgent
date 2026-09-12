-- sleepagent:transactional=true

-- R1-A/R1-B: bound post-claim crash recovery independently from handler
-- failures, and make schedule time advance strictly and without catch-up bursts.

ALTER TABLE public.sleep_domain_normalization_work
  ADD COLUMN execution_reclaim_count INTEGER NOT NULL DEFAULT 0
    CHECK (execution_reclaim_count >= 0),
  ADD COLUMN max_execution_reclaims INTEGER NOT NULL DEFAULT 3
    CHECK (max_execution_reclaims BETWEEN 1 AND 100);
ALTER TABLE public.sleep_domain_operations
  ADD COLUMN execution_reclaim_count INTEGER NOT NULL DEFAULT 0
    CHECK (execution_reclaim_count >= 0),
  ADD COLUMN max_execution_reclaims INTEGER NOT NULL DEFAULT 3
    CHECK (max_execution_reclaims BETWEEN 1 AND 100);
ALTER TABLE public.backend_delivery_intents
  ADD COLUMN execution_reclaim_count INTEGER NOT NULL DEFAULT 0
    CHECK (execution_reclaim_count >= 0),
  ADD COLUMN max_execution_reclaims INTEGER NOT NULL DEFAULT 3
    CHECK (max_execution_reclaims BETWEEN 1 AND 100);
ALTER TABLE public.backend_retention_jobs
  ADD COLUMN execution_reclaim_count INTEGER NOT NULL DEFAULT 0
    CHECK (execution_reclaim_count >= 0),
  ADD COLUMN max_execution_reclaims INTEGER NOT NULL DEFAULT 3
    CHECK (max_execution_reclaims BETWEEN 1 AND 100);

CREATE OR REPLACE FUNCTION public.sleepagent_exhaust_expired_reclaims_v1(
  requested_kind TEXT,
  requested_selector TEXT DEFAULT NULL
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  deployment_mode TEXT := NULLIF(
    current_setting('sleepagent.data_mode', TRUE), ''
  );
  affected INTEGER := 0;
  changed INTEGER := 0;
BEGIN
  IF deployment_mode NOT IN ('live', 'replay')
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'worker'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'trusted worker context is required for reclaim exhaustion';
  END IF;

  IF requested_kind = 'normalization' THEN
    UPDATE public.sleep_domain_normalization_work AS work
    SET status = 'quarantined',
        last_error_code = 'execution_reclaim_exhausted',
        lease_owner = NULL, worker_instance = NULL, fencing_token = NULL,
        heartbeat_at = NULL, lease_expires_at = NULL,
        updated_at = clock_timestamp()
    WHERE work.data_mode = deployment_mode
      AND work.status = 'running'
      AND work.lease_expires_at <= clock_timestamp()
      AND work.execution_reclaim_count >= work.max_execution_reclaims;
  ELSIF requested_kind = 'operation' THEN
    UPDATE public.sleep_domain_operations AS operation
    SET status = 'dead_letter',
        outcome_class = 'terminal_failure',
        dead_lettered_at = clock_timestamp(),
        lease_owner = NULL, worker_instance = NULL, fencing_token = NULL,
        heartbeat_at = NULL, lease_expires_at = NULL,
        cas_version = operation.cas_version + 1,
        updated_at = clock_timestamp()
    WHERE operation.data_mode = deployment_mode
      AND operation.queue_name = requested_selector
      AND operation.status = 'running'
      AND operation.lease_expires_at <= clock_timestamp()
      AND operation.execution_reclaim_count >=
        operation.max_execution_reclaims;
  ELSIF requested_kind = 'delivery' THEN
    UPDATE public.backend_delivery_intents AS intent
    SET status = CASE WHEN intent.status = 'dispatching'
          OR intent.dispatch_permit_at IS NOT NULL
        THEN 'outcome_unknown' ELSE 'dead_letter' END,
        worker_instance = NULL, fencing_token = NULL,
        lease_expires_at = NULL,
        updated_at = clock_timestamp()
    WHERE intent.data_mode = deployment_mode
      AND intent.destination = requested_selector
      AND intent.status IN ('running', 'dispatching')
      AND intent.lease_expires_at <= clock_timestamp()
      AND intent.execution_reclaim_count >= intent.max_execution_reclaims;
  ELSIF requested_kind = 'retention' THEN
    UPDATE public.backend_retention_jobs AS job
    SET status = 'dead_letter',
        worker_instance = NULL, fencing_token = NULL,
        heartbeat_at = NULL, lease_expires_at = NULL,
        updated_at = clock_timestamp()
    WHERE job.data_mode = deployment_mode
      AND job.status = 'running'
      AND job.lease_expires_at <= clock_timestamp()
      AND job.execution_reclaim_count >= job.max_execution_reclaims;
  ELSE
    RAISE EXCEPTION 'unsupported reclaim kind';
  END IF;
  GET DIAGNOSTICS changed = ROW_COUNT;
  affected := affected + changed;
  RETURN affected;
END;
$$;

REVOKE ALL ON FUNCTION public.sleepagent_exhaust_expired_reclaims_v1(TEXT, TEXT)
  FROM PUBLIC;

-- Patch the four active claim functions while retaining their existing
-- authorization, fairness, predecessor, and capacity rules.
DO $$
DECLARE
  definition TEXT;
  original TEXT;
BEGIN
  definition := pg_get_functiondef(
    'public.sleepagent_claim_normalization_work(text,integer)'::regprocedure
  );
  original := definition;
  definition := replace(definition, '  RETURN QUERY' || chr(10),
    '  PERFORM public.sleepagent_exhaust_expired_reclaims_v1('
      || '''normalization'', NULL);' || chr(10) || chr(10) || '  RETURN QUERY'
      || chr(10));
  definition := replace(definition,
    'AND work.lease_expires_at <= clock_timestamp())',
    'AND work.lease_expires_at <= clock_timestamp()' || chr(10)
      || '          AND work.execution_reclaim_count < '
      || 'work.max_execution_reclaims)');
  definition := replace(definition,
    'lease_generation = work.lease_generation + 1,',
    'execution_reclaim_count = CASE' || chr(10)
      || '          WHEN candidate.starts_business_attempt '
      || 'THEN work.execution_reclaim_count' || chr(10)
      || '          ELSE work.execution_reclaim_count + 1' || chr(10)
      || '        END,' || chr(10)
      || '        lease_generation = work.lease_generation + 1,');
  IF definition = original
     OR position('execution_reclaim_count <' IN definition) = 0
     OR position('sleepagent_exhaust_expired_reclaims_v1' IN definition) = 0
     OR position('ELSE work.execution_reclaim_count + 1' IN definition) = 0 THEN
    RAISE EXCEPTION 'normalization reclaim patch drifted';
  END IF;
  EXECUTE definition;

  definition := pg_get_functiondef(
    'public.sleepagent_claim_operation(text,text,integer)'::regprocedure
  );
  original := definition;
  definition := replace(definition, '  RETURN QUERY' || chr(10),
    '  PERFORM public.sleepagent_exhaust_expired_reclaims_v1('
      || '''operation'', requested_queue);' || chr(10) || chr(10)
      || '  RETURN QUERY' || chr(10));
  definition := replace(definition,
    'AND op.lease_expires_at <= clock_timestamp())',
    'AND op.lease_expires_at <= clock_timestamp()' || chr(10)
      || '          AND op.execution_reclaim_count < '
      || 'op.max_execution_reclaims)');
  definition := replace(definition,
    'lease_generation = op.lease_generation + 1,',
    'execution_reclaim_count = CASE' || chr(10)
      || '          WHEN candidate.starts_business_attempt '
      || 'THEN op.execution_reclaim_count' || chr(10)
      || '          ELSE op.execution_reclaim_count + 1' || chr(10)
      || '        END,' || chr(10)
      || '        lease_generation = op.lease_generation + 1,');
  IF definition = original
     OR position('execution_reclaim_count <' IN definition) = 0
     OR position('sleepagent_exhaust_expired_reclaims_v1' IN definition) = 0
     OR position('ELSE op.execution_reclaim_count + 1' IN definition) = 0 THEN
    RAISE EXCEPTION 'operation reclaim patch drifted';
  END IF;
  EXECUTE definition;

  definition := pg_get_functiondef(
    'public.sleepagent_claim_delivery(text,text,integer)'::regprocedure
  );
  original := definition;
  definition := replace(definition, '  RETURN QUERY' || chr(10),
    '  PERFORM public.sleepagent_exhaust_expired_reclaims_v1('
      || '''delivery'', requested_destination);' || chr(10) || chr(10)
      || '  RETURN QUERY' || chr(10));
  definition := replace(definition,
    'AND intent.lease_expires_at <= clock_timestamp())',
    'AND intent.lease_expires_at <= clock_timestamp()' || chr(10)
      || '          AND intent.execution_reclaim_count < '
      || 'intent.max_execution_reclaims)');
  definition := replace(definition,
    'lease_generation = intent.lease_generation + 1,',
    'execution_reclaim_count = CASE' || chr(10)
      || '          WHEN candidate.starts_business_attempt '
      || 'THEN intent.execution_reclaim_count' || chr(10)
      || '          ELSE intent.execution_reclaim_count + 1' || chr(10)
      || '        END,' || chr(10)
      || '        lease_generation = intent.lease_generation + 1,');
  IF definition = original
     OR position('execution_reclaim_count <' IN definition) = 0
     OR position('sleepagent_exhaust_expired_reclaims_v1' IN definition) = 0
     OR position('ELSE intent.execution_reclaim_count + 1' IN definition) = 0 THEN
    RAISE EXCEPTION 'delivery reclaim patch drifted';
  END IF;
  EXECUTE definition;

  definition := pg_get_functiondef(
    'public.sleepagent_claim_retention_job(text,integer)'::regprocedure
  );
  original := definition;
  definition := replace(definition, '  RETURN QUERY' || chr(10),
    '  PERFORM public.sleepagent_exhaust_expired_reclaims_v1('
      || '''retention'', NULL);' || chr(10) || chr(10) || '  RETURN QUERY'
      || chr(10));
  definition := replace(definition,
    'AND job.lease_expires_at <= clock_timestamp())',
    'AND job.lease_expires_at <= clock_timestamp()' || chr(10)
      || '          AND job.execution_reclaim_count < '
      || 'job.max_execution_reclaims)');
  definition := replace(definition,
    'lease_generation = job.lease_generation + 1,',
    'execution_reclaim_count = CASE' || chr(10)
      || '          WHEN candidate.starts_business_attempt '
      || 'THEN job.execution_reclaim_count' || chr(10)
      || '          ELSE job.execution_reclaim_count + 1' || chr(10)
      || '        END,' || chr(10)
      || '        lease_generation = job.lease_generation + 1,');
  IF definition = original
     OR position('execution_reclaim_count <' IN definition) = 0
     OR position('sleepagent_exhaust_expired_reclaims_v1' IN definition) = 0
     OR position('ELSE job.execution_reclaim_count + 1' IN definition) = 0 THEN
    RAISE EXCEPTION 'retention reclaim patch drifted';
  END IF;
  EXECUTE definition;
END;
$$;

ALTER TABLE public.backend_acquisition_schedules
  ADD CONSTRAINT backend_acquisition_schedule_jitter_lt_cadence
  CHECK (jitter_seconds >= 0 AND jitter_seconds < cadence_seconds);

-- Preserve deterministic signed jitter, but skip missed cadence slots instead
-- of emitting catch-up work on every poll. One invocation already emits at
-- most requested_limit fires and at most one fire per selected schedule.
DO $$
DECLARE
  definition TEXT;
  original TEXT;
BEGIN
  definition := pg_get_functiondef(
    'public.sleepagent_fire_due_acquisition_schedules(text,integer,boolean)'
      ::regprocedure
  );
  original := definition;
  definition := replace(definition,
    'next_run_at = candidate.next_run_at' || chr(10)
      || '          + make_interval(secs => candidate.cadence_seconds + jitter_offset),',
    'next_run_at = GREATEST(' || chr(10)
      || '          candidate.next_run_at + make_interval(' || chr(10)
      || '            secs => candidate.cadence_seconds + jitter_offset' || chr(10)
      || '          ),' || chr(10)
      || '          clock_timestamp() + make_interval(' || chr(10)
      || '            secs => candidate.cadence_seconds + jitter_offset' || chr(10)
      || '          )' || chr(10)
      || '        ),');
  IF definition = original
     OR position('clock_timestamp() + make_interval' IN definition) = 0
     OR position('candidate.cadence_seconds + jitter_offset' IN definition) = 0 THEN
    RAISE EXCEPTION 'acquisition time advancement patch drifted';
  END IF;
  EXECUTE definition;
END;
$$;

-- Operational totals count independent reclaims, include current outcome work,
-- and treat normalization quarantine as governed terminal degradation.
DO $$
DECLARE
  definition TEXT;
  original TEXT;
BEGIN
  definition := pg_get_functiondef(
    'public.sleepagent_internal_operational_metrics()'::regprocedure
  );
  original := definition;
  definition := replace(definition,
    'work.lease_generation' || chr(10)
      || '    FROM public.sleep_domain_normalization_work AS work',
    'work.execution_reclaim_count AS reclaim_count' || chr(10)
      || '    FROM public.sleep_domain_normalization_work AS work');
  definition := replace(definition,
    'operation.lease_generation' || chr(10)
      || '    FROM public.sleep_domain_operations AS operation',
    'operation.execution_reclaim_count AS reclaim_count' || chr(10)
      || '    FROM public.sleep_domain_operations AS operation');
  definition := replace(definition,
    'journey.lease_expires_at, journey.lease_generation' || chr(10),
    'journey.lease_expires_at,' || chr(10)
      || '      GREATEST(journey.lease_generation - 1, 0) AS reclaim_count'
      || chr(10));
  definition := replace(definition,
    'intent.lease_expires_at, intent.lease_generation' || chr(10),
    'intent.lease_expires_at, intent.execution_reclaim_count AS reclaim_count'
      || chr(10));
  definition := replace(definition,
    'job.lease_generation' || chr(10)
      || '    FROM public.backend_retention_jobs AS job',
    'job.execution_reclaim_count AS reclaim_count' || chr(10)
      || '    FROM public.backend_retention_jobs AS job');
  definition := replace(definition,
    '''perceptor.sleep_report_pull'', ''night.finalization_scan''',
    '''perceptor.sleep_report_pull'', ''night.finalization_scan'',' || chr(10)
      || '          ''care.outcome.evaluate.v1''');
  definition := replace(definition,
    'COALESCE(sum(GREATEST(lease_generation - 1, 0)), 0)',
    'COALESCE(sum(reclaim_count), 0)');
  definition := replace(definition,
    'WHERE state IN (''dead_letter'', ''failed'', ''blocked'')',
    'WHERE state IN (''dead_letter'', ''failed'', ''blocked'', ''quarantined'')');
  IF definition = original
     OR position('execution_reclaim_count AS reclaim_count' IN definition) = 0
     OR position('COALESCE(sum(reclaim_count), 0)' IN definition) = 0
     OR position('''care.outcome.evaluate.v1''' IN definition) = 0
     OR position('''quarantined''' IN definition) = 0 THEN
    RAISE EXCEPTION 'operational reclaim metrics patch drifted';
  END IF;
  EXECUTE definition;
END;
$$;
