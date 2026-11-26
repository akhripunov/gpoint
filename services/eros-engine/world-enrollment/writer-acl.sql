-- PROPOSED protected ACL contract, NOT a migration and not auto-applied.
-- Run only as authorized cluster administrator against verified dedicated Eros DB.
-- New role starts NOLOGIN; provision password/login through protected secrets
-- lifecycle only after exact ACL and isolation verification.
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $$ BEGIN
  IF current_database() <> 'eros' OR EXISTS(SELECT FROM pg_roles WHERE rolname='eros_world_writer')
  THEN RAISE EXCEPTION 'Expected verified Eros database and new writer role'; END IF;
END $$;
CREATE ROLE eros_world_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS NOREPLICATION;
GRANT CONNECT ON DATABASE eros TO eros_world_writer;
GRANT USAGE ON SCHEMA engine TO eros_world_writer;
GRANT SELECT(id,user_id,instance_id,channel,archived) ON engine.chat_sessions TO eros_world_writer;
GRANT SELECT(id,owner_uid,status,genome_id) ON engine.persona_instances TO eros_world_writer;
GRANT SELECT(owner_uid,stories_enabled,town_enabled), INSERT(owner_uid,stories_enabled,town_enabled), UPDATE(stories_enabled,town_enabled) ON engine.world_enrollments TO eros_world_writer;
GRANT SELECT(owner_uid,content), INSERT(owner_uid,content), UPDATE(content,updated_at) ON engine.world_worldviews TO eros_world_writer;
COMMIT;
