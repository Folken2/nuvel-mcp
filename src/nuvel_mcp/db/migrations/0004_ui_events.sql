-- nuvel.ui_event/1, as normalised by traces.normalize: one row per page visit
-- on a plugin surface. Content-free by schema: events maps catalogued event
-- ids to counts, and stays in record.
CREATE TABLE {schema}.ui_events (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant          text,
    run_id          text NOT NULL,
    install_id      text NOT NULL,
    plugin_name     text NOT NULL,
    plugin_version  text,
    surface         text NOT NULL,
    host            text NOT NULL,
    started_at      timestamptz NOT NULL,
    duration_s      integer NOT NULL,
    first_action_s  integer,
    record          jsonb NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ui_events_run_uq ON {schema}.ui_events ((coalesce(tenant, '')), run_id);
CREATE INDEX ui_events_surface_idx ON {schema}.ui_events (plugin_name, surface, started_at);

-- Row-level security, forced, with the one owner policy -- as 0002 does for
-- every table.
DO $$
DECLARE
    owner text;
BEGIN
    SELECT pg_get_userbyid(relowner) INTO owner
    FROM pg_class WHERE oid = '{schema}.ui_events'::regclass;
    ALTER TABLE {schema}.ui_events ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {schema}.ui_events FORCE ROW LEVEL SECURITY;
    EXECUTE format('CREATE POLICY owner_all ON {schema}.ui_events TO %I '
                   'USING (true) WITH CHECK (true)', owner);
END $$;

REVOKE ALL ON {schema}.ui_events FROM PUBLIC;
