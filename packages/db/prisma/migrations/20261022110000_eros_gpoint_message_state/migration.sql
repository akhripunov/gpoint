CREATE TABLE "ErosAssistantMessageState" (
  "userId" TEXT NOT NULL,
  "creatorId" TEXT NOT NULL,
  "sessionId" UUID NOT NULL,
  "messageId" VARCHAR(64) NOT NULL,
  "readAt" TIMESTAMP(3),
  CONSTRAINT "ErosAssistantMessageState_pkey" PRIMARY KEY ("userId", "creatorId", "sessionId", "messageId")
);

CREATE INDEX "ErosAssistantMessageState_userId_creatorId_sessionId_readAt_idx"
  ON "ErosAssistantMessageState"("userId", "creatorId", "sessionId", "readAt");

ALTER TABLE "ErosAssistantMessageState"
  ADD CONSTRAINT "ErosAssistantMessageState_userId_fkey"
  FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE "ErosAssistantMessageState"
  ADD CONSTRAINT "ErosAssistantMessageState_creatorId_fkey"
  FOREIGN KEY ("creatorId") REFERENCES "Creator"("id") ON DELETE CASCADE ON UPDATE CASCADE;
