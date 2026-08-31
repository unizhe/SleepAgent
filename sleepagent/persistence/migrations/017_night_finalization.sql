-- sleepagent:transactional=true

-- G7/M14: acquisition/data finalization, separate from episode date ownership.

CREATE TABLE public.sleep_domain_night_finalizations (
  night_finalization_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN (
    'open', 'soft_finalized', 'hard_finalized', 'reconciliation_required'
  )),
  current_finalization_revision_id TEXT,
  current_revision_number INTEGER CHECK (current_revision_number >= 1),
  cas_version BIGINT NOT NULL DEFAULT 0 CHECK (cas_version >= 0),
  policy_version TEXT NOT NULL,
  policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  UNIQUE (night_finalization_id, namespace_id, data_mode),
  UNIQUE (namespace_id, data_mode, night_episode_id),
  FOREIGN KEY (night_episode_id, namespace_id, data_mode)
    REFERENCES public.sleep_domain_night_episodes (
      night_episode_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
    REFERENCES public.backend_namespace_generations (
      namespace_id, data_mode, generation
    ) ON DELETE RESTRICT,
  CHECK (
    (state = 'open' AND current_finalization_revision_id IS NULL
      AND current_revision_number IS NULL)
    OR (state <> 'open' AND current_finalization_revision_id IS NOT NULL
      AND current_revision_number IS NOT NULL)
  ),
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%'
      AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%'
      AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

CREATE TABLE public.sleep_domain_night_finalization_revisions (
  night_finalization_revision_id TEXT PRIMARY KEY,
  night_finalization_id TEXT NOT NULL,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  run_id TEXT,
  arm_id TEXT,
  subject_id TEXT NOT NULL,
  night_episode_id TEXT NOT NULL,
  finalization_revision_number INTEGER NOT NULL CHECK (
    finalization_revision_number >= 1
  ),
  parent_finalization_revision_id TEXT,
  source_night_episode_revision_id TEXT NOT NULL,
  source_report_version_id TEXT,
  state TEXT NOT NULL CHECK (state IN (
    'soft_finalized', 'hard_finalized', 'reconciliation_required'
  )),
  provisional BOOLEAN NOT NULL,
  coverage_status TEXT NOT NULL CHECK (coverage_status IN (
    'complete', 'partial', 'data_insufficient'
  )),
  coverage_caveat TEXT,
  revision_cause TEXT NOT NULL CHECK (revision_cause IN (
    'wake_grace_elapsed', 'vendor_report_reconciled',
    'maximum_wait_elapsed', 'late_material_evidence',
    'reconciliation_conflict'
  )),
  material_sha256 TEXT NOT NULL CHECK (material_sha256 ~ '^[0-9a-f]{64}$'),
  finalization_json JSONB NOT NULL,
  reanalysis_operation_id TEXT,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (night_finalization_revision_id, night_finalization_id),
  UNIQUE (night_finalization_id, finalization_revision_number),
  UNIQUE (night_finalization_id, material_sha256),
  FOREIGN KEY (night_finalization_id, namespace_id, data_mode)
    REFERENCES public.sleep_domain_night_finalizations (
      night_finalization_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (source_night_episode_revision_id, namespace_id, data_mode)
    REFERENCES public.sleep_domain_night_episode_revisions (
      night_episode_revision_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  FOREIGN KEY (parent_finalization_revision_id)
    REFERENCES public.sleep_domain_night_finalization_revisions (
      night_finalization_revision_id
    ) ON DELETE RESTRICT,
  FOREIGN KEY (reanalysis_operation_id, namespace_id, data_mode)
    REFERENCES public.sleep_domain_operations (
      operation_id, namespace_id, data_mode
    ) ON DELETE RESTRICT,
  CHECK (
    (finalization_revision_number = 1
      AND parent_finalization_revision_id IS NULL)
    OR (finalization_revision_number > 1
      AND parent_finalization_revision_id IS NOT NULL)
  ),
  CHECK (
    (state = 'soft_finalized' AND provisional = TRUE
      AND coverage_caveat IS NOT NULL)
    OR (state = 'hard_finalized' AND provisional = FALSE)
    OR state = 'reconciliation_required'
  ),
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%'
      AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%'
      AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  )
);

ALTER TABLE public.sleep_domain_night_finalizations
  ADD CONSTRAINT sleep_domain_night_finalization_current_revision_fk
  FOREIGN KEY (
    current_finalization_revision_id, night_finalization_id
  ) REFERENCES public.sleep_domain_night_finalization_revisions (
    night_finalization_revision_id, night_finalization_id
  ) DEFERRABLE INITIALLY DEFERRED;

CREATE INDEX idx_sleep_domain_night_finalization_subject
  ON public.sleep_domain_night_finalizations (
    namespace_id, data_mode, subject_id, state, updated_at
  );
CREATE INDEX idx_sleep_domain_night_finalization_revision_episode
  ON public.sleep_domain_night_finalization_revisions (
    namespace_id, data_mode, night_episode_id,
    finalization_revision_number DESC
  );

CREATE OR REPLACE FUNCTION public.sleepagent_validate_night_finalization_update()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF ROW(
    NEW.night_finalization_id, NEW.namespace_id, NEW.data_mode,
    NEW.namespace_generation, NEW.run_id, NEW.arm_id, NEW.subject_id,
    NEW.night_episode_id, NEW.policy_version, NEW.policy_sha256, NEW.created_at
  ) IS DISTINCT FROM ROW(
    OLD.night_finalization_id, OLD.namespace_id, OLD.data_mode,
    OLD.namespace_generation, OLD.run_id, OLD.arm_id, OLD.subject_id,
    OLD.night_episode_id, OLD.policy_version, OLD.policy_sha256, OLD.created_at
  ) THEN
    RAISE EXCEPTION 'NightFinalization identity and policy are immutable';
  END IF;
  IF NEW.cas_version <> OLD.cas_version + 1
     OR NEW.current_revision_number IS NULL
     OR NEW.current_revision_number IS DISTINCT FROM
       COALESCE(OLD.current_revision_number, 0) + 1
     OR NEW.current_finalization_revision_id IS NULL THEN
    RAISE EXCEPTION 'NightFinalization CAS/revision transition is invalid';
  END IF;
  IF NOT (
    (OLD.state = 'open' AND NEW.state IN (
      'soft_finalized', 'hard_finalized', 'reconciliation_required'
    ))
    OR (OLD.state = 'soft_finalized' AND NEW.state IN (
      'soft_finalized', 'hard_finalized', 'reconciliation_required'
    ))
    OR (OLD.state = 'hard_finalized' AND NEW.state IN (
      'hard_finalized', 'reconciliation_required'
    ))
    OR (OLD.state = 'reconciliation_required' AND NEW.state IN (
      'soft_finalized', 'hard_finalized', 'reconciliation_required'
    ))
  ) THEN
    RAISE EXCEPTION 'NightFinalization state transition is invalid';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER sleepagent_validate_night_finalization_update_trigger
BEFORE UPDATE ON public.sleep_domain_night_finalizations
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_validate_night_finalization_update();

CREATE OR REPLACE FUNCTION public.sleepagent_finalization_revision_append_only()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'NightFinalization revision is immutable';
END;
$$;

CREATE TRIGGER sleepagent_finalization_revision_append_only_trigger
BEFORE UPDATE OR DELETE ON public.sleep_domain_night_finalization_revisions
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_finalization_revision_append_only();

ALTER TABLE public.sleep_domain_night_finalizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sleep_domain_night_finalizations FORCE ROW LEVEL SECURITY;
CREATE POLICY sleep_domain_night_finalization_scope
  ON public.sleep_domain_night_finalizations
  USING (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

ALTER TABLE public.sleep_domain_night_finalization_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sleep_domain_night_finalization_revisions FORCE ROW LEVEL SECURITY;
CREATE POLICY sleep_domain_night_finalization_revision_scope
  ON public.sleep_domain_night_finalization_revisions
  USING (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ))
  WITH CHECK (public.sleepagent_subject_generation_scope_allows(
    namespace_id, data_mode, subject_id, namespace_generation, run_id, arm_id
  ));

REVOKE ALL ON FUNCTION public.sleepagent_validate_night_finalization_update()
  FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_finalization_revision_append_only()
  FROM PUBLIC;
