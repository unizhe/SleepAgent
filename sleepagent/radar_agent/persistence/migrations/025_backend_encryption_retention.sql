-- sleepagent:transactional=true
-- Envelope-key metadata, retention work and auditable crypto-shred receipts.

CREATE TABLE IF NOT EXISTS backend_retention_classes (
  retention_class TEXT PRIMARY KEY,
  policy_version TEXT NOT NULL,
  retention_seconds BIGINT NOT NULL CHECK (retention_seconds > 0),
  expiry_action TEXT NOT NULL CHECK (
    expiry_action IN ('expire', 'crypto_shred', 'delete_projection', 'retain_audit')
  ),
  legal_hold_supported BOOLEAN NOT NULL DEFAULT FALSE,
  configuration_sha256 TEXT NOT NULL CHECK (
    configuration_sha256 ~ '^[0-9a-f]{64}$'
  ),
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS backend_retention_deks (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  retention_domain TEXT NOT NULL CHECK (
    retention_domain IN (
      'raw', 'conversation_checkpoint', 'personalized_memory_profile',
      'export_cache', 'audit_pseudonym'
    )
  ),
  generation BIGINT NOT NULL CHECK (generation >= 1),
  kek_key_id TEXT NOT NULL,
  wrapping_algorithm TEXT NOT NULL,
  wrapped_dek BYTEA,
  dek_sha256 TEXT NOT NULL CHECK (dek_sha256 ~ '^[0-9a-f]{64}$'),
  status TEXT NOT NULL CHECK (status IN ('active', 'retired', 'shredded')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  retired_at TIMESTAMPTZ,
  shredded_at TIMESTAMPTZ,
  PRIMARY KEY (
    namespace_id, data_mode, subject_id, retention_domain, generation
  ),
  FOREIGN KEY (namespace_id, data_mode, subject_id)
    REFERENCES backend_subjects (namespace_id, data_mode, subject_id)
    ON DELETE RESTRICT,
  CHECK (
    (status IN ('active', 'retired') AND wrapped_dek IS NOT NULL
      AND shredded_at IS NULL)
    OR (status = 'shredded' AND wrapped_dek IS NULL
      AND shredded_at IS NOT NULL)
  ),
  CHECK (retired_at IS NULL OR retired_at >= created_at),
  CHECK (shredded_at IS NULL OR shredded_at >= created_at),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_backend_retention_dek_active
  ON backend_retention_deks (
    namespace_id, data_mode, subject_id, retention_domain
  )
  WHERE status = 'active';

CREATE TABLE IF NOT EXISTS backend_retention_bindings (
  retention_binding_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  retention_domain TEXT NOT NULL,
  dek_generation BIGINT NOT NULL CHECK (dek_generation >= 1),
  retention_class TEXT NOT NULL
    REFERENCES backend_retention_classes(retention_class) ON DELETE RESTRICT,
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  legal_hold BOOLEAN NOT NULL DEFAULT FALSE,
  legal_hold_reason TEXT,
  binding_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (namespace_id, data_mode, resource_type, resource_id),
  FOREIGN KEY (
    namespace_id, data_mode, subject_id, retention_domain, dek_generation
  ) REFERENCES backend_retention_deks (
    namespace_id, data_mode, subject_id, retention_domain, generation
  ) ON DELETE RESTRICT,
  CHECK (
    (legal_hold = FALSE AND legal_hold_reason IS NULL)
    OR (legal_hold = TRUE AND legal_hold_reason IS NOT NULL)
  ),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_backend_retention_due
  ON backend_retention_bindings (
    namespace_id, data_mode, legal_hold, expires_at, retention_binding_id
  );

CREATE TABLE IF NOT EXISTS backend_retention_jobs (
  retention_job_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  retention_domain TEXT NOT NULL,
  dek_generation BIGINT NOT NULL CHECK (dek_generation >= 1),
  semantic_key TEXT NOT NULL,
  job_kind TEXT NOT NULL CHECK (
    job_kind IN ('scheduled_expiry', 'subject_forget', 'key_rotation')
  ),
  status TEXT NOT NULL CHECK (
    status IN (
      'pending', 'running', 'retry', 'succeeded', 'failed',
      'dead_letter', 'reconciliation_required'
    )
  ),
  priority INTEGER NOT NULL DEFAULT 0,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts INTEGER NOT NULL DEFAULT 8 CHECK (max_attempts >= 1),
  available_at TIMESTAMPTZ NOT NULL,
  lease_generation BIGINT NOT NULL DEFAULT 0,
  fencing_token TEXT,
  worker_instance TEXT,
  heartbeat_at TIMESTAMPTZ,
  lease_expires_at TIMESTAMPTZ,
  authorization_snapshot_json JSONB NOT NULL CHECK (
    jsonb_typeof(authorization_snapshot_json) = 'object'
    AND authorization_snapshot_json ?& ARRAY[
      'authorization_epoch', 'privacy_epoch', 'retrieval_policy_epoch'
    ]
  ),
  job_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (namespace_id, data_mode, semantic_key),
  UNIQUE (
    retention_job_id, namespace_id, data_mode, subject_id,
    retention_domain, dek_generation
  ),
  FOREIGN KEY (
    namespace_id, data_mode, subject_id, retention_domain, dek_generation
  ) REFERENCES backend_retention_deks (
    namespace_id, data_mode, subject_id, retention_domain, generation
  ) ON DELETE RESTRICT,
  CHECK (
    status <> 'running'
    OR (lease_generation >= 1 AND fencing_token IS NOT NULL
      AND worker_instance IS NOT NULL AND lease_expires_at IS NOT NULL)
  ),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_backend_retention_job_claim
  ON backend_retention_jobs (
    data_mode, status, priority DESC, available_at, created_at
  );

CREATE TABLE IF NOT EXISTS backend_shred_receipts (
  shred_receipt_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  subject_pseudonym TEXT NOT NULL,
  retention_job_id TEXT NOT NULL,
  retention_domain TEXT NOT NULL,
  dek_generation BIGINT NOT NULL CHECK (dek_generation >= 1),
  lease_generation BIGINT NOT NULL CHECK (lease_generation >= 1),
  fencing_token TEXT NOT NULL,
  policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  destroyed_json JSONB NOT NULL,
  expired_json JSONB NOT NULL,
  retained_with_reason_json JSONB NOT NULL,
  receipt_json JSONB NOT NULL,
  completed_at TIMESTAMPTZ NOT NULL,
  UNIQUE (
    namespace_id, data_mode, subject_id, retention_domain, dek_generation
  ),
  FOREIGN KEY (
    retention_job_id, namespace_id, data_mode, subject_id,
    retention_domain, dek_generation
  ) REFERENCES backend_retention_jobs (
    retention_job_id, namespace_id, data_mode, subject_id,
    retention_domain, dek_generation
  ) ON DELETE RESTRICT,
  CHECK (jsonb_typeof(destroyed_json) = 'array'),
  CHECK (jsonb_typeof(expired_json) = 'array'),
  CHECK (jsonb_typeof(retained_with_reason_json) = 'array'),
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS backend_retention_events (
  retention_event_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  retention_job_id TEXT NOT NULL,
  retention_domain TEXT NOT NULL,
  dek_generation BIGINT NOT NULL CHECK (dek_generation >= 1),
  sequence BIGINT NOT NULL CHECK (sequence >= 1),
  event_type TEXT NOT NULL,
  lease_generation BIGINT NOT NULL CHECK (lease_generation >= 1),
  fencing_token TEXT NOT NULL,
  event_json JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  UNIQUE (retention_job_id, sequence),
  FOREIGN KEY (
    retention_job_id, namespace_id, data_mode, subject_id,
    retention_domain, dek_generation
  ) REFERENCES backend_retention_jobs (
    retention_job_id, namespace_id, data_mode, subject_id,
    retention_domain, dek_generation
  ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

ALTER TABLE backend_retention_deks
  ADD CONSTRAINT backend_retention_dek_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT;
ALTER TABLE backend_retention_deks
  ADD CONSTRAINT backend_retention_dek_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT;
ALTER TABLE backend_retention_jobs
  ADD CONSTRAINT backend_retention_job_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT;
ALTER TABLE backend_retention_jobs
  ADD CONSTRAINT backend_retention_job_arm_fk
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION sleepagent_retention_fence_allows(
  target_retention_job_id TEXT,
  target_namespace_id TEXT,
  target_data_mode TEXT,
  target_namespace_generation BIGINT,
  target_run_id TEXT,
  target_arm_id TEXT,
  target_subject_id TEXT,
  target_retention_domain TEXT,
  target_dek_generation BIGINT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT
    public.sleepagent_principal_context_allows()
    AND NULLIF(current_setting('sleepagent.process_role', TRUE), '') =
      'worker'
    AND public.sleepagent_subject_generation_scope_allows(
      target_namespace_id, target_data_mode, target_subject_id,
      target_namespace_generation, target_run_id, target_arm_id
    )
    AND EXISTS (
      SELECT 1
      FROM public.backend_retention_jobs AS job
      WHERE job.retention_job_id = target_retention_job_id
        AND job.namespace_id = target_namespace_id
        AND job.data_mode = target_data_mode
        AND job.namespace_generation = target_namespace_generation
        AND job.run_id IS NOT DISTINCT FROM target_run_id
        AND job.arm_id IS NOT DISTINCT FROM target_arm_id
        AND job.subject_id = target_subject_id
        AND job.retention_domain = target_retention_domain
        AND job.dek_generation = target_dek_generation
        AND job.status = 'running'
        AND job.lease_generation = expected_lease_generation
        AND job.fencing_token = expected_fencing_token
        AND job.worker_instance = NULLIF(
          current_setting('sleepagent.worker_instance', TRUE), ''
        )
        AND job.lease_expires_at > clock_timestamp()
        AND job.authorization_snapshot_json ->> 'authorization_epoch' =
          NULLIF(current_setting(
            'sleepagent.authorization_epoch', TRUE
          ), '')
        AND job.authorization_snapshot_json ->> 'privacy_epoch' =
          NULLIF(current_setting('sleepagent.privacy_epoch', TRUE), '')
        AND job.authorization_snapshot_json ->> 'retrieval_policy_epoch' =
          NULLIF(current_setting(
            'sleepagent.retrieval_policy_epoch', TRUE
          ), '')
    )
$$;

REVOKE ALL ON FUNCTION sleepagent_retention_fence_allows(
  TEXT, TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT, BIGINT, BIGINT, TEXT
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_protect_retention_dek()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'retention DEK metadata cannot be deleted';
  END IF;
  IF NEW.namespace_id <> OLD.namespace_id
     OR NEW.data_mode <> OLD.data_mode
     OR NEW.subject_id <> OLD.subject_id
     OR NEW.retention_domain <> OLD.retention_domain
     OR NEW.generation <> OLD.generation
     OR NEW.dek_sha256 <> OLD.dek_sha256
     OR NEW.created_at <> OLD.created_at THEN
    RAISE EXCEPTION 'retention DEK identity is immutable';
  END IF;
  IF OLD.status = 'shredded' THEN
    RAISE EXCEPTION 'shredded retention DEK metadata is terminal';
  END IF;
  IF OLD.status = 'retired' AND NEW.status NOT IN ('retired', 'shredded') THEN
    RAISE EXCEPTION 'retired retention DEK may only transition to shredded';
  END IF;
  IF OLD.status = 'active'
     AND NEW.status NOT IN ('active', 'retired', 'shredded') THEN
    RAISE EXCEPTION 'invalid retention DEK state transition';
  END IF;
  IF NEW.status = 'shredded' AND OLD.status <> 'shredded'
     AND NOT EXISTS (
       SELECT 1
       FROM public.backend_shred_receipts AS receipt
       WHERE receipt.namespace_id = NEW.namespace_id
         AND receipt.data_mode = NEW.data_mode
         AND receipt.namespace_generation = NEW.namespace_generation
         AND receipt.run_id IS NOT DISTINCT FROM NEW.run_id
         AND receipt.arm_id IS NOT DISTINCT FROM NEW.arm_id
         AND receipt.subject_id = NEW.subject_id
         AND receipt.retention_domain = NEW.retention_domain
         AND receipt.dek_generation = NEW.generation
         AND public.sleepagent_retention_fence_allows(
           receipt.retention_job_id, receipt.namespace_id,
           receipt.data_mode, receipt.namespace_generation,
           receipt.run_id, receipt.arm_id, receipt.subject_id,
           receipt.retention_domain, receipt.dek_generation,
           receipt.lease_generation, receipt.fencing_token
         )
     ) THEN
    RAISE EXCEPTION
      'retention DEK shred requires a current fenced job and receipt';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS backend_retention_dek_state_guard
  ON backend_retention_deks;
CREATE TRIGGER backend_retention_dek_state_guard
BEFORE UPDATE OR DELETE ON backend_retention_deks
FOR EACH ROW EXECUTE FUNCTION sleepagent_protect_retention_dek();

CREATE OR REPLACE FUNCTION sleepagent_enforce_shred_receipt_fence()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF NOT public.sleepagent_retention_fence_allows(
    NEW.retention_job_id, NEW.namespace_id, NEW.data_mode,
    NEW.namespace_generation, NEW.run_id, NEW.arm_id, NEW.subject_id,
    NEW.retention_domain, NEW.dek_generation, NEW.lease_generation,
    NEW.fencing_token
  ) THEN
    RAISE EXCEPTION 'stale or unauthorized retention work fence';
  END IF;
  IF TG_TABLE_NAME = 'backend_shred_receipts' THEN
    NEW.completed_at := clock_timestamp();
  ELSIF TG_TABLE_NAME = 'backend_retention_events' THEN
    NEW.occurred_at := clock_timestamp();
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS backend_shred_receipt_fence
  ON backend_shred_receipts;
CREATE TRIGGER backend_shred_receipt_fence
BEFORE INSERT ON backend_shred_receipts
FOR EACH ROW EXECUTE FUNCTION sleepagent_enforce_shred_receipt_fence();

DROP TRIGGER IF EXISTS backend_retention_event_fence
  ON backend_retention_events;
CREATE TRIGGER backend_retention_event_fence
BEFORE INSERT ON backend_retention_events
FOR EACH ROW EXECUTE FUNCTION sleepagent_enforce_shred_receipt_fence();

DROP TRIGGER IF EXISTS backend_shred_receipt_immutable
  ON backend_shred_receipts;
CREATE TRIGGER backend_shred_receipt_immutable
BEFORE UPDATE OR DELETE ON backend_shred_receipts
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

DROP TRIGGER IF EXISTS backend_retention_event_immutable
  ON backend_retention_events;
CREATE TRIGGER backend_retention_event_immutable
BEFORE UPDATE OR DELETE ON backend_retention_events
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

-- Existing encrypted payloads/manifests remain protocol v1.  New production
-- encryption records exact retention domain and DEK generation.
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN encryption_protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN retention_subject_id TEXT;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN retention_domain TEXT;
ALTER TABLE sleep_domain_raw_inbox
  ADD COLUMN dek_generation BIGINT;
ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_encryption_v2_contract CHECK (
    encryption_protocol_version < 2 OR (
      retention_subject_id IS NOT NULL
      AND retention_domain = 'raw'
      AND dek_generation >= 1
    )
  ) NOT VALID;

ALTER TABLE sleep_domain_raw_inbox
  ADD CONSTRAINT sleep_domain_raw_encryption_v2_dek_fk
  FOREIGN KEY (
    namespace_id, data_mode, retention_subject_id,
    retention_domain, dek_generation
  ) REFERENCES backend_retention_deks (
    namespace_id, data_mode, subject_id, retention_domain, generation
  ) ON DELETE RESTRICT NOT VALID;

ALTER TABLE product_induction_manifests
  ADD COLUMN IF NOT EXISTS encryption_protocol_version INTEGER
    NOT NULL DEFAULT 1;
ALTER TABLE product_induction_manifests
  ADD COLUMN IF NOT EXISTS retention_domain TEXT;
ALTER TABLE product_induction_manifests
  ADD COLUMN IF NOT EXISTS dek_generation BIGINT;
ALTER TABLE product_induction_manifests
  ADD CONSTRAINT product_induction_manifest_encryption_v2_contract CHECK (
    encryption_protocol_version < 2 OR (
      namespace_id IS NOT NULL
      AND data_mode IS NOT NULL
      AND retention_domain = 'personalized_memory_profile'
      AND dek_generation >= 1
      AND wrapped_data_key IS NOT NULL
      AND key_id IS NOT NULL
    )
  ) NOT VALID;

ALTER TABLE product_induction_manifests
  ADD CONSTRAINT product_induction_manifest_encryption_v2_dek_fk
  FOREIGN KEY (
    namespace_id, data_mode, subject_id, retention_domain, dek_generation
  ) REFERENCES backend_retention_deks (
    namespace_id, data_mode, subject_id, retention_domain, generation
  ) ON DELETE RESTRICT NOT VALID;

CREATE OR REPLACE FUNCTION sleepagent_claim_retention_job(
  claimant_worker_instance TEXT,
  requested_lease_seconds INTEGER
)
RETURNS TABLE (
  retention_job_id TEXT,
  namespace_id TEXT,
  data_mode TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT,
  retention_domain TEXT,
  dek_generation BIGINT,
  authorization_epoch BIGINT,
  privacy_epoch BIGINT,
  retrieval_policy_epoch BIGINT,
  lease_generation BIGINT,
  fencing_token TEXT
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
  caller_process_role TEXT := NULLIF(
    current_setting('sleepagent.process_role', TRUE), ''
  );
BEGIN
  IF principal IS NULL OR deployment_mode NOT IN ('live', 'replay')
     OR request_purpose IS NULL OR caller_process_role <> 'worker'
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION
      'trusted retention principal/data mode/purpose context is required';
  END IF;
  IF claimant_worker_instance IS NULL OR claimant_worker_instance = ''
     OR requested_lease_seconds < 1 OR requested_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'invalid retention claim parameters';
  END IF;

  RETURN QUERY
  WITH candidate AS (
    SELECT job.retention_job_id,
      epoch_row.authorization_epoch AS claim_authorization_epoch,
      epoch_row.privacy_epoch AS claim_privacy_epoch,
      epoch_row.retrieval_policy_epoch AS claim_retrieval_policy_epoch
    FROM public.backend_retention_jobs AS job
    JOIN public.backend_namespaces AS ns
     ON ns.namespace_id = job.namespace_id
     AND ns.data_mode = job.data_mode
    JOIN public.backend_subject_epochs AS epoch_row
      ON epoch_row.namespace_id = job.namespace_id
     AND epoch_row.data_mode = job.data_mode
     AND epoch_row.subject_id = job.subject_id
    WHERE job.data_mode = deployment_mode
      AND (
        job.status IN ('pending', 'retry')
        OR (job.status = 'running'
          AND job.lease_expires_at <= clock_timestamp())
      )
      AND job.available_at <= clock_timestamp()
      AND job.attempt_count < job.max_attempts
      AND ns.status = 'active'
      AND job.authorization_snapshot_json ->> 'authorization_epoch' =
        epoch_row.authorization_epoch::text
      AND job.authorization_snapshot_json ->> 'privacy_epoch' =
        epoch_row.privacy_epoch::text
      AND job.authorization_snapshot_json ->> 'retrieval_policy_epoch' =
        epoch_row.retrieval_policy_epoch::text
      AND EXISTS (
        SELECT 1
        FROM public.backend_principal_grants AS grant_row
        WHERE grant_row.principal_id = principal
          AND grant_row.namespace_id = job.namespace_id
          AND grant_row.data_mode = job.data_mode
          AND grant_row.purpose = request_purpose
          AND grant_row.authorization_epoch =
            epoch_row.authorization_epoch
          AND grant_row.status = 'active'
          AND grant_row.valid_from <= clock_timestamp()
          AND (
            grant_row.valid_until IS NULL
            OR grant_row.valid_until > clock_timestamp()
          )
          AND grant_row.allowed_handlers_json ? 'retention'
          AND EXISTS (
            SELECT 1
            FROM public.backend_service_principals AS service_principal
            WHERE service_principal.principal_id = principal
              AND service_principal.status = 'active'
          )
      )
    ORDER BY job.priority DESC, job.available_at, job.created_at,
      job.retention_job_id
    FOR UPDATE OF ns, job SKIP LOCKED
    LIMIT 1
  ), claimed AS (
    UPDATE public.backend_retention_jobs AS job
    SET status = 'running',
        attempt_count = job.attempt_count + 1,
        lease_generation = job.lease_generation + 1,
        fencing_token = gen_random_uuid()::text,
        worker_instance = claimant_worker_instance,
        heartbeat_at = clock_timestamp(),
        lease_expires_at = clock_timestamp()
          + make_interval(secs => requested_lease_seconds),
        updated_at = clock_timestamp()
    FROM candidate
    WHERE job.retention_job_id = candidate.retention_job_id
    RETURNING job.retention_job_id, job.namespace_id, job.data_mode,
      job.namespace_generation, job.run_id, job.arm_id, job.subject_id,
      job.retention_domain, job.dek_generation,
      candidate.claim_authorization_epoch, candidate.claim_privacy_epoch,
      candidate.claim_retrieval_policy_epoch, job.lease_generation,
      job.fencing_token
  )
  SELECT claimed.retention_job_id, claimed.namespace_id, claimed.data_mode,
    claimed.namespace_generation, claimed.run_id, claimed.arm_id,
    claimed.subject_id, claimed.retention_domain, claimed.dek_generation,
    claimed.claim_authorization_epoch, claimed.claim_privacy_epoch,
    claimed.claim_retrieval_policy_epoch,
    claimed.lease_generation, claimed.fencing_token
  FROM claimed;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_claim_retention_job(TEXT, INTEGER)
  FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_heartbeat_retention_job(
  target_retention_job_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_lease_seconds INTEGER
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_lease_seconds < 1 OR requested_lease_seconds > 3600 THEN
    RAISE EXCEPTION 'invalid retention heartbeat lease duration';
  END IF;
  UPDATE public.backend_retention_jobs
  SET heartbeat_at = clock_timestamp(),
      lease_expires_at = clock_timestamp()
        + make_interval(secs => requested_lease_seconds),
      updated_at = clock_timestamp()
  WHERE retention_job_id = target_retention_job_id
    AND status = 'running'
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp();
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_heartbeat_retention_job(
  TEXT, BIGINT, TEXT, INTEGER
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION sleepagent_finalize_retention_job(
  target_retention_job_id TEXT,
  expected_lease_generation BIGINT,
  expected_fencing_token TEXT,
  requested_final_status TEXT,
  requested_next_available_at TIMESTAMPTZ
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  changed INTEGER;
BEGIN
  IF requested_final_status NOT IN (
    'succeeded', 'retry', 'failed', 'dead_letter',
    'reconciliation_required'
  ) OR (requested_final_status = 'retry'
    AND requested_next_available_at IS NULL) THEN
    RAISE EXCEPTION 'invalid retention final status';
  END IF;
  UPDATE public.backend_retention_jobs
  SET status = requested_final_status,
      available_at = COALESCE(requested_next_available_at, available_at),
      lease_expires_at = NULL,
      worker_instance = NULL,
      updated_at = clock_timestamp()
  WHERE retention_job_id = target_retention_job_id
    AND status = 'running'
    AND lease_generation = expected_lease_generation
    AND fencing_token = expected_fencing_token
    AND worker_instance = NULLIF(
      current_setting('sleepagent.worker_instance', TRUE), ''
    )
    AND public.sleepagent_principal_context_allows()
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
    AND lease_expires_at > clock_timestamp()
    AND authorization_snapshot_json ->> 'authorization_epoch' = NULLIF(
      current_setting('sleepagent.authorization_epoch', TRUE), ''
    )
    AND authorization_snapshot_json ->> 'privacy_epoch' = NULLIF(
      current_setting('sleepagent.privacy_epoch', TRUE), ''
    )
    AND authorization_snapshot_json ->> 'retrieval_policy_epoch' = NULLIF(
      current_setting('sleepagent.retrieval_policy_epoch', TRUE), ''
    );
  GET DIAGNOSTICS changed = ROW_COUNT;
  RETURN changed = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_finalize_retention_job(
  TEXT, BIGINT, TEXT, TEXT, TIMESTAMPTZ
) FROM PUBLIC;

ALTER TABLE backend_retention_deks ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_retention_deks FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_retention_dek_scope ON backend_retention_deks
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_retention_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_retention_bindings FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_retention_binding_scope ON backend_retention_bindings
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_retention_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_retention_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_retention_job_scope ON backend_retention_jobs
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_shred_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_shred_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_shred_receipt_scope ON backend_shred_receipts
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_retention_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_retention_events FORCE ROW LEVEL SECURITY;
CREATE POLICY backend_retention_event_scope ON backend_retention_events
  USING (sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (
    sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );
