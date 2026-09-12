-- sleepagent:transactional=true

-- M3 stores canonical V2 meaning beside the immutable V1 compatibility row.
-- The sidecar is the sole authority for V2 analytics; raw and V1 evidence are
-- deliberately not rewritten by this migration.
CREATE TABLE public.sleep_domain_observation_semantics_v2 (
  observation_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  schema_version TEXT NOT NULL CHECK (
    schema_version = 'canonical_observation.v2'
  ),
  observation_type TEXT NOT NULL,
  metric_id TEXT NOT NULL,
  semantic_payload_json JSONB NOT NULL,
  canonical_unit TEXT,
  occurred_at TIMESTAMPTZ NOT NULL,
  aggregation_start_at TIMESTAMPTZ,
  aggregation_end_at TIMESTAMPTZ,
  source_kind TEXT NOT NULL,
  provenance_json JSONB NOT NULL,
  vendor_semantic_code TEXT,
  semantics_version TEXT NOT NULL CHECK (
    semantics_version = 'observation_semantics.v2'
  ),
  ontology_version TEXT NOT NULL,
  normalizer_version TEXT NOT NULL,
  semantic_identity TEXT NOT NULL CHECK (
    semantic_identity ~ '^[0-9a-f]{64}$'
  ),
  transport_receipt_identity TEXT NOT NULL,
  trusted_for_analytics BOOLEAN NOT NULL,
  upcast_status TEXT NOT NULL CHECK (
    upcast_status IN ('native_v2', 'legacy_classified', 'legacy_ambiguous')
  ),
  classification_evidence JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (observation_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, semantic_identity),
  FOREIGN KEY (observation_id, namespace_id, data_mode, subject_id)
    REFERENCES public.sleep_domain_canonical_observations (
      observation_id, namespace_id, data_mode, subject_id
    ) ON DELETE RESTRICT,
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%')
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%')
  ),
  CHECK (
    (aggregation_start_at IS NULL AND aggregation_end_at IS NULL)
    OR (
      aggregation_start_at IS NOT NULL
      AND aggregation_end_at IS NOT NULL
      AND aggregation_end_at > aggregation_start_at
    )
  ),
  CONSTRAINT sleep_domain_observation_semantics_v2_movement CHECK (
    observation_type <> 'movement'
    OR (
      metric_id = 'movement_index'
      AND canonical_unit = 'vendor_index'
      AND source_kind = 'device_measured'
      AND aggregation_start_at IS NULL
      AND aggregation_end_at IS NULL
      AND trusted_for_analytics
      AND upcast_status <> 'legacy_ambiguous'
      AND jsonb_typeof(semantic_payload_json -> 'value') = 'number'
      AND (semantic_payload_json ->> 'value')::NUMERIC >= 0
    )
    OR (
      metric_id = 'movement_event_count'
      AND canonical_unit = 'count'
      AND source_kind = 'vendor_derived'
      AND aggregation_start_at IS NOT NULL
      AND aggregation_end_at IS NOT NULL
      AND aggregation_end_at > aggregation_start_at
      AND trusted_for_analytics
      AND upcast_status <> 'legacy_ambiguous'
      AND jsonb_typeof(semantic_payload_json -> 'value') = 'number'
      AND (semantic_payload_json ->> 'value')::NUMERIC >= 0
      AND trunc((semantic_payload_json ->> 'value')::NUMERIC) =
          (semantic_payload_json ->> 'value')::NUMERIC
    )
    OR (
      metric_id = 'legacy_ambiguous_movement'
      AND canonical_unit = 'legacy_unknown'
      AND source_kind IN ('device_measured', 'vendor_derived')
      AND NOT trusted_for_analytics
      AND upcast_status = 'legacy_ambiguous'
      AND jsonb_typeof(semantic_payload_json -> 'value') = 'number'
      AND (semantic_payload_json ->> 'value')::NUMERIC >= 0
    )
  ),
  CONSTRAINT sleep_domain_observation_semantics_v2_nonmovement CHECK (
    observation_type = 'movement'
    OR (
      metric_id NOT IN (
        'movement_index',
        'movement_event_count',
        'legacy_ambiguous_movement'
      )
      AND trusted_for_analytics
      AND upcast_status <> 'legacy_ambiguous'
    )
  )
);

CREATE INDEX idx_sleep_domain_observation_semantics_v2_metric_time
  ON public.sleep_domain_observation_semantics_v2 (
    namespace_id, data_mode, subject_id, metric_id, occurred_at
  );

CREATE INDEX idx_sleep_domain_observation_semantics_v2_count_window
  ON public.sleep_domain_observation_semantics_v2 (
    namespace_id, data_mode, subject_id,
    aggregation_start_at, aggregation_end_at
  ) WHERE metric_id = 'movement_event_count' AND trusted_for_analytics;

CREATE TRIGGER sleep_domain_observation_semantics_v2_immutable
BEFORE UPDATE OR DELETE ON public.sleep_domain_observation_semantics_v2
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_reject_append_only_mutation();

ALTER TABLE public.sleep_domain_observation_semantics_v2
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sleep_domain_observation_semantics_v2
  FORCE ROW LEVEL SECURITY;
CREATE POLICY sleep_domain_observation_semantics_v2_scope
  ON public.sleep_domain_observation_semantics_v2
  USING (
    public.sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  )
  WITH CHECK (
    public.sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

REVOKE ALL ON TABLE public.sleep_domain_observation_semantics_v2 FROM PUBLIC;
