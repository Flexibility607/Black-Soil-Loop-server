\set ON_ERROR_STOP on

SELECT 'CREATE ROLE blacksoil_showcase_owner NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'blacksoil_showcase_owner') \gexec

SELECT format('CREATE ROLE blacksoil_showcase_b01 LOGIN PASSWORD %L', :'showcase_b01_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'blacksoil_showcase_b01') \gexec
SELECT format('CREATE ROLE blacksoil_showcase_b02 LOGIN PASSWORD %L', :'showcase_b02_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'blacksoil_showcase_b02') \gexec
SELECT format('CREATE ROLE blacksoil_showcase_worker LOGIN PASSWORD %L', :'showcase_worker_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'blacksoil_showcase_worker') \gexec

ALTER ROLE blacksoil_showcase_b01 PASSWORD :'showcase_b01_password';
ALTER ROLE blacksoil_showcase_b02 PASSWORD :'showcase_b02_password';
ALTER ROLE blacksoil_showcase_worker PASSWORD :'showcase_worker_password';

REVOKE blacksoil_showcase_owner FROM blacksoil_showcase_b01;
REVOKE blacksoil_showcase_owner FROM blacksoil_showcase_b02;
REVOKE blacksoil_showcase_owner FROM blacksoil_showcase_worker;

SELECT 'CREATE DATABASE black_soil_loop_showcase OWNER blacksoil_showcase_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'black_soil_loop_showcase') \gexec

REVOKE ALL ON DATABASE black_soil_loop_showcase FROM PUBLIC;
GRANT CONNECT ON DATABASE black_soil_loop_showcase
TO blacksoil_showcase_b01, blacksoil_showcase_b02, blacksoil_showcase_worker;
REVOKE CONNECT ON DATABASE black_soil_loop_showcase FROM blacksoil_b01, blacksoil_b02, blacksoil_worker;
REVOKE CONNECT ON DATABASE black_soil_loop
FROM blacksoil_showcase_b01, blacksoil_showcase_b02, blacksoil_showcase_worker;
