-- sleepagent:transactional=true

-- G7/M13: durable, feature-gated acquisition schedules and idempotent fires.

CREATE TABLE public.backend_acquisition_schedules (
  schedule_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  device_binding_id TEXT NOT NULL,
  binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
  job_type TEXT NOT NULL CHECK (job_type IN (
    'perceptor.history_overlap_pull',
    'perceptor.sleep_report_pull',
    'night.finalization_scan'
  )),
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  next_run_at TIMESTAMPTZ NOT NULL,
  last_fire_at TIMESTAMPTZ,
  last_success_at TIMESTAMPTZ,
  consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
  last_error_code TEXT,
  cadence_seconds INTEGER NOT NULL CHECK (cadence_seconds BETWEEN 60 AND 604800),
  jitter_seconds INTEGER NOT NULL DEFAULT 0 CHECK (jitter_seconds BETWEEN 0 AND 3600),
  max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 100),
  schedule_policy_version TEXT NOT NULL,
  schedule_policy_sha256 TEXT NOT NULL CHECK (
    schedule_policy_sha256 ~ '^[0-9a-f]{64}$'
  ),
  schedule_json JSONB NOT NULL,
  cas_version BIGINT NOT NULL DEFAULT 0 CHECK (cas_version >= 0),
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  UNIQUE (namespace_id, data_mode, subject_id, device_binding_id,
          binding_version, job_type),
  FOREIGN KEY (
    device_binding_id, namespace_id, data_mode, binding_version, subject_id
  ) REFERENCES public.sleep_domain_device_bindings (
    device_binding_id, namespace_id, data_mode, binding_version, subject_id
  ) ON DELETE RESTRICT,
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
    REFERENCES public.backend_namespace_generations (
      namespace_id, data_mode, generation
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%'
      AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%'
      AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX idx_backend_acquisition_schedules_due
  ON public.backend_acquisition_schedules (
    data_mode, enabled, next_run_at, schedule_id
  );

CREATE TABLE public.backend_acquisition_schedule_fires (
  fire_id TEXT PRIMARY KEY,
  schedule_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  device_binding_id TEXT NOT NULL,
  binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
  job_type TEXT NOT NULL,
  scheduled_for TIMESTAMPTZ NOT NULL,
  operation_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued', 'succeeded', 'failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  last_error_code TEXT,
  fired_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  fire_json JSONB NOT NULL,
  CONSTRAINT ux_backend_acquisition_schedule_fire_time
    UNIQUE (schedule_id, scheduled_for),
  UNIQUE (operation_id),
  FOREIGN KEY (schedule_id) REFERENCES public.backend_acquisition_schedules(schedule_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (operation_id, namespace_id, data_mode)
    REFERENCES public.sleep_domain_operations(operation_id, namespace_id, data_mode)
    ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%'
      AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%'
      AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX idx_backend_acquisition_schedule_fires_status
  ON public.backend_acquisition_schedule_fires (
    namespace_id, data_mode, subject_id, status, fired_at
  );

ALTER TABLE public.backend_acquisition_schedules ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.backend_acquisition_schedules FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_acquisition_schedule_scope
  ON public.backend_acquisition_schedules
  USING (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE public.backend_acquisition_schedule_fires ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.backend_acquisition_schedule_fires FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_acquisition_schedule_fire_scope
  ON public.backend_acquisition_schedule_fires
  USING (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

CREATE OR REPLACE FUNCTION public.sleepagent_fire_due_acquisition_schedules(
  claimant_worker_instance TEXT,
  requested_limit INTEGER,
  scheduler_feature_enabled BOOLEAN
)
RETURNS TABLE (
  fire_id TEXT,
  schedule_id TEXT,
  operation_id TEXT,
  job_type TEXT,
  scheduled_for TIMESTAMPTZ
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
  candidate RECORD;
  fire_value TEXT;
  operation_value TEXT;
  operation_digest TEXT;
  operation_timestamp_hex TEXT;
  authorization_snapshot JSONB;
  operation_payload JSONB;
  jitter_offset INTEGER;
BEGIN
  IF scheduler_feature_enabled IS NOT TRUE THEN
    RETURN;
  END IF;
  IF principal IS NULL OR deployment_mode NOT IN ('live', 'replay')
     OR request_purpose <> 'acquisition_schedule'
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'worker'
     OR claimant_worker_instance IS NULL OR claimant_worker_instance = ''
     OR requested_limit < 1 OR requested_limit > 100
     OR claimant_worker_instance IS DISTINCT FROM NULLIF(
       current_setting('sleepagent.worker_instance', TRUE), ''
     )
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid or untrusted acquisition scheduler context';
  END IF;

  FOR candidate IN
    SELECT schedule.*, epoch.authorization_epoch,
      epoch.privacy_epoch, epoch.retrieval_policy_epoch
    FROM public.backend_acquisition_schedules AS schedule
    JOIN public.backend_namespaces AS namespace
      ON namespace.namespace_id = schedule.namespace_id
     AND namespace.data_mode = schedule.data_mode
     AND namespace.status = 'active'
    JOIN public.backend_subject_epochs AS epoch
      ON epoch.namespace_id = schedule.namespace_id
     AND epoch.data_mode = schedule.data_mode
     AND epoch.subject_id = schedule.subject_id
    WHERE schedule.data_mode = deployment_mode
      AND schedule.enabled = TRUE
      AND schedule.next_run_at <= clock_timestamp()
      AND EXISTS (
        SELECT 1 FROM public.backend_principal_grants AS grant_row
        WHERE grant_row.principal_id = principal
          AND grant_row.namespace_id = schedule.namespace_id
          AND grant_row.data_mode = schedule.data_mode
          AND grant_row.purpose = request_purpose
          AND grant_row.authorization_epoch = epoch.authorization_epoch
          AND grant_row.status = 'active'
          AND grant_row.valid_from <= clock_timestamp()
          AND (grant_row.valid_until IS NULL
            OR grant_row.valid_until > clock_timestamp())
          AND grant_row.allowed_handlers_json ? schedule.job_type
      )
      AND EXISTS (
        SELECT 1 FROM public.backend_principal_grants AS worker_grant
        WHERE worker_grant.principal_id = principal
          AND worker_grant.namespace_id = schedule.namespace_id
          AND worker_grant.data_mode = schedule.data_mode
          AND worker_grant.purpose = 'worker'
          AND worker_grant.authorization_epoch = epoch.authorization_epoch
          AND worker_grant.status = 'active'
          AND worker_grant.valid_from <= clock_timestamp()
          AND (worker_grant.valid_until IS NULL
            OR worker_grant.valid_until > clock_timestamp())
          AND worker_grant.allowed_handlers_json ? schedule.job_type
      )
    ORDER BY schedule.next_run_at, schedule.schedule_id
    FOR UPDATE OF schedule SKIP LOCKED
    LIMIT requested_limit
  LOOP
    fire_value := 'schedule-fire:' || encode(digest(convert_to(
      candidate.schedule_id || chr(31) || candidate.next_run_at::text,
      'UTF8'), 'sha256'), 'hex');
    operation_digest := encode(digest(convert_to(
      candidate.schedule_id || chr(31) || candidate.next_run_at::text,
      'UTF8'), 'sha256'), 'hex');
    operation_timestamp_hex := lpad(to_hex(floor(
      extract(epoch FROM candidate.next_run_at) * 1000
    )::bigint), 12, '0');
    operation_value :=
      substr(operation_timestamp_hex, 1, 8) || '-' ||
      substr(operation_timestamp_hex, 9, 4) || '-7' ||
      substr(operation_digest, 1, 3) || '-8' ||
      substr(operation_digest, 4, 3) || '-' ||
      substr(operation_digest, 7, 12);
    authorization_snapshot := jsonb_build_object(
      'schema_version', 'workload_authorization_snapshot.v1',
      'workload_principal_id', principal,
      'namespace_id', candidate.namespace_id,
      'namespace_generation', candidate.namespace_generation,
      'data_mode', candidate.data_mode,
      'run_id', candidate.run_id,
      'arm_id', candidate.arm_id,
      'subject_id', candidate.subject_id,
      'purpose', 'worker',
      'allowed_handler', candidate.job_type,
      'authorization_epoch', candidate.authorization_epoch,
      'privacy_epoch', candidate.privacy_epoch,
      'retrieval_policy_epoch', candidate.retrieval_policy_epoch
    );
    operation_payload := jsonb_build_object(
      'schema_version', 'scheduled_acquisition_operation.v1',
      'schedule_id', candidate.schedule_id,
      'fire_id', fire_value,
      'job_type', candidate.job_type,
      'scheduled_for', candidate.next_run_at,
      'device_binding_id', candidate.device_binding_id,
      'binding_version', candidate.binding_version,
      'authorization_snapshot', authorization_snapshot
    );

    INSERT INTO public.sleep_domain_operations (
      operation_id, namespace_id, data_mode, operation_type,
      subject_id, service_principal_id, actor_id,
      target_resource_id, target_resource_key, idempotency_key,
      request_sha256, status, attempt_count, cas_version,
      operation_json, created_at, updated_at, protocol_version,
      namespace_generation, run_id, arm_id, id_scheme, origin_kind,
      semantic_key, queue_name, priority, available_at, max_attempts,
      workload_authorization_snapshot_json, policy_sha256
    ) VALUES (
      operation_value, candidate.namespace_id, candidate.data_mode,
      candidate.job_type, candidate.subject_id, principal, NULL,
      candidate.device_binding_id,
      candidate.schedule_id || ':' || candidate.next_run_at::text,
      fire_value,
      encode(digest(convert_to(operation_payload::text, 'UTF8'), 'sha256'), 'hex'),
      'pending', 0, 0, operation_payload, clock_timestamp(), clock_timestamp(),
      2, candidate.namespace_generation, candidate.run_id, candidate.arm_id,
      'uuidv7', 'system', fire_value, candidate.job_type, 50,
      clock_timestamp(), candidate.max_attempts, authorization_snapshot,
      candidate.schedule_policy_sha256
    ) ON CONFLICT ON CONSTRAINT sleep_domain_operations_pkey DO NOTHING;

    INSERT INTO public.backend_acquisition_schedule_fires (
      fire_id, schedule_id, namespace_id, data_mode, namespace_generation,
      run_id, arm_id, subject_id, device_binding_id, binding_version,
      job_type, scheduled_for, operation_id, status, fired_at, fire_json
    ) VALUES (
      fire_value, candidate.schedule_id, candidate.namespace_id,
      candidate.data_mode, candidate.namespace_generation,
      candidate.run_id, candidate.arm_id, candidate.subject_id,
      candidate.device_binding_id, candidate.binding_version,
      candidate.job_type, candidate.next_run_at, operation_value, 'queued',
      clock_timestamp(), jsonb_build_object(
        'schema_version', 'acquisition_schedule_fire.v1',
        'schedule_id', candidate.schedule_id,
        'scheduled_for', candidate.next_run_at,
        'operation_id', operation_value
      )
    ) ON CONFLICT ON CONSTRAINT ux_backend_acquisition_schedule_fire_time
      DO NOTHING;

    jitter_offset := CASE WHEN candidate.jitter_seconds = 0 THEN 0 ELSE
      ((pg_catalog.hashtextextended(
        candidate.schedule_id || candidate.next_run_at::text, 0
      ) & 2147483647) % (candidate.jitter_seconds * 2 + 1))::integer
        - candidate.jitter_seconds
    END;
    UPDATE public.backend_acquisition_schedules AS schedule
    SET last_fire_at = clock_timestamp(),
        next_run_at = candidate.next_run_at
          + make_interval(secs => candidate.cadence_seconds + jitter_offset),
        cas_version = schedule.cas_version + 1,
        updated_at = clock_timestamp()
    WHERE schedule.schedule_id = candidate.schedule_id
      AND schedule.cas_version = candidate.cas_version;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'acquisition schedule CAS conflict';
    END IF;

    fire_id := fire_value;
    schedule_id := candidate.schedule_id;
    operation_id := operation_value;
    job_type := candidate.job_type;
    scheduled_for := candidate.next_run_at;
    RETURN NEXT;
  END LOOP;
END;
$$;

REVOKE ALL ON FUNCTION public.sleepagent_fire_due_acquisition_schedules(
  TEXT, INTEGER, BOOLEAN
) FROM PUBLIC;
