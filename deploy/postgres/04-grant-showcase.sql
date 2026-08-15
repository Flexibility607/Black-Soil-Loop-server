\set ON_ERROR_STOP on
\connect black_soil_loop_showcase

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA iam, core, b01, b02, integration FROM PUBLIC;

GRANT USAGE ON SCHEMA iam, core, b01, integration TO blacksoil_showcase_b01;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA iam, core, b01, integration
TO blacksoil_showcase_b01;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA iam, core, b01, integration
TO blacksoil_showcase_b01;

GRANT USAGE ON SCHEMA iam, core, b02, integration TO blacksoil_showcase_b02;
GRANT SELECT ON ALL TABLES IN SCHEMA core TO blacksoil_showcase_b02;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA iam, b02, integration
TO blacksoil_showcase_b02;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA iam, b02, integration TO blacksoil_showcase_b02;

GRANT USAGE ON SCHEMA iam, core, b01, b02, integration TO blacksoil_showcase_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA iam, core, b01, b02, integration
TO blacksoil_showcase_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA iam, core, b01, b02, integration
TO blacksoil_showcase_worker;

ALTER DEFAULT PRIVILEGES FOR ROLE blacksoil_showcase_owner IN SCHEMA iam, core, b01, integration
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO blacksoil_showcase_b01;
ALTER DEFAULT PRIVILEGES FOR ROLE blacksoil_showcase_owner IN SCHEMA iam, core, b01, integration
GRANT USAGE, SELECT ON SEQUENCES TO blacksoil_showcase_b01;
ALTER DEFAULT PRIVILEGES FOR ROLE blacksoil_showcase_owner IN SCHEMA core
GRANT SELECT ON TABLES TO blacksoil_showcase_b02;
ALTER DEFAULT PRIVILEGES FOR ROLE blacksoil_showcase_owner IN SCHEMA iam, b02, integration
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO blacksoil_showcase_b02;
ALTER DEFAULT PRIVILEGES FOR ROLE blacksoil_showcase_owner IN SCHEMA iam, b02, integration
GRANT USAGE, SELECT ON SEQUENCES TO blacksoil_showcase_b02;
ALTER DEFAULT PRIVILEGES FOR ROLE blacksoil_showcase_owner IN SCHEMA iam, core, b01, b02, integration
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO blacksoil_showcase_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE blacksoil_showcase_owner IN SCHEMA iam, core, b01, b02, integration
GRANT USAGE, SELECT ON SEQUENCES TO blacksoil_showcase_worker;

REVOKE INSERT, UPDATE, DELETE ON TABLE b01.dashboard_map_points FROM blacksoil_showcase_b01;
GRANT SELECT ON TABLE b01.dashboard_map_points TO blacksoil_showcase_b01;
