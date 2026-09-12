-- sleepagent:transactional=true

-- G7/M12: governed temporal DeviceBinding lifecycle.

ALTER TABLE public.sleep_domain_device_bindings
  ADD COLUMN cas_version BIGINT NOT NULL DEFAULT 0 CHECK (cas_version >= 0),
  ADD COLUMN supersedes_device_binding_id TEXT,
  ADD COLUMN ended_at TIMESTAMPTZ,
  ADD COLUMN revoked_at TIMESTAMPTZ;

ALTER TABLE public.sleep_domain_device_bindings
  ADD CONSTRAINT sleep_domain_binding_status_v2 CHECK (
    status IN ('active', 'ended', 'revoked')
    AND ((status = 'active' AND effective_until IS NULL
          AND ended_at IS NULL AND revoked_at IS NULL)
      OR (status = 'ended' AND effective_until IS NOT NULL
          AND ended_at = effective_until AND revoked_at IS NULL)
      OR (status = 'revoked' AND effective_until IS NOT NULL
          AND revoked_at = effective_until))
  ),
  ADD CONSTRAINT sleep_domain_binding_supersedes_fk
    FOREIGN KEY (supersedes_device_binding_id)
    REFERENCES public.sleep_domain_device_bindings(device_binding_id)
    ON DELETE RESTRICT;

CREATE UNIQUE INDEX ux_sleep_domain_binding_subject_version
  ON public.sleep_domain_device_bindings (
    device_binding_id, namespace_id, data_mode, binding_version, subject_id
  );

ALTER TABLE public.sleep_domain_device_binding_audit
  DROP CONSTRAINT sleep_domain_device_binding_audit_action_check;
ALTER TABLE public.sleep_domain_device_binding_audit
  ADD COLUMN subject_id TEXT,
  ADD COLUMN previous_device_binding_id TEXT,
  ADD COLUMN new_device_binding_id TEXT,
  ADD COLUMN reason TEXT;

UPDATE public.sleep_domain_device_binding_audit AS audit
SET subject_id = binding.subject_id,
    new_device_binding_id = binding.device_binding_id,
    reason = COALESCE(audit.audit_json ->> 'reason', 'historical_import')
FROM public.sleep_domain_device_bindings AS binding
WHERE binding.namespace_id = audit.namespace_id
  AND binding.data_mode = audit.data_mode
  AND binding.device_id = audit.device_id
  AND binding.binding_version = audit.new_binding_version;

ALTER TABLE public.sleep_domain_device_binding_audit
  ALTER COLUMN subject_id SET NOT NULL,
  ALTER COLUMN new_device_binding_id SET NOT NULL,
  ALTER COLUMN reason SET NOT NULL,
  ADD CONSTRAINT sleep_domain_device_binding_audit_action_check CHECK (
    action IN ('created', 'rebound', 'ended', 'revoked', 'validated')
  ),
  ADD CONSTRAINT sleep_domain_binding_audit_previous_fk
    FOREIGN KEY (previous_device_binding_id)
    REFERENCES public.sleep_domain_device_bindings(device_binding_id)
    ON DELETE RESTRICT,
  ADD CONSTRAINT sleep_domain_binding_audit_new_fk
    FOREIGN KEY (new_device_binding_id)
    REFERENCES public.sleep_domain_device_bindings(device_binding_id)
    ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION public.sleepagent_validate_device_binding_v2()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, public
AS $$
DECLARE
  prior public.sleep_domain_device_bindings%ROWTYPE;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_timezone_names
    WHERE name = NEW.timezone_name
  ) THEN
    RAISE EXCEPTION 'invalid IANA timezone';
  END IF;

  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      NEW.namespace_id || chr(31) || NEW.data_mode || chr(31) || NEW.device_id,
      0
    )
  );

  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'active' OR NEW.effective_until IS NOT NULL
       OR NEW.cas_version <> 0 THEN
      RAISE EXCEPTION 'new DeviceBinding must begin active at CAS zero';
    END IF;
    SELECT * INTO prior
    FROM public.sleep_domain_device_bindings AS binding
    WHERE binding.namespace_id = NEW.namespace_id
      AND binding.data_mode = NEW.data_mode
      AND binding.device_id = NEW.device_id
    ORDER BY binding.binding_version DESC
    LIMIT 1;
    IF FOUND AND (
      NEW.binding_version <> prior.binding_version + 1
      OR NEW.supersedes_device_binding_id IS DISTINCT FROM prior.device_binding_id
      OR prior.effective_until IS DISTINCT FROM NEW.effective_from
      OR prior.status NOT IN ('ended', 'revoked')
    ) THEN
      RAISE EXCEPTION 'DeviceBinding temporal version chain is invalid';
    ELSIF NOT FOUND AND (
      NEW.binding_version <> 1 OR NEW.supersedes_device_binding_id IS NOT NULL
    ) THEN
      RAISE EXCEPTION 'first DeviceBinding version must be version one';
    END IF;
  ELSE
    IF ROW(
      NEW.device_binding_id, NEW.namespace_id, NEW.data_mode,
      NEW.binding_version, NEW.device_id, NEW.provider_id,
      NEW.provider_account_id, NEW.subject_id, NEW.timezone_name,
      NEW.effective_from, NEW.supersedes_device_binding_id, NEW.recorded_at
    ) IS DISTINCT FROM ROW(
      OLD.device_binding_id, OLD.namespace_id, OLD.data_mode,
      OLD.binding_version, OLD.device_id, OLD.provider_id,
      OLD.provider_account_id, OLD.subject_id, OLD.timezone_name,
      OLD.effective_from, OLD.supersedes_device_binding_id, OLD.recorded_at
    ) THEN
      RAISE EXCEPTION 'historical DeviceBinding identity is immutable';
    END IF;
    IF OLD.status <> 'active' OR NEW.status NOT IN ('ended', 'revoked')
       OR NEW.effective_until IS NULL
       OR NEW.effective_until <= NEW.effective_from
       OR NEW.cas_version <> OLD.cas_version + 1 THEN
      RAISE EXCEPTION 'DeviceBinding lifecycle transition is invalid';
    END IF;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM public.sleep_domain_device_bindings AS other
    WHERE other.namespace_id = NEW.namespace_id
      AND other.data_mode = NEW.data_mode
      AND other.device_id = NEW.device_id
      AND other.device_binding_id <> NEW.device_binding_id
      AND tstzrange(other.effective_from, other.effective_until, '[)')
          && tstzrange(NEW.effective_from, NEW.effective_until, '[)')
  ) THEN
    RAISE EXCEPTION 'DeviceBinding intervals overlap';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS sleepagent_validate_device_binding_v2_trigger
  ON public.sleep_domain_device_bindings;
CREATE TRIGGER sleepagent_validate_device_binding_v2_trigger
BEFORE INSERT OR UPDATE ON public.sleep_domain_device_bindings
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_validate_device_binding_v2();

CREATE OR REPLACE FUNCTION public.sleepagent_binding_audit_append_only()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'DeviceBinding audit is append-only';
END;
$$;

DROP TRIGGER IF EXISTS sleepagent_binding_audit_append_only_trigger
  ON public.sleep_domain_device_binding_audit;
CREATE TRIGGER sleepagent_binding_audit_append_only_trigger
BEFORE UPDATE OR DELETE ON public.sleep_domain_device_binding_audit
FOR EACH ROW EXECUTE FUNCTION public.sleepagent_binding_audit_append_only();

ALTER TABLE public.sleep_domain_device_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sleep_domain_device_identities FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_device_identity_scope
  ON public.sleep_domain_device_identities;
CREATE POLICY sleep_domain_device_identity_scope
  ON public.sleep_domain_device_identities
  USING (public.sleepagent_namespace_scope_allows(namespace_id, data_mode))
  WITH CHECK (public.sleepagent_namespace_scope_allows(namespace_id, data_mode));

ALTER TABLE public.sleep_domain_device_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sleep_domain_device_bindings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_device_binding_scope
  ON public.sleep_domain_device_bindings;
CREATE POLICY sleep_domain_device_binding_scope
  ON public.sleep_domain_device_bindings
  USING (public.sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ))
  WITH CHECK (public.sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ));

ALTER TABLE public.sleep_domain_device_binding_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sleep_domain_device_binding_audit FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sleep_domain_device_binding_audit_scope
  ON public.sleep_domain_device_binding_audit;
CREATE POLICY sleep_domain_device_binding_audit_scope
  ON public.sleep_domain_device_binding_audit
  USING (public.sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ))
  WITH CHECK (public.sleepagent_subject_scope_allows(
    namespace_id, data_mode, subject_id
  ));

REVOKE ALL ON FUNCTION public.sleepagent_validate_device_binding_v2()
  FROM PUBLIC;
REVOKE ALL ON FUNCTION public.sleepagent_binding_audit_append_only()
  FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.sleepagent_manage_device_binding(
  requested_command_id TEXT,
  requested_action TEXT,
  requested_current_binding_id TEXT,
  requested_expected_cas BIGINT,
  requested_new_binding_id TEXT,
  requested_device_id TEXT,
  requested_provider_id TEXT,
  requested_provider_account_id TEXT,
  requested_provider_device_key TEXT,
  requested_provider_device_json JSONB,
  requested_subject_id TEXT,
  requested_timezone_name TEXT,
  requested_effective_at TIMESTAMPTZ,
  requested_actor_id TEXT,
  requested_authorization_id TEXT,
  requested_reason TEXT,
  requested_binding_json JSONB,
  requested_audit_json JSONB
)
RETURNS TABLE (
  device_binding_id TEXT,
  binding_version INTEGER,
  status TEXT,
  cas_version BIGINT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
  namespace_value TEXT := NULLIF(
    current_setting('sleepagent.namespace_id', TRUE), ''
  );
  mode_value TEXT := NULLIF(current_setting('sleepagent.data_mode', TRUE), '');
  actor_value TEXT := NULLIF(current_setting('sleepagent.actor_id', TRUE), '');
  purpose_value TEXT := NULLIF(current_setting('sleepagent.purpose', TRUE), '');
  current_row public.sleep_domain_device_bindings%ROWTYPE;
  replay_row public.sleep_domain_device_binding_audit%ROWTYPE;
  next_version INTEGER;
  transition_status TEXT;
BEGIN
  IF NOT public.sleepagent_principal_context_allows()
     OR NULLIF(current_setting('sleepagent.process_role', TRUE), '') <> 'api'
     OR namespace_value IS NULL OR mode_value NOT IN ('live', 'replay')
     OR actor_value IS DISTINCT FROM requested_actor_id
     OR purpose_value <> 'device_binding_management'
     OR requested_action NOT IN ('created', 'rebound', 'ended', 'revoked', 'validated')
     OR requested_command_id IS NULL OR requested_command_id = ''
     OR requested_device_id IS NULL OR requested_device_id = ''
     OR requested_subject_id IS NULL OR requested_subject_id = ''
     OR requested_effective_at IS NULL
     OR requested_authorization_id IS NULL OR requested_authorization_id = ''
     OR requested_reason IS NULL OR requested_reason = ''
     OR NOT public.sleepagent_subject_scope_allows(
       namespace_value, mode_value, requested_subject_id
     ) THEN
    RAISE EXCEPTION 'invalid or unauthorized DeviceBinding command';
  END IF;

  SELECT * INTO replay_row
  FROM public.sleep_domain_device_binding_audit AS audit
  WHERE audit.namespace_id = namespace_value
    AND audit.data_mode = mode_value
    AND audit.command_id = requested_command_id;
  IF FOUND THEN
    IF replay_row.action <> requested_action
       OR replay_row.actor_id <> requested_actor_id
       OR replay_row.audit_json ->> 'command_sha256'
          IS DISTINCT FROM requested_audit_json ->> 'command_sha256' THEN
      RAISE EXCEPTION 'DeviceBinding command id conflicts with prior command';
    END IF;
    RETURN QUERY
    SELECT binding.device_binding_id, binding.binding_version,
      binding.status, binding.cas_version
    FROM public.sleep_domain_device_bindings AS binding
    WHERE binding.device_binding_id = replay_row.new_device_binding_id;
    RETURN;
  END IF;

  INSERT INTO public.sleep_domain_device_identities (
    namespace_id, data_mode, provider_id, provider_account_id,
    provider_device_key, device_id, provider_device_json, created_at
  ) VALUES (
    namespace_value, mode_value, requested_provider_id,
    requested_provider_account_id, requested_provider_device_key,
    requested_device_id, requested_provider_device_json, clock_timestamp()
  ) ON CONFLICT (
    namespace_id, data_mode, provider_id, provider_account_id,
    provider_device_key
  ) DO NOTHING;
  IF NOT EXISTS (
    SELECT 1 FROM public.sleep_domain_device_identities AS identity
    WHERE identity.namespace_id = namespace_value
      AND identity.data_mode = mode_value
      AND identity.provider_id = requested_provider_id
      AND identity.provider_account_id = requested_provider_account_id
      AND identity.provider_device_key = requested_provider_device_key
      AND identity.device_id = requested_device_id
      AND identity.provider_device_json = requested_provider_device_json
  ) THEN
    RAISE EXCEPTION 'provider device identity conflicts with prior discovery';
  END IF;

  IF requested_action = 'created' THEN
    IF requested_current_binding_id IS NOT NULL
       OR requested_expected_cas IS NOT NULL
       OR requested_new_binding_id IS NULL THEN
      RAISE EXCEPTION 'create DeviceBinding command shape is invalid';
    END IF;
    next_version := 1;
    INSERT INTO public.sleep_domain_device_bindings (
      device_binding_id, namespace_id, data_mode, binding_version,
      device_id, provider_id, provider_account_id, subject_id,
      timezone_name, effective_from, effective_until, status,
      binding_json, recorded_at, cas_version
    ) VALUES (
      requested_new_binding_id, namespace_value, mode_value, next_version,
      requested_device_id, requested_provider_id,
      requested_provider_account_id, requested_subject_id,
      requested_timezone_name, requested_effective_at, NULL, 'active',
      requested_binding_json, clock_timestamp(), 0
    );
    transition_status := 'active';
  ELSE
    IF requested_current_binding_id IS NULL OR requested_expected_cas IS NULL THEN
      RAISE EXCEPTION 'DeviceBinding transition requires current binding and CAS';
    END IF;
    SELECT * INTO current_row
    FROM public.sleep_domain_device_bindings AS binding
    WHERE binding.device_binding_id = requested_current_binding_id
      AND binding.namespace_id = namespace_value
      AND binding.data_mode = mode_value
    FOR UPDATE;
    IF NOT FOUND OR current_row.device_id <> requested_device_id
       OR current_row.cas_version <> requested_expected_cas THEN
      RAISE EXCEPTION 'DeviceBinding CAS conflict';
    END IF;
    IF current_row.subject_id <> requested_subject_id AND NOT EXISTS (
      SELECT 1
      FROM public.backend_actor_subject_bindings AS authority
      WHERE authority.actor_id = requested_actor_id
        AND authority.namespace_id = namespace_value
        AND authority.data_mode = mode_value
        AND authority.subject_id = current_row.subject_id
        AND authority.status = 'active'
        AND authority.valid_from <= clock_timestamp()
        AND (authority.valid_until IS NULL
          OR authority.valid_until > clock_timestamp())
        AND authority.purpose_json ? 'device_binding_management'
    ) THEN
      RAISE EXCEPTION 'DeviceBinding transfer lacks prior-subject authority';
    END IF;

    IF requested_action = 'rebound' THEN
      IF requested_new_binding_id IS NULL
         OR requested_effective_at <= current_row.effective_from THEN
        RAISE EXCEPTION 'rebind DeviceBinding command shape is invalid';
      END IF;
      UPDATE public.sleep_domain_device_bindings AS binding
      SET status = 'ended', effective_until = requested_effective_at,
          ended_at = requested_effective_at,
          binding_json = jsonb_set(
            jsonb_set(current_row.binding_json, '{status}', '"ended"'::jsonb, TRUE),
            '{effective_until}', to_jsonb(requested_effective_at), TRUE
          ),
          cas_version = binding.cas_version + 1
      WHERE binding.device_binding_id = current_row.device_binding_id;
      next_version := current_row.binding_version + 1;
      INSERT INTO public.sleep_domain_device_bindings (
        device_binding_id, namespace_id, data_mode, binding_version,
        device_id, provider_id, provider_account_id, subject_id,
        timezone_name, effective_from, effective_until, status,
        binding_json, recorded_at, cas_version,
        supersedes_device_binding_id
      ) VALUES (
        requested_new_binding_id, namespace_value, mode_value, next_version,
        requested_device_id, requested_provider_id,
        requested_provider_account_id, requested_subject_id,
        requested_timezone_name, requested_effective_at, NULL, 'active',
        requested_binding_json, clock_timestamp(), 0,
        current_row.device_binding_id
      );
      transition_status := 'active';
    ELSIF requested_action IN ('ended', 'revoked') THEN
      transition_status := CASE
        WHEN requested_action = 'ended' THEN 'ended' ELSE 'revoked'
      END;
      UPDATE public.sleep_domain_device_bindings AS binding
      SET status = transition_status,
          effective_until = requested_effective_at,
          ended_at = CASE WHEN transition_status = 'ended'
            THEN requested_effective_at ELSE NULL END,
          revoked_at = CASE WHEN transition_status = 'revoked'
            THEN requested_effective_at ELSE NULL END,
          binding_json = jsonb_set(
            jsonb_set(requested_binding_json, '{status}', to_jsonb(transition_status), TRUE),
            '{effective_until}', to_jsonb(requested_effective_at), TRUE
          ),
          cas_version = binding.cas_version + 1
      WHERE binding.device_binding_id = current_row.device_binding_id;
      requested_new_binding_id := current_row.device_binding_id;
      next_version := current_row.binding_version;
    ELSE
      requested_new_binding_id := current_row.device_binding_id;
      next_version := current_row.binding_version;
      transition_status := current_row.status;
    END IF;
  END IF;

  INSERT INTO public.sleep_domain_device_binding_audit (
    audit_event_id, namespace_id, data_mode, command_id, action,
    provider_id, provider_account_id, provider_device_key, device_id,
    previous_binding_version, new_binding_version, actor_id,
    authorization_id, audit_json, occurred_at, subject_id,
    previous_device_binding_id, new_device_binding_id, reason
  ) VALUES (
    'binding-audit:' || encode(digest(convert_to(
      namespace_value || chr(31) || mode_value || chr(31) || requested_command_id,
      'UTF8'), 'sha256'), 'hex'),
    namespace_value, mode_value, requested_command_id, requested_action,
    requested_provider_id, requested_provider_account_id,
    requested_provider_device_key, requested_device_id,
    CASE WHEN requested_action = 'created' THEN NULL
      ELSE current_row.binding_version END,
    next_version, requested_actor_id, requested_authorization_id,
    requested_audit_json, clock_timestamp(), requested_subject_id,
    CASE WHEN requested_action = 'created' THEN NULL
      ELSE current_row.device_binding_id END,
    requested_new_binding_id, requested_reason
  );

  RETURN QUERY
  SELECT binding.device_binding_id, binding.binding_version,
    binding.status, binding.cas_version
  FROM public.sleep_domain_device_bindings AS binding
  WHERE binding.device_binding_id = requested_new_binding_id;
END;
$$;

REVOKE ALL ON FUNCTION public.sleepagent_manage_device_binding(
  TEXT, TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB,
  TEXT, TEXT, TIMESTAMPTZ, TEXT, TEXT, TEXT, JSONB, JSONB
) FROM PUBLIC;
