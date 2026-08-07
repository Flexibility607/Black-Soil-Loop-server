\set ON_ERROR_STOP on

SELECT 'CREATE ROLE blacksoil_owner NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'blacksoil_owner') \gexec

SELECT format('CREATE ROLE blacksoil_b01 LOGIN PASSWORD %L', :'b01_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'blacksoil_b01') \gexec
SELECT format('CREATE ROLE blacksoil_b02 LOGIN PASSWORD %L', :'b02_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'blacksoil_b02') \gexec
SELECT format('CREATE ROLE blacksoil_worker LOGIN PASSWORD %L', :'worker_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'blacksoil_worker') \gexec

ALTER ROLE blacksoil_b01 PASSWORD :'b01_password';
ALTER ROLE blacksoil_b02 PASSWORD :'b02_password';
ALTER ROLE blacksoil_worker PASSWORD :'worker_password';
REVOKE blacksoil_owner FROM blacksoil_worker;

SELECT 'CREATE DATABASE black_soil_loop OWNER blacksoil_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'black_soil_loop') \gexec

REVOKE ALL ON DATABASE black_soil_loop FROM PUBLIC;
GRANT CONNECT ON DATABASE black_soil_loop TO blacksoil_b01, blacksoil_b02, blacksoil_worker;
