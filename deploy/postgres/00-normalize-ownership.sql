\set ON_ERROR_STOP on
\connect black_soil_loop

-- Older deployments could recreate mapped tables as a service role. Normalize
-- those objects before Alembic runs so migrations always execute as the owner.
REASSIGN OWNED BY blacksoil_b01 TO blacksoil_owner;
REASSIGN OWNED BY blacksoil_b02 TO blacksoil_owner;
REASSIGN OWNED BY blacksoil_worker TO blacksoil_owner;

-- Migrations use the local PostgreSQL peer account; runtime roles never inherit
-- schema-owner privileges.
REVOKE blacksoil_owner FROM blacksoil_worker;
