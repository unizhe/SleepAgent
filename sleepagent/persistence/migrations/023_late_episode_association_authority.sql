-- sleepagent:transactional=true

-- C1B: governed late-observation association evidence.
-- Existing rows remain readable under their legacy subject scope. Every new
-- row must pin namespace generation and LIVE/Replay arm authority.

ALTER TABLE public.sleep_domain_pending_episode_associations
  ADD COLUMN namespace_generation BIGINT;
ALTER TABLE public.sleep_domain_pending_episode_associations
  ADD COLUMN run_id TEXT;
ALTER TABLE public.sleep_domain_pending_episode_associations
  ADD COLUMN arm_id TEXT;

ALTER TABLE public.sleep_domain_pending_episode_associations
  ADD CONSTRAINT sleep_domain_pending_association_generation_required_v2
  CHECK (namespace_generation IS NOT NULL) NOT VALID;

ALTER TABLE public.sleep_domain_pending_episode_associations
  ADD CONSTRAINT sleep_domain_pending_association_mode_scope_v2
  CHECK (
    namespace_generation IS NULL
    OR (data_mode = 'live' AND run_id IS NULL AND arm_id IS NULL)
    OR (data_mode = 'replay' AND run_id IS NOT NULL AND arm_id IS NOT NULL)
  ) NOT VALID;

ALTER TABLE public.sleep_domain_pending_episode_associations
  ADD CONSTRAINT sleep_domain_pending_association_generation_fk_v2
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
  REFERENCES public.backend_namespace_generations (
    namespace_id, data_mode, generation
  ) ON DELETE RESTRICT NOT VALID;

ALTER TABLE public.sleep_domain_pending_episode_associations
  ADD CONSTRAINT sleep_domain_pending_association_arm_fk_v2
  FOREIGN KEY (namespace_id, namespace_generation, run_id, arm_id)
  REFERENCES public.backend_replay_arms (
    namespace_id, namespace_generation, run_id, arm_id
  ) ON DELETE RESTRICT NOT VALID;

ALTER TABLE public.sleep_domain_pending_episode_associations
  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sleep_domain_pending_episode_associations
  FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.sleep_domain_pending_episode_associations
  FROM PUBLIC;
DROP POLICY IF EXISTS sleep_domain_pending_episode_association_scope
  ON public.sleep_domain_pending_episode_associations;
CREATE POLICY sleep_domain_pending_episode_association_scope
  ON public.sleep_domain_pending_episode_associations
  USING (
    (
      namespace_generation IS NULL
      AND public.sleepagent_subject_scope_allows(
        namespace_id, data_mode, subject_id
      )
    )
    OR public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation,
      run_id, arm_id
    )
  )
  WITH CHECK (
    public.sleepagent_subject_generation_scope_allows(
      namespace_id, data_mode, subject_id, namespace_generation,
      run_id, arm_id
    )
  );

CREATE INDEX idx_sleep_domain_pending_association_subject_v2
  ON public.sleep_domain_pending_episode_associations (
    namespace_id, data_mode, namespace_generation,
    subject_id, status, created_at
  );

-- Closed-Episode resolution probes exact historical binding evidence for a
-- bounded Episode set. Keep that existence check index-backed as membership
-- history grows within a namespace.
CREATE INDEX idx_sleep_domain_membership_binding_episode_v2
  ON public.sleep_domain_episode_observation_memberships (
    namespace_id, data_mode, night_episode_id, subject_id,
    device_binding_id, binding_version
  );
