-- sleepagent:transactional=true

-- Existing deployments already have bounded API roles.  Migration 030
-- introduced the SECURITY DEFINER boundary; grant only that function to the
-- registered API-capable roles while leaving quarantine and audit tables
-- inaccessible directly.

REVOKE ALL ON FUNCTION
  public.sleepagent_requeue_perceptor_pull_quarantine(
    TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TIMESTAMPTZ
  ) FROM PUBLIC;

DO $$
DECLARE
  api_role NAME;
BEGIN
  FOR api_role IN
    SELECT DISTINCT principal.database_role_name
    FROM public.backend_service_principals AS principal
    WHERE principal.principal_kind IN (
      'bff', 'external_service', 'demo_controller'
    )
  LOOP
    EXECUTE format(
      'GRANT EXECUTE ON FUNCTION public.sleepagent_requeue_perceptor_pull_quarantine(TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TIMESTAMPTZ) TO %I',
      api_role
    );
  END LOOP;
END;
$$;

DO $$
DECLARE
  missing_count BIGINT;
  public_execute BOOLEAN;
BEGIN
  SELECT count(*) INTO missing_count
  FROM public.backend_service_principals AS principal
  WHERE principal.principal_kind IN (
      'bff', 'external_service', 'demo_controller'
    )
    AND NOT has_function_privilege(
      principal.database_role_name,
      'public.sleepagent_requeue_perceptor_pull_quarantine(text,text,text,text,text,text,timestamptz)',
      'EXECUTE'
    );
  SELECT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_proc AS function_row
    CROSS JOIN LATERAL pg_catalog.aclexplode(
      COALESCE(
        function_row.proacl,
        pg_catalog.acldefault('f', function_row.proowner)
      )
    ) AS privilege_row
    WHERE function_row.oid =
      'public.sleepagent_requeue_perceptor_pull_quarantine(text,text,text,text,text,text,timestamptz)'::regprocedure
      AND privilege_row.grantee = 0
      AND privilege_row.privilege_type = 'EXECUTE'
  ) INTO public_execute;
  IF missing_count <> 0
     OR public_execute THEN
    RAISE EXCEPTION 'Pull quarantine reprocess function grant is incomplete';
  END IF;
END;
$$;
