CREATE SCHEMA IF NOT EXISTS engine;

CREATE TABLE IF NOT EXISTS engine.eros_inference_jobs (
  id UUID PRIMARY KEY,
  request_id UUID NOT NULL UNIQUE,
  request_hash CHAR(64) NOT NULL,
  tenant_id TEXT NOT NULL,
  user_id UUID NOT NULL,
  creator_id UUID,
  session_id UUID NOT NULL,
  instance_id UUID NOT NULL,
  job_type TEXT NOT NULL CHECK (job_type IN ('CHAT_COMPLETION', 'SYSTEM_COMPLETION')),
  request JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING'
    CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED')),
  lease_token UUID,
  lease_expires_at TIMESTAMPTZ,
  deadline_at TIMESTAMPTZ NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 1),
  response_text TEXT,
  error_code VARCHAR(64),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK ((status = 'RUNNING') = (lease_token IS NOT NULL)),
  CONSTRAINT eros_inference_jobs_creator_required
    CHECK (job_type = 'SYSTEM_COMPLETION' OR creator_id IS NOT NULL)
);

ALTER TABLE engine.eros_inference_jobs ADD COLUMN IF NOT EXISTS creator_id UUID;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname = 'eros_inference_jobs_creator_required'
       AND conrelid = 'engine.eros_inference_jobs'::regclass
  ) THEN
    ALTER TABLE engine.eros_inference_jobs
      ADD CONSTRAINT eros_inference_jobs_creator_required
      CHECK (job_type = 'SYSTEM_COMPLETION' OR creator_id IS NOT NULL) NOT VALID;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS eros_inference_jobs_claim_idx
  ON engine.eros_inference_jobs (tenant_id, created_at, id) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS eros_inference_jobs_deadline_idx
  ON engine.eros_inference_jobs (tenant_id, deadline_at, status);
