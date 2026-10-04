-- G Sales owns these six business tables. AGATA/public domain objects are untouched.
CREATE SCHEMA gsales;
REVOKE ALL ON SCHEMA gsales FROM PUBLIC;

CREATE TABLE gsales.telegram_accounts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 tenant_id uuid NOT NULL CHECK (tenant_id='c4d67889-877e-4d1b-a8d8-597c479d638f'),
 display_name text NOT NULL, telegram_user_id bigint NOT NULL CHECK (telegram_user_id>0),
 telegram_username text, session_secret_ref uuid NOT NULL,
 connection_status text NOT NULL DEFAULT 'disconnected' CHECK (connection_status IN ('disconnected','authorized','error')),
 is_enabled boolean NOT NULL DEFAULT false, last_connected_at timestamptz,
 last_error_code text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,telegram_user_id), UNIQUE(tenant_id,session_secret_ref)
);
CREATE TABLE gsales.sellers (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL,
 agata_character_id uuid NOT NULL, telegram_account_id uuid,
 display_name text NOT NULL, language text NOT NULL DEFAULT 'ru',
 status text NOT NULL DEFAULT 'paused' CHECK (status IN ('active','paused','disabled')),
 auto_reply_enabled boolean NOT NULL DEFAULT false, global_paused boolean NOT NULL DEFAULT true,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,agata_character_id),
 FOREIGN KEY(tenant_id,telegram_account_id) REFERENCES gsales.telegram_accounts(tenant_id,id)
);
CREATE TABLE gsales.leads (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL,
 assigned_seller_id uuid NOT NULL, telegram_user_id bigint NOT NULL CHECK(telegram_user_id>0),
 telegram_username text NOT NULL, display_name text NOT NULL, agata_user_id uuid,
 current_stage text NOT NULL DEFAULT 'discovered' CHECK(current_stage IN ('discovered','reviewed','approved','contacted','replied','interested','registered','declined','opted_out')),
 contact_eligibility text NOT NULL DEFAULT 'unverified' CHECK(contact_eligibility IN ('unverified','allowed','blocked')),
 contact_eligibility_checked_at timestamptz, eligibility_evidence text,
 gpoint_creator_external_id text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,telegram_user_id),
 FOREIGN KEY(tenant_id,assigned_seller_id) REFERENCES gsales.sellers(tenant_id,id)
);
CREATE TABLE gsales.lead_sources (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, lead_id uuid NOT NULL,
 source_type text NOT NULL CHECK(source_type IN ('manual','public_profile','opt_in')),
 source_reference text NOT NULL, discovered_at timestamptz NOT NULL DEFAULT now(), metadata jsonb NOT NULL DEFAULT '{}',
 FOREIGN KEY(tenant_id,lead_id) REFERENCES gsales.leads(tenant_id,id),
 UNIQUE(tenant_id,lead_id,source_type,source_reference)
);
CREATE TABLE gsales.outreach_attempts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, lead_id uuid NOT NULL,
 seller_id uuid NOT NULL, telegram_account_id uuid NOT NULL,
 attempt_type text NOT NULL CHECK(attempt_type IN ('first','reply')),
 approval_status text NOT NULL DEFAULT 'pending' CHECK(approval_status IN ('pending','approved','rejected')),
 approved_by bigint, approved_at timestamptz, approved_content_hash text,
 approved_recipient_ref text, approval_binding_hash text,
 message_reference text NOT NULL, content_hash text NOT NULL,
 provider_message_id bigint, telegram_random_id bigint NOT NULL,
 idempotency_key text NOT NULL, delivery_status text NOT NULL DEFAULT 'draft'
 CHECK(delivery_status IN ('draft','sending','sent','unknown','blocked','failed')),
 inbound_message_id bigint, attempted_at timestamptz, sent_at timestamptz,
 history_confirmed_at timestamptz, retry_after timestamptz, error_code text, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,id), UNIQUE(tenant_id,idempotency_key),
 FOREIGN KEY(tenant_id,lead_id) REFERENCES gsales.leads(tenant_id,id),
 FOREIGN KEY(tenant_id,seller_id) REFERENCES gsales.sellers(tenant_id,id),
 FOREIGN KEY(tenant_id,telegram_account_id) REFERENCES gsales.telegram_accounts(tenant_id,id),
 CHECK(attempt_type <> 'reply' OR inbound_message_id IS NOT NULL)
);
CREATE UNIQUE INDEX outreach_first_once ON gsales.outreach_attempts(tenant_id,lead_id)
 WHERE attempt_type='first' AND delivery_status IN ('sending','sent','unknown');
CREATE UNIQUE INDEX outreach_reply_once ON gsales.outreach_attempts(tenant_id,lead_id,inbound_message_id)
 WHERE attempt_type='reply';
CREATE TABLE gsales.dialog_controls (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, lead_id uuid NOT NULL,
 seller_id uuid NOT NULL, telegram_account_id uuid NOT NULL, agata_relationship_id uuid,
 ai_mode text NOT NULL DEFAULT 'disabled' CHECK(ai_mode IN ('disabled','manual','awaiting_approval','auto_reply','paused','opted_out')),
 paused_reason text, human_takeover boolean NOT NULL DEFAULT false,
 pending_inbound_message_id bigint, last_inbound_message_id bigint, last_outbound_message_id bigint, last_auto_reply_at timestamptz,
 auto_reply_count integer NOT NULL DEFAULT 0 CHECK(auto_reply_count>=0),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,lead_id),
 FOREIGN KEY(tenant_id,lead_id) REFERENCES gsales.leads(tenant_id,id),
 FOREIGN KEY(tenant_id,seller_id) REFERENCES gsales.sellers(tenant_id,id),
 FOREIGN KEY(tenant_id,telegram_account_id) REFERENCES gsales.telegram_accounts(tenant_id,id)
);
CREATE INDEX leads_stage ON gsales.leads(tenant_id,current_stage,created_at);
CREATE INDEX sources_lead ON gsales.lead_sources(tenant_id,lead_id);
CREATE INDEX attempts_pending ON gsales.outreach_attempts(tenant_id,approval_status,delivery_status);
CREATE INDEX controls_seller ON gsales.dialog_controls(tenant_id,seller_id,ai_mode);
CREATE FUNCTION gsales.touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN NEW.updated_at=now(); RETURN NEW; END $$;
DO $$ DECLARE t text; BEGIN
 FOR t IN SELECT unnest(ARRAY['sellers','telegram_accounts','leads','lead_sources','outreach_attempts','dialog_controls']) LOOP
  EXECUTE format('ALTER TABLE gsales.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE gsales.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY gsales_login_tenant ON gsales.%I FOR ALL USING (tenant_id=''c4d67889-877e-4d1b-a8d8-597c479d638f''::uuid AND session_user=''gsales_runtime'') WITH CHECK (tenant_id=''c4d67889-877e-4d1b-a8d8-597c479d638f''::uuid AND session_user=''gsales_runtime'')',t);
  IF t<>'lead_sources' AND t<>'outreach_attempts' THEN
   EXECUTE format('CREATE TRIGGER updated_at BEFORE UPDATE ON gsales.%I FOR EACH ROW EXECUTE FUNCTION gsales.touch_updated_at()',t);
  END IF;
 END LOOP;
END $$;
GRANT USAGE ON SCHEMA gsales TO gsales_runtime;
GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA gsales TO gsales_runtime;
-- No AGATA/public grants, no DELETE: opt-out and audit records survive lifecycle transitions.
