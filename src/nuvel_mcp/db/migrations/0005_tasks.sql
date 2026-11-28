-- Tasks (io.modelcontextprotocol/tasks): one row per task, the record as
-- nuvel_mcp.tasks.store describes it. The record's result is a tool's
-- output, so user content: rows go when expires_at passes (the server's
-- task TTL, 24 hours by default), only the creating tenant may read one,
-- and nothing here is logged or relayed.
CREATE TABLE {schema}.tasks (
    task_id     text PRIMARY KEY CHECK (task_id ~ '^[0-9a-f]{32}$'),
    tenant      text,
    expires_at  timestamptz NOT NULL,
    record      jsonb NOT NULL
);
CREATE INDEX tasks_expires_idx ON {schema}.tasks (expires_at);

-- Row-level security, forced, with the one owner policy -- as 0002 does for
-- every table.
DO $$
DECLARE
    owner text;
BEGIN
    SELECT pg_get_userbyid(relowner) INTO owner
    FROM pg_class WHERE oid = '{schema}.tasks'::regclass;
    ALTER TABLE {schema}.tasks ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {schema}.tasks FORCE ROW LEVEL SECURITY;
    EXECUTE format('CREATE POLICY owner_all ON {schema}.tasks TO %I '
                   'USING (true) WITH CHECK (true)', owner);
END $$;

REVOKE ALL ON {schema}.tasks FROM PUBLIC;
