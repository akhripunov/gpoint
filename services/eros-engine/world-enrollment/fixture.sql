\set ON_ERROR_STOP on
-- Disposable hosted CI database only; never run against the real Eros DB.
DO $$ BEGIN
  IF current_database() <> 'gpoint_world_enrollment_test'
      OR EXISTS (SELECT FROM pg_namespace WHERE nspname='engine')
      OR EXISTS (SELECT FROM pg_roles WHERE rolname='eros_runtime')
  THEN RAISE EXCEPTION 'expected fresh isolated test database and role'; END IF;
END $$;
CREATE ROLE eros_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE SCHEMA engine;
CREATE TABLE engine.persona_instances(id uuid PRIMARY KEY, genome_id uuid, owner_uid uuid NOT NULL, status text NOT NULL);
CREATE TABLE engine.chat_sessions(id uuid PRIMARY KEY, user_id uuid NOT NULL, instance_id uuid, channel text NOT NULL, archived boolean NOT NULL DEFAULT false);
CREATE TABLE engine.chat_messages(id uuid DEFAULT gen_random_uuid() PRIMARY KEY, session_id uuid NOT NULL REFERENCES engine.chat_sessions(id), role text NOT NULL, channel text);
CREATE TABLE engine.world_enrollments(owner_uid uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(), town_enabled boolean NOT NULL DEFAULT false, stories_enabled boolean NOT NULL DEFAULT false);
CREATE TABLE engine.world_worldviews(owner_uid uuid PRIMARY KEY, content text NOT NULL CHECK(char_length(content) BETWEEN 1 AND 10000), updated_at timestamptz NOT NULL DEFAULT now());
GRANT USAGE ON SCHEMA engine TO eros_runtime;
GRANT SELECT ON engine.persona_instances,engine.chat_sessions TO eros_runtime;
GRANT INSERT ON engine.chat_messages TO eros_runtime;
GRANT SELECT,INSERT,UPDATE ON engine.world_enrollments,engine.world_worldviews TO eros_runtime;
\ir install.sql
INSERT INTO engine.persona_instances
SELECT md5('persona-'||n)::uuid, CASE WHEN n=4 THEN NULL ELSE md5('shared-genome')::uuid END,
 md5('owner-'||n)::uuid, CASE WHEN n=3 THEN 'inactive' ELSE 'active' END
FROM generate_series(1,10) AS n;
INSERT INTO engine.chat_sessions
SELECT md5('session-'||n)::uuid,md5('owner-'||CASE WHEN n=6 THEN 7 ELSE n END)::uuid,
 md5('persona-'||n)::uuid,'text',n=5 FROM generate_series(1,10) AS n;
INSERT INTO engine.world_worldviews VALUES(md5('owner-2')::uuid,'Existing custom world','2020-01-01T00:00:00Z');
INSERT INTO engine.world_enrollments(owner_uid) VALUES(md5('owner-2')::uuid);
DO $$ BEGIN
  IF (SELECT count(*) FROM engine.world_enrollments) <> 1 THEN RAISE EXCEPTION 'world created without a message'; END IF;
END $$;
SET ROLE eros_runtime;
INSERT INTO engine.chat_messages(session_id,role,channel)
SELECT md5('session-'||n)::uuid, CASE WHEN n=8 THEN 'assistant' ELSE 'user' END,
 CASE WHEN n=7 THEN 'product_qa' ELSE NULL END FROM generate_series(1,8) AS n;
RESET ROLE;
UPDATE engine.world_worldviews SET content='Preserve this world',updated_at='2021-01-01T00:00:00Z' WHERE owner_uid=md5('owner-1')::uuid;
SET ROLE eros_runtime;
INSERT INTO engine.chat_messages(session_id,role) VALUES(md5('session-1')::uuid,'user');
RESET ROLE;
DO $$ BEGIN
  IF (SELECT count(*) FROM engine.world_enrollments) <> 2
     OR (SELECT count(*) FROM engine.world_worldviews) <> 2
     OR EXISTS(SELECT FROM engine.world_enrollments WHERE NOT town_enabled OR NOT stories_enabled)
     OR NOT EXISTS(SELECT FROM engine.world_worldviews WHERE owner_uid=md5('owner-1')::uuid AND content='Preserve this world' AND updated_at='2021-01-01T00:00:00Z')
     OR NOT EXISTS(SELECT FROM engine.world_worldviews WHERE owner_uid=md5('owner-2')::uuid AND content='Existing custom world' AND updated_at='2020-01-01T00:00:00Z')
  THEN RAISE EXCEPTION 'world isolation or preservation failed'; END IF;
END $$;
BEGIN;
SET LOCAL ROLE eros_runtime;
INSERT INTO engine.chat_messages(session_id,role) VALUES(md5('session-9')::uuid,'user');
DO $$ BEGIN
  IF NOT EXISTS(SELECT FROM engine.world_enrollments WHERE owner_uid=md5('owner-9')::uuid)
  THEN RAISE EXCEPTION 'enrollment not atomic'; END IF;
END $$;
ROLLBACK;
DO $$ BEGIN
 IF EXISTS(SELECT FROM engine.world_enrollments WHERE owner_uid=md5('owner-9')::uuid) OR EXISTS(SELECT FROM engine.world_worldviews WHERE owner_uid=md5('owner-9')::uuid)
 THEN RAISE EXCEPTION 'enrollment survived rollback'; END IF;
END $$;
\ir remove.sql
SET ROLE eros_runtime;
INSERT INTO engine.chat_messages(session_id,role) VALUES(md5('session-10')::uuid,'user');
RESET ROLE;
DO $$ BEGIN
 IF (SELECT count(*) FROM engine.world_enrollments) <> 2 OR (SELECT count(*) FROM engine.world_worldviews) <> 2
 THEN RAISE EXCEPTION 'rollback removed worlds or left enrollment active'; END IF;
END $$;
SELECT 'GPOINT_WORLD_ENROLLMENT_FIXTURE=PASS' AS result;
