-- sleepagent:transactional=true
-- Final release hardening: reachable-table RLS, epoch invalidation, and
-- namespace-fair bounded durable claims.

ALTER TABLE sleep_domain_processing_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_processing_receipts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_processing_receipt_scope
  ON sleep_domain_processing_receipts;
CREATE POLICY sleep_domain_processing_receipt_scope
  ON sleep_domain_processing_receipts
  USING (EXISTS (
    SELECT 1
    FROM public.sleep_domain_raw_inbox AS raw
    WHERE raw.raw_ingress_record_id =
        sleep_domain_processing_receipts.raw_ingress_record_id
      AND raw.namespace_id = sleep_domain_processing_receipts.namespace_id
      AND raw.data_mode = sleep_domain_processing_receipts.data_mode
      AND raw.scope_protocol_version >= 2
      AND public.sleepagent_subject_generation_scope_allows(
        raw.namespace_id, raw.data_mode, raw.subject_id,
        raw.namespace_generation, raw.run_id, raw.arm_id
      )
  ))
  WITH CHECK (EXISTS (
    SELECT 1
    FROM public.sleep_domain_raw_inbox AS raw
    WHERE raw.raw_ingress_record_id =
        sleep_domain_processing_receipts.raw_ingress_record_id
      AND raw.namespace_id = sleep_domain_processing_receipts.namespace_id
      AND raw.data_mode = sleep_domain_processing_receipts.data_mode
      AND raw.scope_protocol_version >= 2
      AND public.sleepagent_subject_generation_scope_allows(
        raw.namespace_id, raw.data_mode, raw.subject_id,
        raw.namespace_generation, raw.run_id, raw.arm_id
      )
  ));

ALTER TABLE sleep_domain_adapter_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_adapter_candidates FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_adapter_candidate_scope
  ON sleep_domain_adapter_candidates;
CREATE POLICY sleep_domain_adapter_candidate_scope
  ON sleep_domain_adapter_candidates
  USING (EXISTS (
    SELECT 1
    FROM public.sleep_domain_raw_inbox AS raw
    WHERE raw.raw_ingress_record_id =
        sleep_domain_adapter_candidates.raw_ingress_record_id
      AND raw.namespace_id = sleep_domain_adapter_candidates.namespace_id
      AND raw.data_mode = sleep_domain_adapter_candidates.data_mode
      AND raw.scope_protocol_version >= 2
      AND public.sleepagent_subject_generation_scope_allows(
        raw.namespace_id, raw.data_mode, raw.subject_id,
        raw.namespace_generation, raw.run_id, raw.arm_id
      )
  ))
  WITH CHECK (EXISTS (
    SELECT 1
    FROM public.sleep_domain_raw_inbox AS raw
    WHERE raw.raw_ingress_record_id =
        sleep_domain_adapter_candidates.raw_ingress_record_id
      AND raw.namespace_id = sleep_domain_adapter_candidates.namespace_id
      AND raw.data_mode = sleep_domain_adapter_candidates.data_mode
      AND raw.scope_protocol_version >= 2
      AND public.sleepagent_subject_generation_scope_allows(
        raw.namespace_id, raw.data_mode, raw.subject_id,
        raw.namespace_generation, raw.run_id, raw.arm_id
      )
  ));

ALTER TABLE sleep_domain_quality_assessments ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_quality_assessments FORCE ROW LEVEL SECURITY;
CREATE POLICY sleep_domain_quality_assessment_scope
  ON sleep_domain_quality_assessments
  USING (public.sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ))
  WITH CHECK (public.sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ));

ALTER TABLE sleep_domain_risk_assessments ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_risk_assessments FORCE ROW LEVEL SECURITY;
CREATE POLICY sleep_domain_risk_assessment_scope
  ON sleep_domain_risk_assessments
  USING (public.sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ))
  WITH CHECK (public.sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ));

ALTER TABLE sleep_domain_vendor_alert_instances ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_vendor_alert_instances FORCE ROW LEVEL SECURITY;
CREATE POLICY sleep_domain_vendor_alert_instance_scope
  ON sleep_domain_vendor_alert_instances
  USING (public.sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ))
  WITH CHECK (public.sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ));

ALTER TABLE sleep_domain_alert_correlation_receipts
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_alert_correlation_receipts
  FORCE ROW LEVEL SECURITY;
CREATE POLICY sleep_domain_alert_correlation_receipt_scope
  ON sleep_domain_alert_correlation_receipts
  USING (public.sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ))
  WITH CHECK (public.sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ));

ALTER TABLE sleep_domain_fast_path_signal_projections
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_fast_path_signal_projections
  FORCE ROW LEVEL SECURITY;
CREATE POLICY sleep_domain_fast_path_signal_projection_scope
  ON sleep_domain_fast_path_signal_projections
  USING (public.sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ))
  WITH CHECK (public.sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ));

ALTER TABLE sleep_domain_fast_path_signal_receipts
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_fast_path_signal_receipts
  FORCE ROW LEVEL SECURITY;
CREATE POLICY sleep_domain_fast_path_signal_receipt_scope
  ON sleep_domain_fast_path_signal_receipts
  USING (public.sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ))
  WITH CHECK (public.sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ));

-- Epoch rotation invalidates query pointers in the same transaction. Historical
-- assessments remain immutable and may be retained under their own policy, but
-- a new authority epoch cannot observe them as current state.
CREATE OR REPLACE FUNCTION sleepagent_invalidate_epoch_scoped_projections()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF NEW.authorization_epoch = OLD.authorization_epoch
     AND NEW.privacy_epoch = OLD.privacy_epoch
     AND NEW.retrieval_policy_epoch = OLD.retrieval_policy_epoch THEN
    RETURN NEW;
  END IF;
  DELETE FROM public.sleep_domain_current_quality
  WHERE namespace_id = NEW.namespace_id AND data_mode = NEW.data_mode
    AND subject_id = NEW.subject_id;
  DELETE FROM public.sleep_domain_current_risk
  WHERE namespace_id = NEW.namespace_id AND data_mode = NEW.data_mode
    AND subject_id = NEW.subject_id;
  DELETE FROM public.sleep_domain_fast_path_signal_projections
  WHERE namespace_id = NEW.namespace_id AND data_mode = NEW.data_mode
    AND subject_id = NEW.subject_id;
  RETURN NEW;
END;
$$;

-- Runtime startup must attest the actor verification-key registry without
-- granting the BFF role direct, scope-free access to the replay seed catalog.
-- Return only the bounded fingerprint set needed by the attestor.
CREATE OR REPLACE FUNCTION sleepagent_attest_actor_verification_keys()
RETURNS TABLE(actor_verification_key_sha256 TEXT)
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT DISTINCT
    seed.metadata_json ->> 'actor_verification_key_sha256'
  FROM public.backend_demo_seed_allowlist AS seed
  WHERE seed.active
    AND seed.metadata_json ->> 'actor_verification_key_sha256' IS NOT NULL
  ORDER BY 1
$$;

REVOKE ALL ON FUNCTION sleepagent_attest_actor_verification_keys()
  FROM PUBLIC;

-- Cross-process operational metrics are derived from durable state so the
-- isolated internal API does not pretend to share in-memory counters with API
-- or Worker processes. Labels are a fixed allowlist and never include tenant,
-- subject, resource, provider request, or free-form error identifiers.
CREATE OR REPLACE FUNCTION sleepagent_internal_operational_metrics()
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
      work.available_at AS ready_at, work.lease_expires_at
    FROM public.sleep_domain_normalization_work AS work
    WHERE work.data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    )
    UNION ALL
    SELECT CASE
        WHEN operation.queue_name IN (
          'fast_path', 'product_agent', 'sleep_command',
          'product_interaction', 'demo_advance', 'induction',
          'demo_reset', 'reconciliation'
        ) THEN operation.queue_name
        ELSE 'unknown'
      END,
      operation.status, operation.available_at, operation.lease_expires_at
    FROM public.sleep_domain_operations AS operation
    WHERE operation.data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    )
    UNION ALL
    SELECT 'replay_journey', journey.phase, journey.resume_at,
      journey.lease_expires_at
    FROM public.backend_demo_journeys AS journey
    WHERE journey.data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    )
    UNION ALL
    SELECT 'delivery', intent.status, intent.available_at,
      intent.lease_expires_at
    FROM public.backend_delivery_intents AS intent
    WHERE intent.data_mode = NULLIF(
      current_setting('sleepagent.data_mode', TRUE), ''
    )
    UNION ALL
    SELECT 'retention', job.status, job.available_at, job.lease_expires_at
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
          AND lease_expires_at <= clock_timestamp()
      ) AS lease_reclaimable_count,
      count(*) FILTER (WHERE state = 'retry') AS retry_count,
      count(*) FILTER (
        WHERE state IN ('dead_letter', 'failed', 'blocked')
      ) AS dead_letter_count,
      count(*) FILTER (
        WHERE state IN ('outcome_unknown', 'reconciliation_required')
      ) AS outcome_unknown_count
    FROM work
    GROUP BY queue_name
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
    'schema_version', 'sleepagent_durable_operational_metrics.v1',
    'queues', COALESCE((
      SELECT jsonb_agg(jsonb_build_object(
        'queue', queue_name,
        'depth', depth,
        'oldest_ready_age_seconds', oldest_ready_age_seconds,
        'lease_reclaimable_count', lease_reclaimable_count,
        'retry_count', retry_count,
        'dead_letter_count', dead_letter_count,
        'outcome_unknown_count', outcome_unknown_count
      ) ORDER BY queue_name)
      FROM queue_metrics
    ), '[]'::JSONB),
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
  ) INTO result;
  RETURN result;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_internal_operational_metrics()
  FROM PUBLIC;

DROP TRIGGER IF EXISTS backend_subject_epoch_projection_invalidation
  ON backend_subject_epochs;
CREATE TRIGGER backend_subject_epoch_projection_invalidation
AFTER UPDATE OF authorization_epoch, privacy_epoch, retrieval_policy_epoch
ON backend_subject_epochs
FOR EACH ROW
EXECUTE FUNCTION sleepagent_invalidate_epoch_scoped_projections();

REVOKE ALL ON FUNCTION sleepagent_invalidate_epoch_scoped_projections()
  FROM PUBLIC;

CREATE TABLE backend_queue_namespace_fairness (
  queue_name TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  claim_count BIGINT NOT NULL DEFAULT 0 CHECK (claim_count >= 0),
  last_claimed_at TIMESTAMPTZ NOT NULL DEFAULT '-infinity',
  PRIMARY KEY (queue_name, namespace_id, data_mode),
  FOREIGN KEY (namespace_id, data_mode)
    REFERENCES backend_namespaces(namespace_id, data_mode)
    ON DELETE RESTRICT
);

ALTER TABLE backend_queue_namespace_fairness ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_queue_namespace_fairness FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_queue_namespace_fairness_scope
  ON backend_queue_namespace_fairness
  USING (public.sleepagent_namespace_scope_allows(namespace_id, data_mode));

CREATE OR REPLACE FUNCTION sleepagent_namespace_last_claimed_at(
  requested_queue TEXT,
  requested_namespace_id TEXT,
  requested_data_mode TEXT
)
RETURNS TIMESTAMPTZ
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT COALESCE((
    SELECT fairness.last_claimed_at
    FROM public.backend_queue_namespace_fairness AS fairness
    WHERE fairness.queue_name = requested_queue
      AND fairness.namespace_id = requested_namespace_id
      AND fairness.data_mode = requested_data_mode
  ), '-infinity'::timestamptz)
$$;

CREATE OR REPLACE FUNCTION sleepagent_namespace_has_worker_capacity(
  requested_namespace_id TEXT,
  requested_data_mode TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT COALESCE((
    SELECT (
      (SELECT count(*) FROM public.sleep_domain_normalization_work AS work
       WHERE work.namespace_id = namespace_row.namespace_id
         AND work.data_mode = namespace_row.data_mode
         AND work.status = 'running'
         AND work.lease_expires_at > clock_timestamp())
      + (SELECT count(*) FROM public.sleep_domain_operations AS operation
         WHERE operation.namespace_id = namespace_row.namespace_id
           AND operation.data_mode = namespace_row.data_mode
           AND operation.status = 'running'
           AND operation.lease_expires_at > clock_timestamp())
      + (SELECT count(*) FROM public.backend_delivery_intents AS intent
         WHERE intent.namespace_id = namespace_row.namespace_id
           AND intent.data_mode = namespace_row.data_mode
           AND intent.status IN ('running', 'dispatching')
           AND intent.lease_expires_at > clock_timestamp())
      + (SELECT count(*) FROM public.backend_retention_jobs AS job
         WHERE job.namespace_id = namespace_row.namespace_id
           AND job.data_mode = namespace_row.data_mode
           AND job.status = 'running'
           AND job.lease_expires_at > clock_timestamp())
      + (SELECT count(*) FROM public.backend_demo_journeys AS journey
         WHERE journey.namespace_id = namespace_row.namespace_id
           AND journey.data_mode = namespace_row.data_mode
           AND journey.worker_instance IS NOT NULL
           AND journey.lease_expires_at > clock_timestamp()
           AND journey.phase NOT IN (
             'succeeded', 'blocked', 'reconciliation_required', 'failed'
           ))
    ) < namespace_row.max_worker_concurrency
    FROM public.backend_namespaces AS namespace_row
    WHERE namespace_row.namespace_id = requested_namespace_id
      AND namespace_row.data_mode = requested_data_mode
      AND namespace_row.status = 'active'
  ), FALSE)
$$;

-- Serialize the capacity decision across every queue without blocking a
-- worker on a hot namespace. The transaction-level advisory lock is held
-- through the claim UPDATE/COMMIT, so another queue cannot pass the capacity
-- check from the same stale snapshot. Hash collisions only reduce throughput;
-- they cannot violate the cap.
CREATE OR REPLACE FUNCTION sleepagent_try_reserve_namespace_capacity(
  requested_namespace_id TEXT,
  requested_data_mode TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF NOT pg_try_advisory_xact_lock(hashtextextended(
    jsonb_build_array(
      requested_data_mode,
      requested_namespace_id
    )::text,
    0
  )) THEN
    RETURN FALSE;
  END IF;
  RETURN public.sleepagent_namespace_has_worker_capacity(
    requested_namespace_id,
    requested_data_mode
  );
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_namespace_last_claimed_at(TEXT, TEXT, TEXT)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION sleepagent_namespace_has_worker_capacity(TEXT, TEXT)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION sleepagent_try_reserve_namespace_capacity(TEXT, TEXT)
  FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_record_namespace_claim()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  target_queue TEXT;
BEGIN
  target_queue := CASE TG_TABLE_NAME
    WHEN 'sleep_domain_normalization_work' THEN 'ingestion'
    WHEN 'backend_demo_journeys' THEN 'replay_journey'
    WHEN 'sleep_domain_operations' THEN to_jsonb(NEW) ->> 'queue_name'
    WHEN 'backend_delivery_intents' THEN to_jsonb(NEW) ->> 'destination'
    WHEN 'backend_retention_jobs' THEN 'retention'
    ELSE NULL
  END;
  IF target_queue IS NULL OR target_queue = '' THEN
    RAISE EXCEPTION 'unknown namespace fairness queue';
  END IF;
  INSERT INTO public.backend_queue_namespace_fairness (
    queue_name, namespace_id, data_mode, claim_count, last_claimed_at
  ) VALUES (
    target_queue, NEW.namespace_id, NEW.data_mode, 1, clock_timestamp()
  )
  ON CONFLICT (queue_name, namespace_id, data_mode) DO UPDATE
  SET claim_count =
        public.backend_queue_namespace_fairness.claim_count + 1,
      last_claimed_at = EXCLUDED.last_claimed_at;
  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_record_namespace_claim() FROM PUBLIC;

CREATE TRIGGER sleep_domain_normalization_claim_fairness
AFTER UPDATE OF lease_generation ON sleep_domain_normalization_work
FOR EACH ROW WHEN (NEW.lease_generation > OLD.lease_generation)
EXECUTE FUNCTION sleepagent_record_namespace_claim();
CREATE TRIGGER backend_demo_journey_claim_fairness
AFTER UPDATE OF lease_generation ON backend_demo_journeys
FOR EACH ROW WHEN (NEW.lease_generation > OLD.lease_generation)
EXECUTE FUNCTION sleepagent_record_namespace_claim();
CREATE TRIGGER sleep_domain_operation_claim_fairness
AFTER UPDATE OF lease_generation ON sleep_domain_operations
FOR EACH ROW WHEN (NEW.lease_generation > OLD.lease_generation)
EXECUTE FUNCTION sleepagent_record_namespace_claim();
CREATE TRIGGER backend_delivery_claim_fairness
AFTER UPDATE OF lease_generation ON backend_delivery_intents
FOR EACH ROW WHEN (NEW.lease_generation > OLD.lease_generation)
EXECUTE FUNCTION sleepagent_record_namespace_claim();
CREATE TRIGGER backend_retention_claim_fairness
AFTER UPDATE OF lease_generation ON backend_retention_jobs
FOR EACH ROW WHEN (NEW.lease_generation > OLD.lease_generation)
EXECUTE FUNCTION sleepagent_record_namespace_claim();

-- Patch the exact release claim functions in-place. Each replacement is
-- guarded so a future source-shape drift fails the migration atomically.
DO $$
DECLARE
  definition TEXT;
  original TEXT;
BEGIN
  definition := pg_get_functiondef(
    'public.sleepagent_claim_normalization_work(text,integer)'::regprocedure
  );
  original := definition;
  definition := replace(definition,
    'AND namespace_row.status = ''active''',
    'AND namespace_row.status = ''active''' || chr(10)
      || '      AND public.sleepagent_try_reserve_namespace_capacity('
      || 'work.namespace_id, work.data_mode)');
  definition := replace(definition,
    'ORDER BY work.available_at,',
    'ORDER BY public.sleepagent_namespace_last_claimed_at('
      || '''ingestion'', work.namespace_id, work.data_mode),' || chr(10)
      || '      work.available_at,');
  IF definition = original
     OR position('sleepagent_try_reserve_namespace_capacity' IN definition) = 0
     OR position('sleepagent_namespace_last_claimed_at' IN definition) = 0 THEN
    RAISE EXCEPTION 'normalization fairness patch drifted';
  END IF;
  EXECUTE definition;

  definition := pg_get_functiondef(
    'public.sleepagent_claim_operation(text,text,integer)'::regprocedure
  );
  original := definition;
  definition := replace(definition,
    'AND ns.status = ''active''',
    'AND ns.status = ''active''' || chr(10)
      || '      AND public.sleepagent_try_reserve_namespace_capacity('
      || 'op.namespace_id, op.data_mode)');
  definition := replace(definition,
    'ORDER BY op.priority DESC, op.available_at, op.created_at, op.operation_id',
    'ORDER BY public.sleepagent_namespace_last_claimed_at('
      || 'requested_queue, op.namespace_id, op.data_mode),' || chr(10)
      || '      op.priority DESC, op.available_at, op.created_at, op.operation_id');
  IF definition = original
     OR position('sleepagent_try_reserve_namespace_capacity' IN definition) = 0
     OR position('sleepagent_namespace_last_claimed_at' IN definition) = 0 THEN
    RAISE EXCEPTION 'operation fairness patch drifted';
  END IF;
  EXECUTE definition;

  definition := pg_get_functiondef(
    'public.sleepagent_claim_delivery(text,text,integer)'::regprocedure
  );
  original := definition;
  definition := replace(definition,
    'AND namespace_row.status = ''active''',
    'AND namespace_row.status = ''active''' || chr(10)
      || '      AND public.sleepagent_try_reserve_namespace_capacity('
      || 'intent.namespace_id, intent.data_mode)');
  definition := replace(definition,
    'ORDER BY intent.priority DESC, intent.available_at,' || chr(10)
      || '      intent.created_at, intent.delivery_intent_id',
    'ORDER BY public.sleepagent_namespace_last_claimed_at('
      || 'requested_destination, intent.namespace_id, intent.data_mode),'
      || chr(10)
      || '      intent.priority DESC, intent.available_at,' || chr(10)
      || '      intent.created_at, intent.delivery_intent_id');
  IF definition = original
     OR position('sleepagent_try_reserve_namespace_capacity' IN definition) = 0
     OR position('sleepagent_namespace_last_claimed_at' IN definition) = 0 THEN
    RAISE EXCEPTION 'delivery fairness patch drifted';
  END IF;
  EXECUTE definition;

  definition := pg_get_functiondef(
    'public.sleepagent_claim_retention_job(text,integer)'::regprocedure
  );
  original := definition;
  definition := replace(definition,
    'AND ns.status = ''active''',
    'AND ns.status = ''active''' || chr(10)
      || '      AND public.sleepagent_try_reserve_namespace_capacity('
      || 'job.namespace_id, job.data_mode)');
  definition := replace(definition,
    'ORDER BY job.priority DESC, job.available_at, job.created_at,'
      || chr(10)
      || '      job.retention_job_id',
    'ORDER BY public.sleepagent_namespace_last_claimed_at('
      || '''retention'', job.namespace_id, job.data_mode),' || chr(10)
      || '      job.priority DESC, job.available_at, job.created_at,'
      || chr(10)
      || '      job.retention_job_id');
  IF definition = original
     OR position('sleepagent_try_reserve_namespace_capacity' IN definition) = 0
     OR position('sleepagent_namespace_last_claimed_at' IN definition) = 0 THEN
    RAISE EXCEPTION 'retention fairness patch drifted';
  END IF;
  EXECUTE definition;

  definition := pg_get_functiondef(
    'public.sleepagent_claim_demo_journey(text,integer)'::regprocedure
  );
  original := definition;
  definition := replace(definition,
    'FROM public.backend_demo_journeys AS journey' || chr(10)
      || '    JOIN public.backend_subject_epochs AS epoch_row',
    'FROM public.backend_demo_journeys AS journey' || chr(10)
      || '    JOIN public.backend_namespaces AS ns' || chr(10)
      || '      ON ns.namespace_id = journey.namespace_id' || chr(10)
      || '     AND ns.data_mode = journey.data_mode' || chr(10)
      || '    JOIN public.backend_subject_epochs AS epoch_row');
  definition := replace(definition,
    'AND journey.resume_at <= clock_timestamp()',
    'AND journey.resume_at <= clock_timestamp()' || chr(10)
      || '      AND public.sleepagent_try_reserve_namespace_capacity('
      || 'journey.namespace_id, journey.data_mode)');
  definition := replace(definition,
    'ORDER BY journey.resume_at, journey.created_at, journey.journey_id'
      || chr(10)
      || '    FOR UPDATE OF journey SKIP LOCKED',
    'ORDER BY public.sleepagent_namespace_last_claimed_at('
      || '''replay_journey'', journey.namespace_id, journey.data_mode),'
      || chr(10)
      || '      journey.resume_at, journey.created_at, journey.journey_id'
      || chr(10)
      || '    FOR UPDATE OF journey, ns SKIP LOCKED');
  IF definition = original
     OR position('sleepagent_try_reserve_namespace_capacity' IN definition) = 0
     OR position('sleepagent_namespace_last_claimed_at' IN definition) = 0
     OR position('FOR UPDATE OF journey, ns SKIP LOCKED' IN definition) = 0 THEN
    RAISE EXCEPTION 'journey fairness patch drifted';
  END IF;
  EXECUTE definition;
END;
$$;
