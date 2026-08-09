-- sleepagent:transactional=true
-- Database-owned namespace, identity, grant and governance epochs.

CREATE TABLE IF NOT EXISTS backend_namespaces (
  namespace_id TEXT PRIMARY KEY,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  current_generation BIGINT NOT NULL DEFAULT 1 CHECK (current_generation >= 1),
  status TEXT NOT NULL DEFAULT 'active' CHECK (
    status IN ('active', 'suspended', 'retired')
  ),
  synthetic_non_release BOOLEAN NOT NULL,
  max_worker_concurrency INTEGER NOT NULL DEFAULT 4 CHECK (
    max_worker_concurrency BETWEEN 1 AND 256
  ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (namespace_id, data_mode),
  CHECK (
    (data_mode = 'live' AND namespace_id LIKE 'live:%'
      AND synthetic_non_release = FALSE)
    OR (data_mode = 'replay' AND namespace_id LIKE 'replay:%'
      AND synthetic_non_release = TRUE)
  )
);

CREATE TABLE IF NOT EXISTS backend_namespace_generations (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  generation BIGINT NOT NULL CHECK (generation >= 1),
  status TEXT NOT NULL CHECK (status IN ('active', 'sealed', 'retired')),
  configuration_sha256 TEXT NOT NULL CHECK (
    configuration_sha256 ~ '^[0-9a-f]{64}$'
  ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  sealed_at TIMESTAMPTZ,
  PRIMARY KEY (namespace_id, data_mode, generation),
  FOREIGN KEY (namespace_id, data_mode)
    REFERENCES backend_namespaces (namespace_id, data_mode) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_backend_active_namespace_generation
  ON backend_namespace_generations (namespace_id, data_mode)
  WHERE status = 'active';

CREATE TABLE IF NOT EXISTS backend_replay_runs (
  run_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  scenario_id TEXT NOT NULL,
  scenario_sha256 TEXT NOT NULL CHECK (scenario_sha256 ~ '^[0-9a-f]{64}$'),
  generation INTEGER NOT NULL CHECK (generation >= 1),
  status TEXT NOT NULL CHECK (status IN ('active', 'sealed', 'reset')),
  synthetic_non_release BOOLEAN NOT NULL DEFAULT TRUE CHECK (
    synthetic_non_release = TRUE
  ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  sealed_at TIMESTAMPTZ,
  UNIQUE (namespace_id, namespace_generation, run_id),
  FOREIGN KEY (namespace_id, data_mode, namespace_generation)
    REFERENCES backend_namespace_generations (
      namespace_id, data_mode, generation
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS backend_replay_arms (
  arm_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES backend_replay_runs(run_id) ON DELETE RESTRICT,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'replay' CHECK (data_mode = 'replay'),
  namespace_generation BIGINT NOT NULL CHECK (namespace_generation >= 1),
  arm_name TEXT NOT NULL,
  configuration_sha256 TEXT NOT NULL CHECK (
    configuration_sha256 ~ '^[0-9a-f]{64}$'
  ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (run_id, arm_name),
  UNIQUE (namespace_id, namespace_generation, run_id, arm_id),
  FOREIGN KEY (namespace_id, namespace_generation, run_id)
    REFERENCES backend_replay_runs (
      namespace_id, namespace_generation, run_id
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS backend_service_principals (
  principal_id TEXT PRIMARY KEY,
  database_role_name NAME NOT NULL UNIQUE,
  principal_kind TEXT NOT NULL CHECK (
    principal_kind IN ('bff', 'external_service', 'worker', 'demo_controller')
  ),
  status TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'revoked')),
  credential_generation INTEGER NOT NULL DEFAULT 1 CHECK (
    credential_generation >= 1
  ),
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS backend_actors (
  actor_id TEXT PRIMARY KEY,
  actor_kind TEXT NOT NULL CHECK (actor_kind IN ('human', 'service')),
  status TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'revoked')),
  external_issuer TEXT,
  external_subject TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (external_issuer, external_subject)
);

CREATE TABLE IF NOT EXISTS backend_subjects (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  timezone_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'forgotten')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (namespace_id, data_mode, subject_id),
  FOREIGN KEY (namespace_id, data_mode)
    REFERENCES backend_namespaces (namespace_id, data_mode) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS backend_actor_subject_bindings (
  binding_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  actor_id TEXT NOT NULL REFERENCES backend_actors(actor_id) ON DELETE RESTRICT,
  subject_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('elder', 'family', 'doctor')),
  status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired')),
  purpose_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  scopes_json JSONB NOT NULL,
  authorization_epoch BIGINT NOT NULL DEFAULT 1 CHECK (
    authorization_epoch >= 1
  ),
  valid_from TIMESTAMPTZ NOT NULL,
  valid_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (namespace_id, data_mode, actor_id, subject_id, role),
  FOREIGN KEY (namespace_id, data_mode, subject_id)
    REFERENCES backend_subjects (namespace_id, data_mode, subject_id)
    ON DELETE RESTRICT,
  CHECK (jsonb_typeof(purpose_json) = 'array'),
  CHECK (jsonb_typeof(scopes_json) = 'array'),
  CHECK (valid_until IS NULL OR valid_until > valid_from)
);

CREATE TABLE IF NOT EXISTS backend_principal_grants (
  grant_id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL
    REFERENCES backend_service_principals(principal_id) ON DELETE RESTRICT,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  purpose TEXT NOT NULL,
  scopes_json JSONB NOT NULL,
  allowed_handlers_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  authorization_epoch BIGINT NOT NULL DEFAULT 1 CHECK (
    authorization_epoch >= 1
  ),
  status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired')),
  valid_from TIMESTAMPTZ NOT NULL,
  valid_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (principal_id, namespace_id, data_mode, purpose),
  FOREIGN KEY (namespace_id, data_mode)
    REFERENCES backend_namespaces (namespace_id, data_mode) ON DELETE RESTRICT,
  CHECK (jsonb_typeof(scopes_json) = 'array'),
  CHECK (jsonb_typeof(allowed_handlers_json) = 'array'),
  CHECK (valid_until IS NULL OR valid_until > valid_from)
);

CREATE TABLE IF NOT EXISTS backend_subject_epochs (
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  authorization_epoch BIGINT NOT NULL DEFAULT 1 CHECK (
    authorization_epoch >= 1
  ),
  privacy_epoch BIGINT NOT NULL DEFAULT 1 CHECK (privacy_epoch >= 1),
  retrieval_policy_epoch BIGINT NOT NULL DEFAULT 1 CHECK (
    retrieval_policy_epoch >= 1
  ),
  cas_version BIGINT NOT NULL DEFAULT 1 CHECK (cas_version >= 1),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (namespace_id, data_mode, subject_id),
  FOREIGN KEY (namespace_id, data_mode, subject_id)
    REFERENCES backend_subjects (namespace_id, data_mode, subject_id)
    ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS backend_authorization_audit (
  audit_id TEXT PRIMARY KEY,
  namespace_id TEXT NOT NULL,
  data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'replay')),
  subject_id TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  actor_id TEXT,
  binding_id TEXT,
  decision TEXT NOT NULL CHECK (decision IN ('allow', 'deny', 'revoke')),
  reason_code TEXT NOT NULL,
  policy_sha256 TEXT NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  authorization_epoch BIGINT NOT NULL,
  privacy_epoch BIGINT NOT NULL,
  retrieval_policy_epoch BIGINT NOT NULL,
  audit_json JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  UNIQUE (namespace_id, data_mode, audit_id)
);

CREATE OR REPLACE FUNCTION sleepagent_scope_setting(setting_name TEXT)
RETURNS TEXT
LANGUAGE SQL
STABLE
PARALLEL SAFE
AS $$
  SELECT NULLIF(current_setting(setting_name, TRUE), '')
$$;

CREATE OR REPLACE FUNCTION sleepagent_principal_context_allows()
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT EXISTS (
    SELECT 1
    FROM public.backend_service_principals AS principal
    WHERE principal.principal_id =
      NULLIF(current_setting('sleepagent.service_principal_id', TRUE), '')
      AND principal.database_role_name::text = session_user::text
      AND principal.status = 'active'
      AND (
        (NULLIF(current_setting('sleepagent.process_role', TRUE), '') =
          'worker' AND principal.principal_kind = 'worker')
        OR
        (NULLIF(current_setting('sleepagent.process_role', TRUE), '') = 'api'
          AND principal.principal_kind IN (
            'bff', 'external_service', 'demo_controller'
          ))
      )
  )
$$;

CREATE OR REPLACE FUNCTION sleepagent_namespace_scope_allows(
  row_namespace_id TEXT,
  row_data_mode TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT
    public.sleepagent_principal_context_allows()
    AND NULLIF(current_setting('sleepagent.namespace_id', TRUE), '') =
      row_namespace_id
    AND NULLIF(current_setting('sleepagent.data_mode', TRUE), '') =
      row_data_mode
    AND EXISTS (
      SELECT 1
      FROM public.backend_principal_grants AS grant_row
      WHERE grant_row.principal_id = NULLIF(
          current_setting('sleepagent.service_principal_id', TRUE), ''
        )
        AND grant_row.namespace_id = row_namespace_id
        AND grant_row.data_mode = row_data_mode
        AND grant_row.purpose = NULLIF(
          current_setting('sleepagent.purpose', TRUE), ''
        )
        AND grant_row.authorization_epoch::text = NULLIF(
          current_setting('sleepagent.authorization_epoch', TRUE), ''
        )
        AND grant_row.status = 'active'
        AND grant_row.valid_from <= CURRENT_TIMESTAMP
        AND (
          grant_row.valid_until IS NULL
          OR grant_row.valid_until > CURRENT_TIMESTAMP
        )
    )
$$;

CREATE OR REPLACE FUNCTION sleepagent_namespace_generation_scope_allows(
  row_namespace_id TEXT,
  row_data_mode TEXT,
  row_namespace_generation BIGINT,
  row_run_id TEXT,
  row_arm_id TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT
    public.sleepagent_namespace_scope_allows(row_namespace_id, row_data_mode)
    AND row_namespace_generation::text = NULLIF(
      current_setting('sleepagent.namespace_generation', TRUE), ''
    )
    AND EXISTS (
      SELECT 1
      FROM public.backend_namespace_generations AS generation_row
      WHERE generation_row.namespace_id = row_namespace_id
        AND generation_row.data_mode = row_data_mode
        AND generation_row.generation = row_namespace_generation
        AND generation_row.status IN ('active', 'sealed')
    )
    AND (
      (row_data_mode = 'live'
        AND row_run_id IS NULL
        AND row_arm_id IS NULL
        AND NULLIF(current_setting('sleepagent.run_id', TRUE), '') IS NULL
        AND NULLIF(current_setting('sleepagent.arm_id', TRUE), '') IS NULL)
      OR
      (row_data_mode = 'replay'
        AND row_run_id = NULLIF(
          current_setting('sleepagent.run_id', TRUE), ''
        )
        AND row_arm_id = NULLIF(
          current_setting('sleepagent.arm_id', TRUE), ''
        )
        AND EXISTS (
          SELECT 1
          FROM public.backend_replay_arms AS replay_arm
          WHERE replay_arm.namespace_id = row_namespace_id
            AND replay_arm.data_mode = row_data_mode
            AND replay_arm.namespace_generation = row_namespace_generation
            AND replay_arm.run_id = row_run_id
            AND replay_arm.arm_id = row_arm_id
        ))
    )
$$;

CREATE OR REPLACE FUNCTION sleepagent_subject_scope_allows(
  row_namespace_id TEXT,
  row_data_mode TEXT,
  row_subject_id TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT
    public.sleepagent_namespace_scope_allows(row_namespace_id, row_data_mode)
    AND NULLIF(current_setting('sleepagent.subject_id', TRUE), '') =
      row_subject_id
    AND EXISTS (
      SELECT 1
      FROM public.backend_subject_epochs AS epoch_row
      WHERE epoch_row.namespace_id = row_namespace_id
        AND epoch_row.data_mode = row_data_mode
        AND epoch_row.subject_id = row_subject_id
        AND epoch_row.authorization_epoch::text = NULLIF(
          current_setting('sleepagent.authorization_epoch', TRUE), ''
        )
        AND epoch_row.privacy_epoch::text = NULLIF(
          current_setting('sleepagent.privacy_epoch', TRUE), ''
        )
        AND epoch_row.retrieval_policy_epoch::text = NULLIF(
          current_setting('sleepagent.retrieval_policy_epoch', TRUE), ''
        )
    )
    AND (
      (NULLIF(current_setting('sleepagent.process_role', TRUE), '') = 'worker'
        AND NULLIF(current_setting('sleepagent.actor_id', TRUE), '') IS NULL)
      OR
      (NULLIF(current_setting('sleepagent.process_role', TRUE), '') = 'api'
        AND EXISTS (
          SELECT 1
          FROM public.backend_actors AS actor_row
          JOIN public.backend_actor_subject_bindings AS binding_row
            ON binding_row.actor_id = actor_row.actor_id
          WHERE actor_row.actor_id = NULLIF(
              current_setting('sleepagent.actor_id', TRUE), ''
            )
            AND actor_row.status = 'active'
            AND binding_row.namespace_id = row_namespace_id
            AND binding_row.data_mode = row_data_mode
            AND binding_row.subject_id = row_subject_id
            AND binding_row.status = 'active'
            AND binding_row.authorization_epoch::text = NULLIF(
              current_setting('sleepagent.authorization_epoch', TRUE), ''
            )
            AND binding_row.valid_from <= CURRENT_TIMESTAMP
            AND (
              binding_row.valid_until IS NULL
              OR binding_row.valid_until > CURRENT_TIMESTAMP
            )
            AND binding_row.purpose_json ? NULLIF(
              current_setting('sleepagent.purpose', TRUE), ''
            )
        ))
    )
$$;

CREATE OR REPLACE FUNCTION sleepagent_subject_generation_scope_allows(
  row_namespace_id TEXT,
  row_data_mode TEXT,
  row_subject_id TEXT,
  row_namespace_generation BIGINT,
  row_run_id TEXT,
  row_arm_id TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT
    public.sleepagent_subject_scope_allows(
      row_namespace_id, row_data_mode, row_subject_id
    )
    AND public.sleepagent_namespace_generation_scope_allows(
      row_namespace_id, row_data_mode, row_namespace_generation,
      row_run_id, row_arm_id
    )
$$;

-- Pre-scope authority resolution is deliberately narrow: the signed actor
-- assertion supplies actor/subject/role, while namespace and epochs come only
-- from active database authority.  Zero or ambiguous matches fail closed.
CREATE OR REPLACE FUNCTION sleepagent_resolve_actor_authority(
  asserted_actor_id TEXT,
  asserted_subject_id TEXT,
  asserted_role TEXT,
  requested_purpose TEXT
)
RETURNS TABLE (
  namespace_id TEXT,
  data_mode TEXT,
  namespace_generation BIGINT,
  run_id TEXT,
  arm_id TEXT,
  binding_id TEXT,
  role TEXT,
  effective_scopes_json JSONB,
  authorization_epoch BIGINT,
  privacy_epoch BIGINT,
  retrieval_policy_epoch BIGINT
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  matched_rows INTEGER;
  trusted_principal TEXT := NULLIF(
    current_setting('sleepagent.service_principal_id', TRUE), ''
  );
  trusted_data_mode TEXT := NULLIF(
    current_setting('sleepagent.data_mode', TRUE), ''
  );
BEGIN
  IF asserted_actor_id IS NULL OR asserted_actor_id = ''
     OR asserted_subject_id IS NULL OR asserted_subject_id = ''
     OR asserted_role NOT IN ('elder', 'family', 'doctor')
     OR requested_purpose IS NULL OR requested_purpose = ''
     OR requested_purpose <> NULLIF(
       current_setting('sleepagent.purpose', TRUE), ''
     )
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'api'
     OR trusted_data_mode NOT IN ('live', 'replay')
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid or untrusted authority resolution context';
  END IF;

  RETURN QUERY
  SELECT grant_row.namespace_id,
    grant_row.data_mode,
    namespace_row.current_generation,
    replay_run.run_id,
    replay_arm.arm_id,
    binding_row.binding_id,
    binding_row.role,
    COALESCE(
      (
        SELECT jsonb_agg(granted.scope_value ORDER BY granted.scope_value)
        FROM jsonb_array_elements_text(
          grant_row.scopes_json
        ) AS granted(scope_value)
        WHERE binding_row.scopes_json ? granted.scope_value
      ),
      '[]'::jsonb
    ),
    epoch_row.authorization_epoch,
    epoch_row.privacy_epoch,
    epoch_row.retrieval_policy_epoch
  FROM public.backend_principal_grants AS grant_row
  JOIN public.backend_namespaces AS namespace_row
    ON namespace_row.namespace_id = grant_row.namespace_id
   AND namespace_row.data_mode = grant_row.data_mode
  JOIN public.backend_actor_subject_bindings AS binding_row
    ON binding_row.namespace_id = grant_row.namespace_id
   AND binding_row.data_mode = grant_row.data_mode
   AND binding_row.actor_id = asserted_actor_id
   AND binding_row.subject_id = asserted_subject_id
   AND binding_row.role = asserted_role
  JOIN public.backend_actors AS actor_row
    ON actor_row.actor_id = binding_row.actor_id
  JOIN public.backend_subjects AS subject_row
    ON subject_row.namespace_id = binding_row.namespace_id
   AND subject_row.data_mode = binding_row.data_mode
   AND subject_row.subject_id = binding_row.subject_id
  JOIN public.backend_subject_epochs AS epoch_row
    ON epoch_row.namespace_id = binding_row.namespace_id
   AND epoch_row.data_mode = binding_row.data_mode
   AND epoch_row.subject_id = binding_row.subject_id
  LEFT JOIN public.backend_replay_runs AS replay_run
    ON grant_row.data_mode = 'replay'
   AND replay_run.namespace_id = grant_row.namespace_id
   AND replay_run.namespace_generation = namespace_row.current_generation
   AND replay_run.status = 'active'
  LEFT JOIN public.backend_replay_arms AS replay_arm
    ON replay_arm.namespace_id = replay_run.namespace_id
   AND replay_arm.namespace_generation = replay_run.namespace_generation
   AND replay_arm.run_id = replay_run.run_id
  WHERE grant_row.principal_id = trusted_principal
    AND grant_row.data_mode = trusted_data_mode
    AND grant_row.purpose = requested_purpose
    AND grant_row.status = 'active'
    AND grant_row.authorization_epoch = epoch_row.authorization_epoch
    AND grant_row.valid_from <= CURRENT_TIMESTAMP
    AND (
      grant_row.valid_until IS NULL
      OR grant_row.valid_until > CURRENT_TIMESTAMP
    )
    AND namespace_row.status = 'active'
    AND actor_row.status = 'active'
    AND subject_row.status = 'active'
    AND binding_row.status = 'active'
    AND binding_row.authorization_epoch = epoch_row.authorization_epoch
    AND binding_row.purpose_json ? requested_purpose
    AND binding_row.valid_from <= CURRENT_TIMESTAMP
    AND (
      binding_row.valid_until IS NULL
      OR binding_row.valid_until > CURRENT_TIMESTAMP
    )
    AND (
      (grant_row.data_mode = 'live'
        AND replay_run.run_id IS NULL AND replay_arm.arm_id IS NULL)
      OR (grant_row.data_mode = 'replay'
        AND replay_run.run_id IS NOT NULL AND replay_arm.arm_id IS NOT NULL)
    );
  GET DIAGNOSTICS matched_rows = ROW_COUNT;
  IF matched_rows <> 1 THEN
    RAISE EXCEPTION 'authority resolution returned % rows', matched_rows;
  END IF;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_resolve_actor_authority(
  TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;

-- Assertion replay protection remains globally unique by issuer/JTI and
-- issuer/nonce.  API roles have no direct table privilege; this narrow entry
-- point binds consumption to a registered database principal and uses the
-- PostgreSQL control clock for both expiry and consumption time.
CREATE OR REPLACE FUNCTION sleepagent_consume_actor_assertion(
  asserted_issuer TEXT,
  asserted_jti TEXT,
  asserted_nonce TEXT,
  asserted_expires_at TIMESTAMPTZ
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  inserted_rows INTEGER;
  control_now TIMESTAMPTZ := clock_timestamp();
BEGIN
  IF asserted_issuer IS NULL OR asserted_issuer = ''
     OR asserted_jti IS NULL OR asserted_jti = ''
     OR asserted_nonce IS NULL OR asserted_nonce = ''
     OR asserted_expires_at IS NULL
     OR asserted_expires_at <= control_now
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'api'
     OR COALESCE(
       NULLIF(current_setting('sleepagent.data_mode', TRUE), ''), ''
     ) NOT IN ('live', 'replay')
     OR NULLIF(current_setting('sleepagent.purpose', TRUE), '') IS NULL
     OR NOT public.sleepagent_principal_context_allows() THEN
    RAISE EXCEPTION 'invalid or untrusted actor assertion context';
  END IF;

  DELETE FROM public.sleep_api_actor_assertion_replays
  WHERE expires_at <= control_now;

  INSERT INTO public.sleep_api_actor_assertion_replays (
    issuer, assertion_id, nonce, expires_at, consumed_at
  ) VALUES (
    asserted_issuer, asserted_jti, asserted_nonce,
    asserted_expires_at, control_now
  )
  ON CONFLICT DO NOTHING;
  GET DIAGNOSTICS inserted_rows = ROW_COUNT;
  RETURN inserted_rows = 1;
END;
$$;

REVOKE ALL ON FUNCTION sleepagent_consume_actor_assertion(
  TEXT, TEXT, TEXT, TIMESTAMPTZ
) FROM PUBLIC;

-- Existing product-state tables predate namespace columns.  New production
-- writes must populate these columns; null legacy rows remain migration-owner
-- only until an explicit compatibility migration has scoped them.
ALTER TABLE product_habit_profile_states
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_habit_profile_states
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_care_context_states
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_care_context_states
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_memory_context_states
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_memory_context_states
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_episode_result_revisions
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_episode_result_revisions
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_induction_manifests
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_induction_manifests
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_episode_digests
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_episode_digests
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_memory_read_receipts
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_memory_read_receipts
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_pending_profile_candidates
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_pending_profile_candidates
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_human_decisions
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_human_decisions
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_pending_habit_change_sets
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_pending_habit_change_sets
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_longitudinal_subject_epochs
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_longitudinal_subject_epochs
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );
ALTER TABLE product_offline_skill_outcomes
  ADD COLUMN IF NOT EXISTS namespace_id TEXT;
ALTER TABLE product_offline_skill_outcomes
  ADD COLUMN IF NOT EXISTS data_mode TEXT CHECK (
    data_mode IS NULL OR data_mode IN ('live', 'replay')
  );

-- RLS is deliberately forced: API and Worker roles must always set exact
-- transaction-local scope; only the migration owner may bypass it.
ALTER TABLE backend_namespaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_namespaces FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_namespaces_scope ON backend_namespaces;
CREATE POLICY backend_namespaces_scope ON backend_namespaces
  USING (sleepagent_namespace_scope_allows(namespace_id, data_mode))
  WITH CHECK (sleepagent_namespace_scope_allows(namespace_id, data_mode));

ALTER TABLE backend_namespace_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_namespace_generations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_namespace_generations_scope
  ON backend_namespace_generations;
CREATE POLICY backend_namespace_generations_scope
  ON backend_namespace_generations
  USING (
    sleepagent_namespace_scope_allows(namespace_id, data_mode)
    AND generation::text = sleepagent_scope_setting(
      'sleepagent.namespace_generation'
    )
  )
  WITH CHECK (
    sleepagent_namespace_scope_allows(namespace_id, data_mode)
    AND generation::text = sleepagent_scope_setting(
      'sleepagent.namespace_generation'
    )
  );

ALTER TABLE backend_replay_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_replay_runs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_replay_runs_scope ON backend_replay_runs;
CREATE POLICY backend_replay_runs_scope ON backend_replay_runs
  USING (
    sleepagent_namespace_scope_allows(namespace_id, data_mode)
    AND namespace_generation::text = sleepagent_scope_setting(
      'sleepagent.namespace_generation'
    )
    AND run_id = sleepagent_scope_setting('sleepagent.run_id')
  )
  WITH CHECK (
    sleepagent_namespace_scope_allows(namespace_id, data_mode)
    AND namespace_generation::text = sleepagent_scope_setting(
      'sleepagent.namespace_generation'
    )
    AND run_id = sleepagent_scope_setting('sleepagent.run_id')
  );

ALTER TABLE backend_replay_arms ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_replay_arms FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_replay_arms_scope ON backend_replay_arms;
CREATE POLICY backend_replay_arms_scope ON backend_replay_arms
  USING (
    sleepagent_namespace_generation_scope_allows(
      namespace_id, data_mode, namespace_generation, run_id, arm_id
    )
  )
  WITH CHECK (
    sleepagent_namespace_generation_scope_allows(
      namespace_id, data_mode, namespace_generation, run_id, arm_id
    )
  );

ALTER TABLE backend_service_principals ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_service_principals FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_service_principal_self
  ON backend_service_principals;
CREATE POLICY backend_service_principal_self ON backend_service_principals
  USING (
    sleepagent_principal_context_allows()
    AND
    principal_id = sleepagent_scope_setting('sleepagent.service_principal_id')
  );

ALTER TABLE backend_actors ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_actors FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_actor_self ON backend_actors;
CREATE POLICY backend_actor_self ON backend_actors
  USING (
    sleepagent_principal_context_allows()
    AND actor_id = sleepagent_scope_setting('sleepagent.actor_id')
  );

ALTER TABLE backend_subjects ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_subjects FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_subjects_scope ON backend_subjects;
CREATE POLICY backend_subjects_scope ON backend_subjects
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE backend_actor_subject_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_actor_subject_bindings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_actor_subject_binding_scope
  ON backend_actor_subject_bindings;
CREATE POLICY backend_actor_subject_binding_scope
  ON backend_actor_subject_bindings
  USING (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
    AND (
      actor_id = sleepagent_scope_setting('sleepagent.actor_id')
      OR sleepagent_scope_setting('sleepagent.process_role') = 'worker'
    )
  )
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE backend_principal_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_principal_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_principal_grant_scope
  ON backend_principal_grants;
CREATE POLICY backend_principal_grant_scope ON backend_principal_grants
  USING (
    sleepagent_namespace_scope_allows(namespace_id, data_mode)
    AND principal_id =
      sleepagent_scope_setting('sleepagent.service_principal_id')
  );

ALTER TABLE backend_subject_epochs ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_subject_epochs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_subject_epoch_scope ON backend_subject_epochs;
CREATE POLICY backend_subject_epoch_scope ON backend_subject_epochs
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE backend_authorization_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE backend_authorization_audit FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS backend_authorization_audit_scope
  ON backend_authorization_audit;
CREATE POLICY backend_authorization_audit_scope ON backend_authorization_audit
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

-- Canonical health records.
ALTER TABLE sleep_domain_raw_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_raw_inbox FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_raw_inbox_scope ON sleep_domain_raw_inbox;
CREATE POLICY sleep_domain_raw_inbox_scope ON sleep_domain_raw_inbox
  USING (sleepagent_namespace_scope_allows(namespace_id, data_mode))
  WITH CHECK (sleepagent_namespace_scope_allows(namespace_id, data_mode));

ALTER TABLE sleep_domain_canonical_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_canonical_observations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_observation_scope
  ON sleep_domain_canonical_observations;
CREATE POLICY sleep_domain_observation_scope
  ON sleep_domain_canonical_observations
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE sleep_domain_night_episodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_night_episodes FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_night_episode_scope
  ON sleep_domain_night_episodes;
CREATE POLICY sleep_domain_night_episode_scope ON sleep_domain_night_episodes
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE sleep_domain_night_episode_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_night_episode_revisions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_night_revision_scope
  ON sleep_domain_night_episode_revisions;
CREATE POLICY sleep_domain_night_revision_scope
  ON sleep_domain_night_episode_revisions
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE sleep_domain_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_operations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_operation_scope ON sleep_domain_operations;
CREATE POLICY sleep_domain_operation_scope ON sleep_domain_operations
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE sleep_domain_analysis_role_views ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_analysis_role_views FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_analysis_role_view_scope
  ON sleep_domain_analysis_role_views;
CREATE POLICY sleep_domain_analysis_role_view_scope
  ON sleep_domain_analysis_role_views
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE sleep_domain_domain_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE sleep_domain_domain_outbox FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_domain_outbox_scope
  ON sleep_domain_domain_outbox;
CREATE POLICY sleep_domain_domain_outbox_scope ON sleep_domain_domain_outbox
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

-- Product state carrying newly added scope columns.  Null legacy rows are not
-- visible to API/Worker roles and require an explicit migration-owner adapter.
ALTER TABLE product_habit_profile_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_habit_profile_states FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_habit_profile_scope
  ON product_habit_profile_states;
CREATE POLICY product_habit_profile_scope ON product_habit_profile_states
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_care_context_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_care_context_states FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_care_context_scope
  ON product_care_context_states;
CREATE POLICY product_care_context_scope ON product_care_context_states
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_memory_context_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_memory_context_states FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_memory_context_scope
  ON product_memory_context_states;
CREATE POLICY product_memory_context_scope ON product_memory_context_states
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_episode_result_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_episode_result_revisions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_episode_result_revision_scope
  ON product_episode_result_revisions;
CREATE POLICY product_episode_result_revision_scope
  ON product_episode_result_revisions
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_induction_manifests ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_induction_manifests FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_induction_manifest_scope
  ON product_induction_manifests;
CREATE POLICY product_induction_manifest_scope
  ON product_induction_manifests
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_episode_digests ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_episode_digests FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_episode_digest_scope
  ON product_episode_digests;
CREATE POLICY product_episode_digest_scope ON product_episode_digests
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_memory_read_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_memory_read_receipts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_memory_read_receipt_scope
  ON product_memory_read_receipts;
CREATE POLICY product_memory_read_receipt_scope
  ON product_memory_read_receipts
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_pending_profile_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_pending_profile_candidates FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_pending_profile_candidate_scope
  ON product_pending_profile_candidates;
CREATE POLICY product_pending_profile_candidate_scope
  ON product_pending_profile_candidates
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_human_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_human_decisions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_human_decision_scope
  ON product_human_decisions;
CREATE POLICY product_human_decision_scope ON product_human_decisions
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_pending_habit_change_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_pending_habit_change_sets FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_pending_habit_change_set_scope
  ON product_pending_habit_change_sets;
CREATE POLICY product_pending_habit_change_set_scope
  ON product_pending_habit_change_sets
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_longitudinal_subject_epochs ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_longitudinal_subject_epochs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_longitudinal_subject_epoch_scope
  ON product_longitudinal_subject_epochs;
CREATE POLICY product_longitudinal_subject_epoch_scope
  ON product_longitudinal_subject_epochs
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );

ALTER TABLE product_offline_skill_outcomes ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_offline_skill_outcomes FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_offline_skill_outcome_scope
  ON product_offline_skill_outcomes;
CREATE POLICY product_offline_skill_outcome_scope
  ON product_offline_skill_outcomes
  USING (sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id))
  WITH CHECK (
    sleepagent_subject_scope_allows(namespace_id, data_mode, subject_id)
  );
