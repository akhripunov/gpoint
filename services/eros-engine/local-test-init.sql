-- LOCAL TEST ONLY. Synthetic disposable database roles. Never reuse passwords.
CREATE ROLE eros_ddl NOLOGIN NOSUPERUSER NOCREATEDB NOBYPASSRLS;
CREATE ROLE eros_migrator LOGIN PASSWORD 'local-only-eros-migrator' NOSUPERUSER NOCREATEDB NOBYPASSRLS;
GRANT CONNECT, CREATE ON DATABASE eros_test TO eros_migrator;
-- Stock upstream stores SQLx's migration ledger in public._sqlx_migrations.
-- This permission is confined to the disposable Eros-only database.
GRANT USAGE, CREATE ON SCHEMA public TO eros_migrator;
CREATE ROLE eros_runtime LOGIN PASSWORD 'local-only-eros-runtime' NOSUPERUSER NOCREATEDB BYPASSRLS;
CREATE ROLE eros_runtime_nobypass LOGIN PASSWORD 'local-only-eros-runtime-nobypass' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE gpoint_api_test LOGIN PASSWORD 'local-only-gpoint-api-test' NOSUPERUSER NOCREATEDB BYPASSRLS;
CREATE ROLE gpoint_worker_test LOGIN PASSWORD 'local-only-gpoint-worker-test' NOSUPERUSER NOCREATEDB NOBYPASSRLS;
REVOKE CONNECT ON DATABASE eros_test FROM PUBLIC;
GRANT CONNECT ON DATABASE eros_test TO eros_migrator, eros_runtime, eros_runtime_nobypass;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE SCHEMA engine AUTHORIZATION eros_ddl;
GRANT USAGE, CREATE ON SCHEMA engine TO eros_migrator;
GRANT USAGE ON SCHEMA engine TO eros_runtime, eros_runtime_nobypass;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_migrator IN SCHEMA engine GRANT ALL ON TABLES TO eros_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_migrator IN SCHEMA engine GRANT ALL ON SEQUENCES TO eros_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_migrator IN SCHEMA engine GRANT EXECUTE ON FUNCTIONS TO eros_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_migrator IN SCHEMA engine GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO eros_runtime_nobypass;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_migrator IN SCHEMA engine GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO eros_runtime_nobypass;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA engine TO eros_runtime_nobypass;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA engine TO eros_runtime_nobypass;
-- Stock Eros owns a separate database because its upstream SQLx ledger lives
-- in public. GPoint Prisma must start against a completely empty database.
CREATE DATABASE gpoint_test OWNER eros_test_admin;
\connect gpoint_test
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
GRANT CONNECT ON DATABASE gpoint_test TO gpoint_api_test, gpoint_worker_test;
CREATE SCHEMA engine AUTHORIZATION eros_test_admin;
GRANT USAGE ON SCHEMA public, engine TO gpoint_api_test;
GRANT USAGE ON SCHEMA engine TO gpoint_worker_test;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_test_admin IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO gpoint_api_test;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_test_admin IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO gpoint_api_test;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_test_admin IN SCHEMA engine GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO gpoint_api_test, gpoint_worker_test;
ALTER DEFAULT PRIVILEGES FOR ROLE eros_test_admin IN SCHEMA engine GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO gpoint_api_test, gpoint_worker_test;
\connect eros_test
