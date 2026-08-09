-- sleepagent:transactional=true
-- The bootstrap runner validates this file against its release-pinned SHA-256
-- before executing it.  It then attests immutable migrations 001-020.

CREATE TABLE IF NOT EXISTS radar_agent_schema_migrations_v2 (
  version INTEGER PRIMARY KEY CHECK (version >= 1),
  migration_name TEXT NOT NULL UNIQUE,
  sql_sha256 TEXT NOT NULL CHECK (
    sql_sha256 ~ '^[0-9a-f]{64}$'
  ),
  status TEXT NOT NULL CHECK (
    status IN ('legacy_attested', 'started', 'applied', 'failed')
  ),
  transactional BOOLEAN NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  finished_at TIMESTAMPTZ,
  applied_by TEXT NOT NULL,
  error_code TEXT,
  CHECK (
    (status = 'started' AND finished_at IS NULL)
    OR (status IN ('legacy_attested', 'applied', 'failed')
        AND finished_at IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_radar_migration_v2_status
  ON radar_agent_schema_migrations_v2 (status, version);

CREATE OR REPLACE FUNCTION sleepagent_protect_migration_ledger_v2()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'migration ledger rows are immutable';
  END IF;
  IF NEW.version <> OLD.version
     OR NEW.migration_name <> OLD.migration_name
     OR NEW.sql_sha256 <> OLD.sql_sha256
     OR NEW.transactional <> OLD.transactional
     OR NEW.started_at <> OLD.started_at
     OR NEW.applied_by <> OLD.applied_by THEN
    RAISE EXCEPTION 'migration identity is immutable';
  END IF;
  IF OLD.status IN ('legacy_attested', 'applied', 'failed') THEN
    RAISE EXCEPTION 'finished migration rows are immutable';
  END IF;
  IF OLD.status = 'started'
     AND NEW.status NOT IN ('applied', 'failed') THEN
    RAISE EXCEPTION 'started migration may only finish applied or failed';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS radar_migration_v2_immutable
  ON radar_agent_schema_migrations_v2;

CREATE TRIGGER radar_migration_v2_immutable
BEFORE UPDATE OR DELETE ON radar_agent_schema_migrations_v2
FOR EACH ROW EXECUTE FUNCTION sleepagent_protect_migration_ledger_v2();

