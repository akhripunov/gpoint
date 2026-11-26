-- Downstream GPoint integration, not an upstream Eros SQLx migration.
-- Requires protected Eros DDL authorization and isolated restore rehearsal.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
CREATE FUNCTION engine.gpoint_enroll_world_after_user_message()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog
AS $function$
DECLARE v_owner uuid;
BEGIN
  SELECT p.owner_uid INTO v_owner
  FROM engine.chat_sessions s
  JOIN engine.persona_instances p ON p.id = s.instance_id
  WHERE s.id = NEW.session_id AND s.channel = 'text' AND NOT s.archived
    AND p.owner_uid = s.user_id AND p.status = 'active' AND p.genome_id IS NOT NULL;
  IF v_owner IS NULL THEN RETURN NEW; END IF;
  IF EXISTS (
    SELECT 1 FROM engine.world_enrollments e
    JOIN engine.world_worldviews w USING(owner_uid)
    WHERE e.owner_uid = v_owner AND e.town_enabled AND e.stories_enabled
      AND w.content ~ '[^[:space:]]'
  ) THEN RETURN NEW; END IF;
  INSERT INTO engine.world_worldviews(owner_uid, content)
  VALUES(v_owner, 'A coherent contemporary world grounded in each fictional persona''s established genome, backstory and conversational evidence. Preserve the persona''s canon, language, circumstances and continuity; do not force a city, occupation, era or relationship that contradicts established facts. Invent only fictional persona events, never the real user''s actions, words or private facts.')
  ON CONFLICT(owner_uid) DO UPDATE SET content=EXCLUDED.content, updated_at=now()
    WHERE engine.world_worldviews.content !~ '[^[:space:]]';
  INSERT INTO engine.world_enrollments(owner_uid, stories_enabled, town_enabled)
  VALUES(v_owner, true, true)
  ON CONFLICT(owner_uid) DO UPDATE SET stories_enabled=true, town_enabled=true
    WHERE NOT engine.world_enrollments.stories_enabled OR NOT engine.world_enrollments.town_enabled;
  RETURN NEW;
END;
$function$;
REVOKE ALL ON FUNCTION engine.gpoint_enroll_world_after_user_message() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION engine.gpoint_enroll_world_after_user_message() TO eros_runtime;
CREATE TRIGGER gpoint_world_on_first_user_message
AFTER INSERT ON engine.chat_messages FOR EACH ROW
WHEN (NEW.role='user' AND NEW.channel IS DISTINCT FROM 'product_qa')
EXECUTE FUNCTION engine.gpoint_enroll_world_after_user_message();
COMMIT;
