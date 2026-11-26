-- Remove integration, retain all existing world and chat data.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
DROP TRIGGER gpoint_world_on_first_user_message ON engine.chat_messages;
DROP FUNCTION engine.gpoint_enroll_world_after_user_message();
COMMIT;
