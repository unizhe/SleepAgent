-- sleepagent:transactional=true
-- P4-D2-B2: activate the provider-neutral Pull evidence and reconciliation
-- tables, harden them as exact subject-generation scoped evidence, and expose
-- one authenticated Perceptor Platform API response ingress function.  The
-- function never accepts the token endpoint and never emits Product/Agent work.

-- Evidence hashes and source channels are canonical lower-case SHA-256 and the
-- two frozen provenance authorities.  NOT VALID preserves any legacy rows while
-- enforcing the contract for every new row.
ALTER TABLE sleep_domain_source_reports
  ADD CONSTRAINT sleep_domain_source_report_sha256_v1 CHECK (
    content_sha256 ~ '^[0-9a-f]{64}$'
  ) NOT VALID;

ALTER TABLE sleep_domain_observation_fact_values
  ADD CONSTRAINT sleep_domain_fact_value_sha256_v1 CHECK (
    value_sha256 ~ '^[0-9a-f]{64}$'
  ) NOT VALID;

ALTER TABLE sleep_domain_observation_acquisitions
  ADD CONSTRAINT sleep_domain_acquisition_sha256_v1 CHECK (
    value_sha256 ~ '^[0-9a-f]{64}$'
  ) NOT VALID;
ALTER TABLE sleep_domain_observation_acquisitions
  ADD CONSTRAINT sleep_domain_acquisition_channel_v1 CHECK (
    acquisition_channel IN ('PUSH', 'PULL')
  ) NOT VALID;

ALTER TABLE sleep_domain_observation_conflicts
  ADD CONSTRAINT sleep_domain_conflict_sha256_v1 CHECK (
    first_value_sha256 ~ '^[0-9a-f]{64}$'
    AND second_value_sha256 ~ '^[0-9a-f]{64}$'
  ) NOT VALID;
ALTER TABLE sleep_domain_observation_conflicts
  ADD CONSTRAINT sleep_domain_conflict_distinct_observation_v1 CHECK (
    first_observation_id <> second_observation_id
  ) NOT VALID;

-- Composite ownership keys prevent cross-tenant/cross-raw references.  An
-- acquisition deliberately does not require its candidate to be the canonical
-- observation's first candidate: a later PUSH/PULL candidate may corroborate
-- the same fact without creating another product-facing observation.
CREATE UNIQUE INDEX ux_sleep_domain_candidate_exact_raw
  ON sleep_domain_adapter_candidates (
    candidate_id, namespace_id, data_mode, raw_ingress_record_id
  );

CREATE UNIQUE INDEX ux_sleep_domain_observation_exact_candidate
  ON sleep_domain_canonical_observations (
    observation_id, namespace_id, data_mode, candidate_id
  );

CREATE UNIQUE INDEX ux_sleep_domain_observation_exact_subject
  ON sleep_domain_canonical_observations (
    observation_id, namespace_id, data_mode, subject_id
  );

CREATE UNIQUE INDEX ux_sleep_domain_fact_value_exact_observation
  ON sleep_domain_observation_fact_values (
    namespace_id, data_mode, fact_slot_key, value_sha256, observation_id
  );

CREATE UNIQUE INDEX ux_sleep_domain_provider_account_exact_provider
  ON sleep_domain_provider_accounts (
    namespace_id, data_mode, provider_account_id, provider_id
  );

ALTER TABLE sleep_domain_source_reports
  ADD CONSTRAINT sleep_domain_source_report_account_fk
  FOREIGN KEY (
    namespace_id, data_mode, provider_account_id, provider_id
  )
  REFERENCES sleep_domain_provider_accounts (
    namespace_id, data_mode, provider_account_id, provider_id
  ) ON DELETE RESTRICT NOT VALID;

ALTER TABLE sleep_domain_observation_fact_values
  ADD CONSTRAINT sleep_domain_fact_value_exact_candidate_fk
  FOREIGN KEY (observation_id, namespace_id, data_mode, candidate_id)
  REFERENCES sleep_domain_canonical_observations (
    observation_id, namespace_id, data_mode, candidate_id
  ) ON DELETE RESTRICT NOT VALID;

ALTER TABLE sleep_domain_observation_acquisitions
  ADD CONSTRAINT sleep_domain_acquisition_exact_candidate_raw_fk
  FOREIGN KEY (
    candidate_id, namespace_id, data_mode, raw_ingress_record_id
  ) REFERENCES sleep_domain_adapter_candidates (
    candidate_id, namespace_id, data_mode, raw_ingress_record_id
  ) ON DELETE RESTRICT NOT VALID;

ALTER TABLE sleep_domain_observation_acquisitions
  ADD CONSTRAINT sleep_domain_acquisition_exact_fact_fk
  FOREIGN KEY (
    namespace_id, data_mode, fact_slot_key, value_sha256, observation_id
  ) REFERENCES sleep_domain_observation_fact_values (
    namespace_id, data_mode, fact_slot_key, value_sha256, observation_id
  ) ON DELETE RESTRICT NOT VALID;

ALTER TABLE sleep_domain_observation_conflicts
  ADD CONSTRAINT sleep_domain_conflict_first_observation_fk
  FOREIGN KEY (first_observation_id, namespace_id, data_mode)
  REFERENCES sleep_domain_canonical_observations (
    observation_id, namespace_id, data_mode
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_observation_conflicts
  ADD CONSTRAINT sleep_domain_conflict_second_observation_fk
  FOREIGN KEY (second_observation_id, namespace_id, data_mode)
  REFERENCES sleep_domain_canonical_observations (
    observation_id, namespace_id, data_mode
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_observation_conflicts
  ADD CONSTRAINT sleep_domain_conflict_first_exact_fact_fk
  FOREIGN KEY (
    namespace_id, data_mode, fact_slot_key,
    first_value_sha256, first_observation_id
  ) REFERENCES sleep_domain_observation_fact_values (
    namespace_id, data_mode, fact_slot_key, value_sha256, observation_id
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_observation_conflicts
  ADD CONSTRAINT sleep_domain_conflict_second_exact_fact_fk
  FOREIGN KEY (
    namespace_id, data_mode, fact_slot_key,
    second_value_sha256, second_observation_id
  ) REFERENCES sleep_domain_observation_fact_values (
    namespace_id, data_mode, fact_slot_key, value_sha256, observation_id
  ) ON DELETE RESTRICT NOT VALID;

CREATE INDEX idx_sleep_domain_source_report_raw
  ON sleep_domain_source_reports (
    namespace_id, data_mode, raw_ingress_record_id, fetched_at
  );
CREATE INDEX idx_sleep_domain_fact_value_slot
  ON sleep_domain_observation_fact_values (
    namespace_id, data_mode, fact_slot_key, value_sha256, observation_id
  );
CREATE INDEX idx_sleep_domain_acquisition_fact_channel
  ON sleep_domain_observation_acquisitions (
    namespace_id, data_mode, fact_slot_key, value_sha256,
    acquisition_channel, acquired_at
  );
CREATE INDEX idx_sleep_domain_acquisition_observation
  ON sleep_domain_observation_acquisitions (
    namespace_id, data_mode, observation_id, acquired_at, acquisition_id
  );
CREATE INDEX idx_sleep_domain_conflict_first_observation
  ON sleep_domain_observation_conflicts (
    namespace_id, data_mode, first_observation_id, detected_at
  );
CREATE INDEX idx_sleep_domain_conflict_second_observation
  ON sleep_domain_observation_conflicts (
    namespace_id, data_mode, second_observation_id, detected_at
  );

DROP TRIGGER IF EXISTS sleep_domain_source_report_immutable
  ON sleep_domain_source_reports;
CREATE TRIGGER sleep_domain_source_report_immutable
BEFORE UPDATE OR DELETE ON sleep_domain_source_reports
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

DROP TRIGGER IF EXISTS sleep_domain_fact_value_immutable
  ON sleep_domain_observation_fact_values;
CREATE TRIGGER sleep_domain_fact_value_immutable
BEFORE UPDATE OR DELETE ON sleep_domain_observation_fact_values
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

DROP TRIGGER IF EXISTS sleep_domain_acquisition_immutable
  ON sleep_domain_observation_acquisitions;
CREATE TRIGGER sleep_domain_acquisition_immutable
BEFORE UPDATE OR DELETE ON sleep_domain_observation_acquisitions
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

DROP TRIGGER IF EXISTS sleep_domain_conflict_immutable
  ON sleep_domain_observation_conflicts;
CREATE TRIGGER sleep_domain_conflict_immutable
BEFORE UPDATE OR DELETE ON sleep_domain_observation_conflicts
FOR EACH ROW EXECUTE FUNCTION sleepagent_reject_append_only_mutation();

-- Pull checkpoint v2 is one exact live subject/binding/data-surface cursor.
-- It records the last raw/work commit fence and advances only after canonical
-- reconciliation (including a valid no-data commit) has completed.
ALTER TABLE sleep_domain_pull_checkpoints
  ADD COLUMN protocol_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD COLUMN namespace_generation BIGINT;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD COLUMN run_id TEXT;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD COLUMN arm_id TEXT;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD COLUMN subject_id TEXT;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD COLUMN device_binding_id TEXT;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD COLUMN binding_version INTEGER;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD COLUMN data_surface TEXT;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD COLUMN overlap_seconds INTEGER;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD COLUMN last_raw_ingress_record_id TEXT;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD COLUMN last_normalization_work_id TEXT;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD COLUMN last_canonical_observation_id TEXT;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD COLUMN last_canonical_commit_at TIMESTAMPTZ;

ALTER TABLE sleep_domain_pull_checkpoints
  ADD CONSTRAINT sleep_domain_pull_checkpoint_v2_contract CHECK (
    protocol_version < 2 OR (
      data_mode = 'live'
      AND namespace_generation >= 1
      AND run_id IS NULL
      AND arm_id IS NULL
      AND subject_id IS NOT NULL
      AND provider_id = 'perceptor'
      AND device_binding_id IS NOT NULL
      AND binding_version >= 1
      AND data_surface IN (
        'current', 'realtime', 'history', 'sleep_report'
      )
      AND overlap_seconds = CASE
        WHEN data_surface = 'history' THEN 3
        ELSE 0
      END
      AND lateness_watermark_at =
        cursor_at - (overlap_seconds * INTERVAL '1 second')
      AND last_raw_ingress_record_id IS NOT NULL
      AND last_normalization_work_id IS NOT NULL
      AND last_canonical_commit_at IS NOT NULL
      AND last_canonical_commit_at <= updated_at
    )
  ) NOT VALID;

CREATE UNIQUE INDEX ux_sleep_domain_binding_exact_authority
  ON sleep_domain_device_bindings (
    device_binding_id, namespace_id, data_mode, binding_version,
    subject_id, provider_id, provider_account_id
  );

CREATE UNIQUE INDEX ux_sleep_domain_raw_exact_generation
  ON sleep_domain_raw_inbox (
    raw_ingress_record_id, namespace_id, data_mode, subject_id,
    namespace_generation
  );

CREATE UNIQUE INDEX ux_sleep_domain_work_exact_raw_generation
  ON sleep_domain_normalization_work (
    work_id, namespace_id, data_mode, raw_ingress_record_id,
    subject_id, namespace_generation
  );

ALTER TABLE sleep_domain_pull_checkpoints
  ADD CONSTRAINT sleep_domain_pull_checkpoint_generation_fk
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD CONSTRAINT sleep_domain_pull_checkpoint_account_fk
  FOREIGN KEY (
    namespace_id, data_mode, provider_account_id, provider_id
  )
  REFERENCES sleep_domain_provider_accounts (
    namespace_id, data_mode, provider_account_id, provider_id
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD CONSTRAINT sleep_domain_pull_checkpoint_binding_fk
  FOREIGN KEY (
    device_binding_id, namespace_id, data_mode, binding_version,
    subject_id, provider_id, provider_account_id
  ) REFERENCES sleep_domain_device_bindings (
    device_binding_id, namespace_id, data_mode, binding_version,
    subject_id, provider_id, provider_account_id
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD CONSTRAINT sleep_domain_pull_checkpoint_raw_fk
  FOREIGN KEY (
    last_raw_ingress_record_id, namespace_id, data_mode, subject_id,
    namespace_generation
  ) REFERENCES sleep_domain_raw_inbox (
    raw_ingress_record_id, namespace_id, data_mode, subject_id,
    namespace_generation
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD CONSTRAINT sleep_domain_pull_checkpoint_work_fk
  FOREIGN KEY (
    last_normalization_work_id, namespace_id, data_mode,
    last_raw_ingress_record_id, subject_id, namespace_generation
  ) REFERENCES sleep_domain_normalization_work (
    work_id, namespace_id, data_mode, raw_ingress_record_id,
    subject_id, namespace_generation
  ) ON DELETE RESTRICT NOT VALID;
ALTER TABLE sleep_domain_pull_checkpoints
  ADD CONSTRAINT sleep_domain_pull_checkpoint_observation_fk
  FOREIGN KEY (
    last_canonical_observation_id, namespace_id, data_mode, subject_id
  ) REFERENCES sleep_domain_canonical_observations (
    observation_id, namespace_id, data_mode, subject_id
  ) ON DELETE RESTRICT NOT VALID;

CREATE UNIQUE INDEX ux_sleep_domain_pull_checkpoint_v2_scope
  ON sleep_domain_pull_checkpoints (
    namespace_id, data_mode, namespace_generation, provider_id,
    provider_account_id, subject_id, device_binding_id, data_surface
  ) WHERE protocol_version >= 2;

CREATE INDEX idx_sleep_domain_pull_checkpoint_cursor
  ON sleep_domain_pull_checkpoints (
    namespace_id, data_mode, namespace_generation, subject_id,
    data_surface, cursor_at
  ) WHERE protocol_version >= 2;

-- The caller supplies the deterministic semantic batch identity.  This index
-- is the final concurrent-write guard that permits multiple exact raw receipts
-- while creating at most one normalization work item for that semantic batch.
CREATE UNIQUE INDEX ux_sleep_domain_perceptor_pull_batch_work
  ON sleep_domain_normalization_work (
    namespace_id, data_mode, (work_json ->> 'batch_identity')
  ) WHERE work_json ->> 'normalizer' = 'perceptor_pull';

-- Each newly activated table follows the subject and generation carried by its
-- exact v2 raw/canonical evidence.  Legacy namespace-only rows are not exposed.
ALTER TABLE sleep_domain_source_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_source_reports FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_source_report_scope
  ON sleep_domain_source_reports;
CREATE POLICY sleep_domain_source_report_scope
  ON sleep_domain_source_reports
  USING (EXISTS (
    SELECT 1
    FROM public.sleep_domain_raw_inbox AS raw
    WHERE raw.raw_ingress_record_id =
        sleep_domain_source_reports.raw_ingress_record_id
      AND raw.namespace_id = sleep_domain_source_reports.namespace_id
      AND raw.data_mode = sleep_domain_source_reports.data_mode
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
        sleep_domain_source_reports.raw_ingress_record_id
      AND raw.namespace_id = sleep_domain_source_reports.namespace_id
      AND raw.data_mode = sleep_domain_source_reports.data_mode
      AND raw.scope_protocol_version >= 2
      AND public.sleepagent_subject_generation_scope_allows(
        raw.namespace_id, raw.data_mode, raw.subject_id,
        raw.namespace_generation, raw.run_id, raw.arm_id
      )
  ));

ALTER TABLE sleep_domain_pull_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_pull_checkpoints FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_pull_checkpoint_scope
  ON sleep_domain_pull_checkpoints;
CREATE POLICY sleep_domain_pull_checkpoint_scope
  ON sleep_domain_pull_checkpoints
  USING (
    protocol_version >= 2
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  )
  WITH CHECK (
    protocol_version >= 2
    AND public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE sleep_domain_observation_fact_values ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_observation_fact_values FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_fact_value_scope
  ON sleep_domain_observation_fact_values;
CREATE POLICY sleep_domain_fact_value_scope
  ON sleep_domain_observation_fact_values
  USING (EXISTS (
    SELECT 1
    FROM public.sleep_domain_canonical_observations AS observation
    JOIN public.sleep_domain_raw_inbox AS raw
      ON raw.raw_ingress_record_id = observation.raw_ingress_record_id
     AND raw.namespace_id = observation.namespace_id
     AND raw.data_mode = observation.data_mode
     AND raw.subject_id = observation.subject_id
    WHERE observation.observation_id =
        sleep_domain_observation_fact_values.observation_id
      AND observation.namespace_id =
        sleep_domain_observation_fact_values.namespace_id
      AND observation.data_mode =
        sleep_domain_observation_fact_values.data_mode
      AND raw.scope_protocol_version >= 2
      AND public.sleepagent_subject_generation_scope_allows(
        raw.namespace_id, raw.data_mode, raw.subject_id,
        raw.namespace_generation, raw.run_id, raw.arm_id
      )
  ))
  WITH CHECK (EXISTS (
    SELECT 1
    FROM public.sleep_domain_canonical_observations AS observation
    JOIN public.sleep_domain_raw_inbox AS raw
      ON raw.raw_ingress_record_id = observation.raw_ingress_record_id
     AND raw.namespace_id = observation.namespace_id
     AND raw.data_mode = observation.data_mode
     AND raw.subject_id = observation.subject_id
    WHERE observation.observation_id =
        sleep_domain_observation_fact_values.observation_id
      AND observation.namespace_id =
        sleep_domain_observation_fact_values.namespace_id
      AND observation.data_mode =
        sleep_domain_observation_fact_values.data_mode
      AND raw.scope_protocol_version >= 2
      AND public.sleepagent_subject_generation_scope_allows(
        raw.namespace_id, raw.data_mode, raw.subject_id,
        raw.namespace_generation, raw.run_id, raw.arm_id
      )
  ));

ALTER TABLE sleep_domain_observation_acquisitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_observation_acquisitions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_acquisition_scope
  ON sleep_domain_observation_acquisitions;
CREATE POLICY sleep_domain_acquisition_scope
  ON sleep_domain_observation_acquisitions
  USING (EXISTS (
    SELECT 1
    FROM public.sleep_domain_canonical_observations AS observation
    JOIN public.sleep_domain_raw_inbox AS raw
      ON raw.raw_ingress_record_id =
        sleep_domain_observation_acquisitions.raw_ingress_record_id
     AND raw.namespace_id = observation.namespace_id
     AND raw.data_mode = observation.data_mode
     AND raw.subject_id = observation.subject_id
    WHERE observation.observation_id =
        sleep_domain_observation_acquisitions.observation_id
      AND observation.namespace_id =
        sleep_domain_observation_acquisitions.namespace_id
      AND observation.data_mode =
        sleep_domain_observation_acquisitions.data_mode
      AND raw.scope_protocol_version >= 2
      AND public.sleepagent_subject_generation_scope_allows(
        raw.namespace_id, raw.data_mode, raw.subject_id,
        raw.namespace_generation, raw.run_id, raw.arm_id
      )
  ))
  WITH CHECK (EXISTS (
    SELECT 1
    FROM public.sleep_domain_canonical_observations AS observation
    JOIN public.sleep_domain_raw_inbox AS raw
      ON raw.raw_ingress_record_id =
        sleep_domain_observation_acquisitions.raw_ingress_record_id
     AND raw.namespace_id = observation.namespace_id
     AND raw.data_mode = observation.data_mode
     AND raw.subject_id = observation.subject_id
    WHERE observation.observation_id =
        sleep_domain_observation_acquisitions.observation_id
      AND observation.namespace_id =
        sleep_domain_observation_acquisitions.namespace_id
      AND observation.data_mode =
        sleep_domain_observation_acquisitions.data_mode
      AND raw.scope_protocol_version >= 2
      AND public.sleepagent_subject_generation_scope_allows(
        raw.namespace_id, raw.data_mode, raw.subject_id,
        raw.namespace_generation, raw.run_id, raw.arm_id
      )
  ));

ALTER TABLE sleep_domain_observation_conflicts ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_observation_conflicts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_conflict_scope
  ON sleep_domain_observation_conflicts;
CREATE POLICY sleep_domain_conflict_scope
  ON sleep_domain_observation_conflicts
  USING (EXISTS (
    SELECT 1
    FROM public.sleep_domain_canonical_observations AS first_observation
    JOIN public.sleep_domain_raw_inbox AS first_raw
      ON first_raw.raw_ingress_record_id =
        first_observation.raw_ingress_record_id
     AND first_raw.namespace_id = first_observation.namespace_id
     AND first_raw.data_mode = first_observation.data_mode
     AND first_raw.subject_id = first_observation.subject_id
    JOIN public.sleep_domain_canonical_observations AS second_observation
      ON second_observation.observation_id =
        sleep_domain_observation_conflicts.second_observation_id
     AND second_observation.namespace_id = first_observation.namespace_id
     AND second_observation.data_mode = first_observation.data_mode
     AND second_observation.subject_id = first_observation.subject_id
    JOIN public.sleep_domain_raw_inbox AS second_raw
      ON second_raw.raw_ingress_record_id =
        second_observation.raw_ingress_record_id
     AND second_raw.namespace_id = second_observation.namespace_id
     AND second_raw.data_mode = second_observation.data_mode
     AND second_raw.subject_id = second_observation.subject_id
     AND second_raw.namespace_generation = first_raw.namespace_generation
    WHERE first_observation.observation_id =
        sleep_domain_observation_conflicts.first_observation_id
      AND first_observation.namespace_id =
        sleep_domain_observation_conflicts.namespace_id
      AND first_observation.data_mode =
        sleep_domain_observation_conflicts.data_mode
      AND first_raw.scope_protocol_version >= 2
      AND second_raw.scope_protocol_version >= 2
      AND public.sleepagent_subject_generation_scope_allows(
        first_raw.namespace_id, first_raw.data_mode, first_raw.subject_id,
        first_raw.namespace_generation, first_raw.run_id, first_raw.arm_id
      )
  ))
  WITH CHECK (EXISTS (
    SELECT 1
    FROM public.sleep_domain_canonical_observations AS first_observation
    JOIN public.sleep_domain_raw_inbox AS first_raw
      ON first_raw.raw_ingress_record_id =
        first_observation.raw_ingress_record_id
     AND first_raw.namespace_id = first_observation.namespace_id
     AND first_raw.data_mode = first_observation.data_mode
     AND first_raw.subject_id = first_observation.subject_id
    JOIN public.sleep_domain_canonical_observations AS second_observation
      ON second_observation.observation_id =
        sleep_domain_observation_conflicts.second_observation_id
     AND second_observation.namespace_id = first_observation.namespace_id
     AND second_observation.data_mode = first_observation.data_mode
     AND second_observation.subject_id = first_observation.subject_id
    JOIN public.sleep_domain_raw_inbox AS second_raw
      ON second_raw.raw_ingress_record_id =
        second_observation.raw_ingress_record_id
     AND second_raw.namespace_id = second_observation.namespace_id
     AND second_raw.data_mode = second_observation.data_mode
     AND second_raw.subject_id = second_observation.subject_id
     AND second_raw.namespace_generation = first_raw.namespace_generation
    WHERE first_observation.observation_id =
        sleep_domain_observation_conflicts.first_observation_id
      AND first_observation.namespace_id =
        sleep_domain_observation_conflicts.namespace_id
      AND first_observation.data_mode =
        sleep_domain_observation_conflicts.data_mode
      AND first_raw.scope_protocol_version >= 2
      AND second_raw.scope_protocol_version >= 2
      AND public.sleepagent_subject_generation_scope_allows(
        first_raw.namespace_id, first_raw.data_mode, first_raw.subject_id,
        first_raw.namespace_generation, first_raw.run_id, first_raw.arm_id
      )
  ));

CREATE OR REPLACE FUNCTION sleepagent_ingest_perceptor_pull(
  target_namespace_id TEXT,
  target_namespace_generation BIGINT,
  target_provider_account_id TEXT,
  asserted_client_id_sha256 TEXT,
  requested_provider_device_id TEXT,
  requested_provider_device_name TEXT,
  response_provider_device_id TEXT,
  response_provider_device_name TEXT,
  pull_endpoint TEXT,
  requested_window_start TIMESTAMPTZ,
  requested_window_end TIMESTAMPTZ,
  requested_report_date DATE,
  requested_at TIMESTAMPTZ,
  received_at TIMESTAMPTZ,
  requested_raw_identity TEXT,
  requested_batch_identity TEXT,
  payload_sha256 TEXT,
  encrypted_payload BYTEA,
  encryption_key_id TEXT,
  content_type TEXT,
  payload_size_bytes INTEGER,
  retention_until TIMESTAMPTZ,
  response_is_valid BOOLEAN,
  response_has_data BOOLEAN,
  proposed_raw_ingress_record_id TEXT,
  proposed_normalization_work_id TEXT,
  proposed_intake_receipt_id TEXT,
  proposed_quarantine_receipt_id TEXT,
  proposed_quarantine_id TEXT,
  safe_metadata_json JSONB
)
RETURNS TABLE (
  disposition TEXT,
  raw_ingress_record_id TEXT,
  normalization_work_id TEXT,
  subject_id TEXT,
  device_binding_id TEXT,
  duplicate BOOLEAN
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  generation_matches INTEGER;
  account_matches INTEGER;
  existing_raw_id TEXT;
  existing_payload_sha256 TEXT;
  existing_raw_work_id TEXT;
  existing_raw_subject_id TEXT;
  existing_raw_binding_id TEXT;
  conflict_identity TEXT;
  binding_matches INTEGER;
  device_binding_candidates INTEGER;
  resolved_binding_id TEXT;
  resolved_binding_version INTEGER;
  resolved_subject_id TEXT;
  resolved_provider_device_id TEXT;
  resolved_provider_device_name TEXT;
  expected_provider_device_key TEXT;
  resolved_authorization_epoch BIGINT;
  resolved_privacy_epoch BIGINT;
  resolved_retrieval_policy_epoch BIGINT;
  normalization_worker_matches INTEGER;
  normalization_worker_principal TEXT;
  normalization_worker_purpose TEXT;
  resolved_surface TEXT;
  resolved_overlap_seconds INTEGER;
  response_semantic_sha256 TEXT;
  provider_device_key TEXT;
  checkpoint_stream_key TEXT;
  checkpoint_cursor_at TIMESTAMPTZ;
  checkpoint_lateness_watermark_at TIMESTAMPTZ;
  metadata_window_start TIMESTAMPTZ;
  metadata_window_end TIMESTAMPTZ;
  metadata_report_date DATE;
  existing_batch_work_id TEXT;
  existing_batch_subject_id TEXT;
  existing_batch_binding_id TEXT;
  existing_batch_account_id TEXT;
  existing_batch_endpoint TEXT;
  existing_batch_surface TEXT;
  existing_batch_semantic_sha256 TEXT;
  semantic_duplicate BOOLEAN := FALSE;
  quarantine_reason TEXT;
  quarantine_disposition TEXT;
BEGIN
  IF NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'api'
     OR NULLIF(current_setting('sleepagent.purpose', TRUE), '') <>
       'perceptor_ingress'
     OR target_namespace_id IS NULL
     OR target_namespace_generation IS NULL
     OR target_namespace_generation < 1
     OR target_provider_account_id IS NULL
     OR target_provider_account_id = ''
     OR asserted_client_id_sha256 IS NULL
     OR asserted_client_id_sha256 !~ '^[0-9a-f]{64}$'
     OR COALESCE(NULLIF(requested_provider_device_id, ''),
                  NULLIF(requested_provider_device_name, '')) IS NULL
     OR requested_at IS NULL
     OR received_at IS NULL
     OR received_at < requested_at
     OR requested_raw_identity IS NULL
     OR requested_raw_identity !~ '^[0-9a-f]{64}$'
     OR requested_batch_identity IS NULL
     OR requested_batch_identity !~ '^[0-9a-f]{64}$'
     OR payload_sha256 IS NULL
     OR payload_sha256 !~ '^[0-9a-f]{64}$'
     OR encrypted_payload IS NULL
     OR octet_length(encrypted_payload) < 1
     OR encryption_key_id IS NULL
     OR encryption_key_id = ''
     OR content_type IS NULL
     OR content_type <> 'application/json'
     OR payload_size_bytes IS NULL
     OR payload_size_bytes < 1
     OR retention_until IS NULL
     OR retention_until <= received_at
     OR response_is_valid IS NULL
     OR response_has_data IS NULL
     OR NULLIF(proposed_raw_ingress_record_id, '') IS NULL
     OR NULLIF(proposed_normalization_work_id, '') IS NULL
     OR NULLIF(proposed_intake_receipt_id, '') IS NULL
     OR NULLIF(proposed_quarantine_receipt_id, '') IS NULL
     OR NULLIF(proposed_quarantine_id, '') IS NULL
     OR safe_metadata_json IS NULL
     OR jsonb_typeof(safe_metadata_json) <> 'object'
     OR target_namespace_id <> NULLIF(
       current_setting('sleepagent.namespace_id', TRUE), ''
     )
     OR target_namespace_generation::TEXT <> NULLIF(
       current_setting('sleepagent.namespace_generation', TRUE), ''
     )
     OR NOT public.sleepagent_namespace_scope_allows(
       target_namespace_id, 'live'
     ) THEN
    RAISE EXCEPTION 'invalid or untrusted Perceptor Pull ingress context';
  END IF;

  IF (SELECT count(*) FROM jsonb_object_keys(safe_metadata_json)) <> 16
     OR NOT (safe_metadata_json ?& ARRAY[
       'schema_version', 'source_channel', 'endpoint', 'raw_sha256',
       'response_semantic_sha256', 'normalizer_version', 'batch_identity',
       'stream_key', 'checkpoint_cursor_at', 'lateness_watermark_at',
       'requested_window_start', 'requested_window_end',
       'requested_report_date', 'response_valid', 'response_has_data',
       'provider_device_key'
     ])
     OR safe_metadata_json ->> 'schema_version' IS DISTINCT FROM
       'perceptor_pull_raw_metadata.v1'
     OR safe_metadata_json ->> 'source_channel' IS DISTINCT FROM 'PULL'
     OR safe_metadata_json ->> 'endpoint' IS DISTINCT FROM pull_endpoint
     OR safe_metadata_json ->> 'raw_sha256' IS DISTINCT FROM payload_sha256
     OR safe_metadata_json ->> 'normalizer_version' IS DISTINCT FROM
       'perceptor-pull-normalizer.v1'
     OR safe_metadata_json ->> 'batch_identity' IS DISTINCT FROM
       requested_batch_identity
     OR safe_metadata_json -> 'response_valid' IS DISTINCT FROM
       to_jsonb(response_is_valid)
     OR safe_metadata_json -> 'response_has_data' IS DISTINCT FROM
       to_jsonb(response_has_data)
     OR jsonb_typeof(safe_metadata_json -> 'checkpoint_cursor_at') <>
       'string'
     OR jsonb_typeof(safe_metadata_json -> 'lateness_watermark_at') <>
       'string'
     OR (
       requested_window_start IS NULL
       AND safe_metadata_json -> 'requested_window_start' <> 'null'::JSONB
     )
     OR (
       requested_window_start IS NOT NULL
       AND jsonb_typeof(safe_metadata_json -> 'requested_window_start') <>
         'string'
     )
     OR (
       requested_window_end IS NULL
       AND safe_metadata_json -> 'requested_window_end' <> 'null'::JSONB
     )
     OR (
       requested_window_end IS NOT NULL
       AND jsonb_typeof(safe_metadata_json -> 'requested_window_end') <>
         'string'
     )
     OR (
       requested_report_date IS NULL
       AND safe_metadata_json -> 'requested_report_date' <> 'null'::JSONB
     )
     OR (
       requested_report_date IS NOT NULL
       AND jsonb_typeof(safe_metadata_json -> 'requested_report_date') <>
         'string'
     ) THEN
    RAISE EXCEPTION 'Perceptor Pull safe metadata contract mismatch';
  END IF;

  BEGIN
    metadata_window_start :=
      (safe_metadata_json ->> 'requested_window_start')::TIMESTAMPTZ;
    metadata_window_end :=
      (safe_metadata_json ->> 'requested_window_end')::TIMESTAMPTZ;
    metadata_report_date :=
      (safe_metadata_json ->> 'requested_report_date')::DATE;
  EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
    RAISE EXCEPTION 'Perceptor Pull safe metadata time contract mismatch';
  END;
  IF metadata_window_start IS DISTINCT FROM requested_window_start
     OR metadata_window_end IS DISTINCT FROM requested_window_end
     OR metadata_report_date IS DISTINCT FROM requested_report_date THEN
    RAISE EXCEPTION 'Perceptor Pull safe metadata coordinates mismatch';
  END IF;

  resolved_surface := CASE pull_endpoint
    WHEN '/vitalSigns/getCurrent' THEN 'current'
    WHEN '/vitalSigns/getRealTimes' THEN 'realtime'
    WHEN '/vitalSigns/getHistoryData' THEN 'history'
    WHEN '/vitalSigns/getSleepReport' THEN 'sleep_report'
    ELSE NULL
  END;
  IF resolved_surface IS NULL THEN
    RAISE EXCEPTION 'Perceptor Pull endpoint is outside the durable read allowlist';
  END IF;
  resolved_overlap_seconds := CASE
    WHEN resolved_surface = 'history' THEN 3
    ELSE 0
  END;

  IF resolved_surface = 'history' THEN
    IF requested_window_start IS NULL
       OR requested_window_end IS NULL
       OR requested_window_end <= requested_window_start
       OR requested_window_end - requested_window_start > INTERVAL '1 hour'
       OR requested_report_date IS NOT NULL THEN
      RAISE EXCEPTION 'invalid bounded Perceptor history window';
    END IF;
  ELSIF resolved_surface = 'sleep_report' THEN
    IF requested_report_date IS NULL
       OR requested_window_start IS NOT NULL
       OR requested_window_end IS NOT NULL THEN
      RAISE EXCEPTION 'invalid Perceptor sleep-report request scope';
    END IF;
  ELSIF requested_window_start IS NOT NULL
        OR requested_window_end IS NOT NULL
        OR requested_report_date IS NOT NULL THEN
    RAISE EXCEPTION 'current Perceptor Pull surface has no history window';
  END IF;

  SELECT count(*) INTO generation_matches
  FROM public.backend_namespaces AS namespace_row
  JOIN public.backend_namespace_generations AS generation_row
    ON generation_row.namespace_id = namespace_row.namespace_id
   AND generation_row.data_mode = namespace_row.data_mode
   AND generation_row.generation = namespace_row.current_generation
  WHERE namespace_row.namespace_id = target_namespace_id
    AND namespace_row.data_mode = 'live'
    AND namespace_row.status = 'active'
    AND namespace_row.current_generation = target_namespace_generation
    AND generation_row.status = 'active';
  IF generation_matches <> 1 THEN
    RAISE EXCEPTION 'Perceptor Pull namespace generation is not active';
  END IF;

  response_semantic_sha256 :=
    safe_metadata_json ->> 'response_semantic_sha256';
  provider_device_key := safe_metadata_json ->> 'provider_device_key';
  checkpoint_stream_key := safe_metadata_json ->> 'stream_key';
  IF response_semantic_sha256 IS NULL
     OR response_semantic_sha256 !~ '^[0-9a-f]{64}$'
     OR provider_device_key IS NULL
     OR provider_device_key !~ '^sha256:[0-9a-f]{64}$'
     OR checkpoint_stream_key IS NULL
     OR checkpoint_stream_key !~
       '^perceptor:pull:stream:[0-9a-f]{64}$'
     OR safe_metadata_json ->> 'checkpoint_cursor_at' IS NULL
     OR safe_metadata_json ->> 'lateness_watermark_at' IS NULL THEN
    RAISE EXCEPTION 'Perceptor Pull safe metadata value mismatch';
  ELSE
    BEGIN
      checkpoint_cursor_at :=
        (safe_metadata_json ->> 'checkpoint_cursor_at')::TIMESTAMPTZ;
      checkpoint_lateness_watermark_at :=
        (safe_metadata_json ->> 'lateness_watermark_at')::TIMESTAMPTZ;
    EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
      RAISE EXCEPTION 'Perceptor Pull safe checkpoint time mismatch';
    END;
    IF (
      checkpoint_lateness_watermark_at <>
        checkpoint_cursor_at -
          (resolved_overlap_seconds * INTERVAL '1 second')
      OR (resolved_surface = 'history'
        AND checkpoint_cursor_at <> requested_window_end)
    ) THEN
      RAISE EXCEPTION 'Perceptor Pull safe checkpoint contract mismatch';
    END IF;
  END IF;

  SELECT count(*) INTO account_matches
  FROM public.sleep_domain_provider_accounts AS account
  WHERE account.namespace_id = target_namespace_id
    AND account.data_mode = 'live'
    AND account.provider_account_id = target_provider_account_id
    AND account.provider_id = 'perceptor'
    AND account.status = 'active'
    AND account.account_metadata_json ->> 'client_id_sha256' =
      asserted_client_id_sha256;
  IF account_matches <> 1 THEN
    RAISE EXCEPTION 'Perceptor provider account binding mismatch';
  END IF;

  SELECT count(*) INTO device_binding_candidates
  FROM public.sleep_domain_device_bindings AS binding
  WHERE binding.namespace_id = target_namespace_id
    AND binding.data_mode = 'live'
    AND binding.provider_id = 'perceptor'
    AND binding.provider_account_id = target_provider_account_id
    AND binding.status = 'active'
    AND EXISTS (
      SELECT 1
      FROM public.backend_subjects AS subject_row
      WHERE subject_row.namespace_id = binding.namespace_id
        AND subject_row.data_mode = binding.data_mode
        AND subject_row.subject_id = binding.subject_id
        AND subject_row.status = 'active'
    )
    AND (
      NULLIF(requested_provider_device_id, '') IS NULL
      OR binding.binding_json #>> '{provider_device,provider_device_id}' =
        requested_provider_device_id
    )
    AND (
      NULLIF(requested_provider_device_name, '') IS NULL
      OR binding.binding_json #>> '{provider_device,provider_device_name}' =
        requested_provider_device_name
    );

  SELECT count(*), min(binding.device_binding_id),
         min(binding.binding_version), min(binding.subject_id),
         min(binding.binding_json #>> '{provider_device,provider_device_id}'),
         min(binding.binding_json #>> '{provider_device,provider_device_name}')
  INTO binding_matches, resolved_binding_id, resolved_binding_version,
       resolved_subject_id, resolved_provider_device_id,
       resolved_provider_device_name
  FROM public.sleep_domain_device_bindings AS binding
  WHERE binding.namespace_id = target_namespace_id
    AND binding.data_mode = 'live'
    AND binding.provider_id = 'perceptor'
    AND binding.provider_account_id = target_provider_account_id
    AND binding.status = 'active'
    AND EXISTS (
      SELECT 1
      FROM public.backend_subjects AS subject_row
      WHERE subject_row.namespace_id = binding.namespace_id
        AND subject_row.data_mode = binding.data_mode
        AND subject_row.subject_id = binding.subject_id
        AND subject_row.status = 'active'
    )
    AND (
      (
        resolved_surface = 'history'
        AND binding.effective_from <= requested_window_start
        AND (
          binding.effective_until IS NULL
          OR binding.effective_until > requested_window_end
        )
      )
      OR (
        resolved_surface = 'sleep_report'
        AND binding.effective_from <= (
          requested_report_date::TIMESTAMP AT TIME ZONE binding.timezone_name
        )
        AND (
          binding.effective_until IS NULL
          OR binding.effective_until >= (
            (requested_report_date + 1)::TIMESTAMP
              AT TIME ZONE binding.timezone_name
          )
        )
      )
      OR (
        resolved_surface IN ('current', 'realtime')
        AND binding.effective_from <= requested_at
        AND (
          binding.effective_until IS NULL
          OR binding.effective_until > requested_at
        )
      )
    )
    AND (
      NULLIF(requested_provider_device_id, '') IS NULL
      OR binding.binding_json #>> '{provider_device,provider_device_id}' =
        requested_provider_device_id
    )
    AND (
      NULLIF(requested_provider_device_name, '') IS NULL
      OR binding.binding_json #>> '{provider_device,provider_device_name}' =
        requested_provider_device_name
    );

  IF binding_matches = 0 AND device_binding_candidates > 0 THEN
    RAISE EXCEPTION
      'requested Pull data period is outside DeviceBinding effective interval';
  END IF;

  IF binding_matches = 1 THEN
    expected_provider_device_key := 'sha256:' || encode(digest(
      target_namespace_generation::TEXT || chr(31) || COALESCE(
        NULLIF(resolved_provider_device_id, ''),
        NULLIF(resolved_provider_device_name, '')
      ),
      'sha256'
    ), 'hex');
    IF provider_device_key IS DISTINCT FROM expected_provider_device_key THEN
      RAISE EXCEPTION 'Perceptor Pull safe device key mismatch';
    END IF;
    SELECT epoch.authorization_epoch, epoch.privacy_epoch,
           epoch.retrieval_policy_epoch
    INTO resolved_authorization_epoch, resolved_privacy_epoch,
         resolved_retrieval_policy_epoch
    FROM public.backend_subject_epochs AS epoch
    WHERE epoch.namespace_id = target_namespace_id
      AND epoch.data_mode = 'live'
      AND epoch.subject_id = resolved_subject_id;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'bound Perceptor subject has no governance epochs';
    END IF;
    SELECT count(*), min(grant_row.principal_id), min(grant_row.purpose)
    INTO normalization_worker_matches, normalization_worker_principal,
         normalization_worker_purpose
    FROM public.backend_principal_grants AS grant_row
    JOIN public.backend_service_principals AS principal
      ON principal.principal_id = grant_row.principal_id
     AND principal.principal_kind = 'worker'
     AND principal.status = 'active'
    WHERE grant_row.namespace_id = target_namespace_id
      AND grant_row.data_mode = 'live'
      AND grant_row.authorization_epoch = resolved_authorization_epoch
      AND grant_row.status = 'active'
      AND grant_row.valid_from <= clock_timestamp()
      AND (grant_row.valid_until IS NULL
        OR grant_row.valid_until > clock_timestamp())
      AND grant_row.allowed_handlers_json ? 'normalization';
    IF normalization_worker_matches <> 1 THEN
      RAISE EXCEPTION 'Perceptor normalization requires one exact worker grant';
    END IF;
  ELSE
    resolved_binding_id := NULL;
    resolved_binding_version := NULL;
    resolved_subject_id := NULL;
    resolved_provider_device_id := NULL;
    resolved_provider_device_name := NULL;
  END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(
    'perceptor-pull-raw' || chr(31) || target_namespace_id || chr(31) ||
      target_provider_account_id || chr(31) || requested_raw_identity,
    0
  ));
  PERFORM pg_advisory_xact_lock(hashtextextended(
    'perceptor-pull-batch' || chr(31) || target_namespace_id || chr(31) ||
      target_provider_account_id || chr(31) || requested_batch_identity,
    0
  ));

  SELECT raw.raw_ingress_record_id,
         raw.pre_normalization_payload_sha256,
         COALESCE(direct_work.work_id, batch_work.work_id),
         raw.subject_id,
         COALESCE(
           direct_work.work_json ->> 'device_binding_id',
           batch_work.work_json ->> 'device_binding_id',
           raw.raw_metadata_json ->> 'device_binding_id'
         )
  INTO existing_raw_id, existing_payload_sha256, existing_raw_work_id,
       existing_raw_subject_id, existing_raw_binding_id
  FROM public.sleep_domain_raw_inbox AS raw
  LEFT JOIN public.sleep_domain_normalization_work AS direct_work
    ON direct_work.raw_ingress_record_id = raw.raw_ingress_record_id
   AND direct_work.namespace_id = raw.namespace_id
   AND direct_work.data_mode = raw.data_mode
   AND direct_work.work_json ->> 'normalizer' = 'perceptor_pull'
  LEFT JOIN public.sleep_domain_normalization_work AS batch_work
    ON batch_work.namespace_id = raw.namespace_id
   AND batch_work.data_mode = raw.data_mode
   AND batch_work.work_json ->> 'normalizer' = 'perceptor_pull'
   AND batch_work.work_json ->> 'batch_identity' = requested_batch_identity
  WHERE raw.namespace_id = target_namespace_id
    AND raw.data_mode = 'live'
    AND raw.provider_account_id = target_provider_account_id
    AND raw.idempotency_identity = requested_raw_identity;

  IF existing_raw_id IS NOT NULL
     AND existing_payload_sha256 = payload_sha256 THEN
    RETURN QUERY SELECT
      'duplicate'::TEXT, existing_raw_id, existing_raw_work_id,
      existing_raw_subject_id, existing_raw_binding_id, TRUE;
    RETURN;
  END IF;

  conflict_identity := 'perceptor-pull-conflict.v1:' || encode(digest(
    requested_raw_identity || chr(31) || payload_sha256,
    'sha256'
  ), 'hex');
  IF existing_raw_id IS NOT NULL THEN
    SELECT raw.raw_ingress_record_id
    INTO raw_ingress_record_id
    FROM public.sleep_domain_raw_inbox AS raw
    WHERE raw.namespace_id = target_namespace_id
      AND raw.data_mode = 'live'
      AND raw.provider_account_id = target_provider_account_id
      AND raw.idempotency_identity = conflict_identity
      AND raw.pre_normalization_payload_sha256 = payload_sha256;
    IF raw_ingress_record_id IS NOT NULL THEN
      RETURN QUERY SELECT
        'quarantined_invalid'::TEXT, raw_ingress_record_id, NULL::TEXT,
        NULL::TEXT, NULL::TEXT, TRUE;
      RETURN;
    END IF;
  END IF;

  IF existing_raw_id IS NOT NULL THEN
    requested_raw_identity := conflict_identity;
    quarantine_reason := 'raw_identity_collision';
    quarantine_disposition := 'quarantined_invalid';
  ELSIF binding_matches = 0 THEN
    quarantine_reason := 'device_unbound';
    quarantine_disposition := 'quarantined_unbound';
  ELSIF binding_matches > 1 THEN
    quarantine_reason := 'device_binding_ambiguous';
    quarantine_disposition := 'quarantined_unbound';
  ELSIF NOT response_is_valid THEN
    quarantine_reason := 'invalid_response';
    quarantine_disposition := 'quarantined_invalid';
  ELSIF (
    NULLIF(response_provider_device_id, '') IS NOT NULL
      AND response_provider_device_id IS DISTINCT FROM
        resolved_provider_device_id
  ) OR (
    NULLIF(response_provider_device_name, '') IS NOT NULL
      AND response_provider_device_name IS DISTINCT FROM
        resolved_provider_device_name
  ) THEN
    quarantine_reason := 'response_device_mismatch';
    quarantine_disposition := 'quarantined_device_mismatch';
  END IF;

  IF quarantine_reason IS NULL THEN
    SELECT work.work_id, work.subject_id,
           work.work_json ->> 'device_binding_id',
           raw.provider_account_id,
           work.work_json ->> 'endpoint',
           work.work_json ->> 'data_surface',
           work.work_json ->> 'response_semantic_sha256'
    INTO existing_batch_work_id, existing_batch_subject_id,
         existing_batch_binding_id, existing_batch_account_id,
         existing_batch_endpoint, existing_batch_surface,
         existing_batch_semantic_sha256
    FROM public.sleep_domain_normalization_work AS work
    JOIN public.sleep_domain_raw_inbox AS raw
      ON raw.raw_ingress_record_id = work.raw_ingress_record_id
     AND raw.namespace_id = work.namespace_id
     AND raw.data_mode = work.data_mode
     AND raw.subject_id = work.subject_id
    WHERE work.namespace_id = target_namespace_id
      AND work.data_mode = 'live'
      AND work.work_json ->> 'normalizer' = 'perceptor_pull'
      AND work.work_json ->> 'batch_identity' = requested_batch_identity;
    IF existing_batch_work_id IS NOT NULL THEN
      IF existing_batch_subject_id IS DISTINCT FROM resolved_subject_id
         OR existing_batch_binding_id IS DISTINCT FROM resolved_binding_id
         OR existing_batch_account_id IS DISTINCT FROM
           target_provider_account_id
         OR existing_batch_endpoint IS DISTINCT FROM pull_endpoint
         OR existing_batch_surface IS DISTINCT FROM resolved_surface
         OR existing_batch_semantic_sha256 IS DISTINCT FROM
           response_semantic_sha256 THEN
        quarantine_reason := 'batch_identity_collision';
        quarantine_disposition := 'quarantined_invalid';
      ELSE
        semantic_duplicate := TRUE;
      END IF;
    END IF;
  END IF;

  IF quarantine_reason IS NOT NULL THEN
    resolved_binding_id := NULL;
    resolved_binding_version := NULL;
    resolved_subject_id := NULL;
  END IF;

  INSERT INTO public.sleep_domain_raw_inbox (
    raw_ingress_record_id, namespace_id, data_mode,
    provider_id, provider_account_id, event_type, message_id,
    request_signed_at, measurement_at, event_occurred_at,
    received_at, signature_profile, signature_verification,
    idempotency_identity, idempotency_version,
    pre_normalization_payload_sha256, encrypted_payload,
    encryption_key_id, encrypted_at, content_type,
    payload_size_bytes, retention_until, raw_metadata_json,
    scope_protocol_version, namespace_generation, run_id,
    arm_id, subject_id, encryption_protocol_version
  ) VALUES (
    proposed_raw_ingress_record_id, target_namespace_id, 'live',
    'perceptor', target_provider_account_id,
    'PerceptorPullResponse:' || resolved_surface, NULL,
    requested_at, NULL, NULL, received_at,
    'perceptor-platform-read-response.v1', 'not_provided',
    requested_raw_identity, 'perceptor-pull-raw.v1', payload_sha256,
    encrypted_payload, encryption_key_id, received_at, content_type,
    payload_size_bytes, retention_until,
    jsonb_build_object(
      'schema_version', 'perceptor_pull_raw_metadata.v1',
      'source_channel', 'PULL',
      'endpoint', pull_endpoint,
      'raw_sha256', payload_sha256,
      'response_semantic_sha256', response_semantic_sha256,
      'normalizer_version', 'perceptor-pull-normalizer.v1',
      'batch_identity', requested_batch_identity,
      'stream_key', checkpoint_stream_key,
      'checkpoint_cursor_at', checkpoint_cursor_at,
      'lateness_watermark_at', checkpoint_lateness_watermark_at,
      'requested_window_start', requested_window_start,
      'requested_window_end', requested_window_end,
      'requested_report_date', requested_report_date,
      'response_valid', response_is_valid,
      'response_has_data', response_has_data,
      'provider_device_key', provider_device_key,
      'data_surface', resolved_surface,
      'raw_identity', requested_raw_identity,
      'device_binding_resolved', resolved_binding_id IS NOT NULL,
      'device_binding_id', resolved_binding_id,
      'quarantine_reason', quarantine_reason
    ),
    CASE WHEN quarantine_reason IS NULL THEN 2 ELSE 1 END,
    CASE WHEN quarantine_reason IS NULL
      THEN target_namespace_generation ELSE NULL END,
    NULL, NULL,
    CASE WHEN quarantine_reason IS NULL THEN resolved_subject_id ELSE NULL END,
    1
  );

  INSERT INTO public.sleep_domain_processing_receipts (
    receipt_id, namespace_id, data_mode, raw_ingress_record_id,
    stage, outcome, receipt_json, occurred_at
  ) VALUES (
    proposed_intake_receipt_id, target_namespace_id, 'live',
    proposed_raw_ingress_record_id, 'intake', 'accepted',
    jsonb_build_object(
      'schema_version', 'processing_receipt.v2',
      'receipt_id', proposed_intake_receipt_id,
      'raw_ingress_record_id', proposed_raw_ingress_record_id,
      'stage', 'intake',
      'outcome', 'accepted',
      'source_channel', 'PULL',
      'response_authenticated', TRUE,
      'semantic_duplicate', semantic_duplicate
    ),
    received_at
  );

  IF quarantine_reason IS NOT NULL THEN
    INSERT INTO public.sleep_domain_processing_receipts (
      receipt_id, namespace_id, data_mode, raw_ingress_record_id,
      stage, outcome, quarantine_reason, receipt_json, occurred_at
    ) VALUES (
      proposed_quarantine_receipt_id, target_namespace_id, 'live',
      proposed_raw_ingress_record_id, 'normalization', 'quarantined',
      quarantine_reason,
      jsonb_build_object(
        'schema_version', 'processing_receipt.v2',
        'receipt_id', proposed_quarantine_receipt_id,
        'raw_ingress_record_id', proposed_raw_ingress_record_id,
        'stage', 'normalization',
        'outcome', 'quarantined',
        'quarantine_reason', quarantine_reason,
        'source_channel', 'PULL'
      ),
      received_at
    );
    INSERT INTO public.sleep_domain_quarantine (
      quarantine_id, namespace_id, data_mode, raw_ingress_record_id,
      reason, detail_code, receipt_id, quarantine_json, quarantined_at
    ) VALUES (
      proposed_quarantine_id, target_namespace_id, 'live',
      proposed_raw_ingress_record_id, quarantine_reason,
      quarantine_disposition, proposed_quarantine_receipt_id,
      jsonb_build_object(
        'schema_version', 'perceptor_quarantine.v1',
        'reason', quarantine_reason,
        'detail_code', quarantine_disposition,
        'raw_ingress_record_id', proposed_raw_ingress_record_id,
        'source_channel', 'PULL'
      ),
      received_at
    );
    RETURN QUERY SELECT
      quarantine_disposition, proposed_raw_ingress_record_id, NULL::TEXT,
      NULL::TEXT, NULL::TEXT, FALSE;
    RETURN;
  END IF;

  IF semantic_duplicate THEN
    RETURN QUERY SELECT
      'semantic_duplicate'::TEXT, proposed_raw_ingress_record_id,
      existing_batch_work_id, resolved_subject_id, resolved_binding_id, TRUE;
    RETURN;
  END IF;

  INSERT INTO public.sleep_domain_normalization_work (
    work_id, namespace_id, data_mode, raw_ingress_record_id,
    work_generation, status, attempt_count, available_at,
    work_json, created_at, updated_at, protocol_version,
    namespace_generation, run_id, arm_id, subject_id,
    authorization_snapshot_json, max_attempts, lease_generation
  ) VALUES (
    proposed_normalization_work_id, target_namespace_id, 'live',
    proposed_raw_ingress_record_id, 1, 'pending', 0, received_at,
    jsonb_build_object(
      'schema_version', 'normalization_work.v2',
      'normalizer', 'perceptor_pull',
      'normalizer_version', 'perceptor-pull-normalizer.v1',
      'raw_ingress_record_id', proposed_raw_ingress_record_id,
      'payload_sha256', payload_sha256,
      'response_semantic_sha256', response_semantic_sha256,
      'provider_device_key', provider_device_key,
      'endpoint', pull_endpoint,
      'data_surface', resolved_surface,
      'source_channel', 'PULL',
      'batch_identity', requested_batch_identity,
      'stream_key', checkpoint_stream_key,
      'checkpoint_cursor_at', checkpoint_cursor_at,
      'lateness_watermark_at', checkpoint_lateness_watermark_at,
      'overlap_seconds', resolved_overlap_seconds,
      'requested_window_start', requested_window_start,
      'requested_window_end', requested_window_end,
      'requested_report_date', requested_report_date,
      'response_valid', response_is_valid,
      'response_has_data', response_has_data,
      'device_binding_id', resolved_binding_id,
      'binding_version', resolved_binding_version,
      'provider_account_id', target_provider_account_id
    ),
    received_at, received_at, 2, target_namespace_generation,
    NULL, NULL, resolved_subject_id,
    jsonb_build_object(
      'schema_version', 'workload_authorization_snapshot.v1',
      'workload_principal_id', normalization_worker_principal,
      'namespace_id', target_namespace_id,
      'namespace_generation', target_namespace_generation,
      'data_mode', 'live',
      'run_id', NULL,
      'arm_id', NULL,
      'subject_id', resolved_subject_id,
      'purpose', normalization_worker_purpose,
      'allowed_handler', 'normalization',
      'authorization_epoch', resolved_authorization_epoch,
      'privacy_epoch', resolved_privacy_epoch,
      'retrieval_policy_epoch', resolved_retrieval_policy_epoch
    ),
    8, 0
  );

  RETURN QUERY SELECT
    'accepted'::TEXT, proposed_raw_ingress_record_id,
    proposed_normalization_work_id, resolved_subject_id,
    resolved_binding_id, FALSE;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_ingest_perceptor_pull(
  TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT,
  TIMESTAMPTZ, TIMESTAMPTZ, DATE, TIMESTAMPTZ, TIMESTAMPTZ,
  TEXT, TEXT, TEXT, BYTEA, TEXT, TEXT, INTEGER, TIMESTAMPTZ,
  BOOLEAN, BOOLEAN, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB
) FROM PUBLIC;

-- The API role intentionally has no SELECT privilege on the Worker-owned
-- checkpoint table.  This reader returns timestamps only after re-resolving
-- the exact live authority and binding.  A first call keeps the caller's
-- explicit bounded window; every continuation starts at the durable lateness
-- watermark, which the v2 contract fixes to cursor_at minus three seconds.
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
    IF requested_window_end <= resolved_checkpoint_cursor THEN
      RAISE EXCEPTION 'Perceptor history continuation must advance the cursor';
    END IF;
    resolved_window_start := resolved_lateness_watermark;
  ELSE
    resolved_window_start := requested_window_start;
  END IF;

  IF requested_window_end - resolved_window_start > INTERVAL '1 hour' THEN
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
      OR binding.effective_until > requested_window_end
    );
  IF binding_matches <> 1 THEN
    RAISE EXCEPTION 'Perceptor history planner binding mismatch';
  END IF;

  RETURN QUERY SELECT
    resolved_window_start,
    requested_window_end,
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
