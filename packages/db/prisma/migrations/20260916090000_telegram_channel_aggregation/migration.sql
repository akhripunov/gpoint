CREATE TABLE "TelegramChannel" (
    "id" TEXT NOT NULL,
    "creatorId" TEXT NOT NULL,
    "telegramChatId" TEXT NOT NULL,
    "username" TEXT,
    "title" TEXT,
    "status" TEXT NOT NULL DEFAULT 'PENDING',
    "importMode" TEXT NOT NULL DEFAULT 'AUTO',
    "importHashtag" TEXT,
    "verificationCodeHash" TEXT,
    "connectedAt" TIMESTAMP(3),
    "disconnectedAt" TIMESTAMP(3),
    "lastPostReceivedAt" TIMESTAMP(3),
    "lastSuccessfulIngestAt" TIMESTAMP(3),
    "lastError" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,
    CONSTRAINT "TelegramChannel_pkey" PRIMARY KEY ("id")
);
CREATE TABLE "PostTranslation" (
    "id" TEXT NOT NULL,
    "postId" TEXT NOT NULL,
    "language" TEXT NOT NULL,
    "text" TEXT NOT NULL,
    "provider" TEXT NOT NULL,
    "sourceHash" TEXT NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,
    CONSTRAINT "PostTranslation_pkey" PRIMARY KEY ("id")
);
ALTER TABLE "Post" ADD COLUMN "source" TEXT NOT NULL DEFAULT 'GPOINT';
ALTER TABLE "Post" ADD COLUMN "telegramChannelId" TEXT;
ALTER TABLE "Post" ADD COLUMN "telegramMessageId" INTEGER;
ALTER TABLE "Post" ADD COLUMN "telegramMediaGroupId" TEXT;
ALTER TABLE "Post" ADD COLUMN "telegramPublishedAt" TIMESTAMP(3);
ALTER TABLE "Post" ADD COLUMN "telegramEditedAt" TIMESTAMP(3);
ALTER TABLE "Post" ADD COLUMN "sourceText" TEXT;
ALTER TABLE "Post" ADD COLUMN "sourceLanguage" TEXT;
ALTER TABLE "Post" ADD COLUMN "sanitizedText" TEXT;
ALTER TABLE "Post" ADD COLUMN "safetyClassification" TEXT NOT NULL DEFAULT 'UNCERTAIN';
ALTER TABLE "Post" ADD COLUMN "safetyReason" TEXT;
ALTER TABLE "Post" ADD COLUMN "ingestStatus" TEXT;
CREATE UNIQUE INDEX "TelegramChannel_creatorId_key" ON "TelegramChannel"("creatorId");
CREATE UNIQUE INDEX "TelegramChannel_telegramChatId_key" ON "TelegramChannel"("telegramChatId");
CREATE INDEX "TelegramChannel_status_updatedAt_idx" ON "TelegramChannel"("status", "updatedAt");
CREATE UNIQUE INDEX "Post_telegramChannelId_telegramMessageId_key" ON "Post"("telegramChannelId", "telegramMessageId");
CREATE INDEX "Post_source_ingestStatus_idx" ON "Post"("source", "ingestStatus");
CREATE UNIQUE INDEX "PostTranslation_postId_language_sourceHash_key" ON "PostTranslation"("postId", "language", "sourceHash");
CREATE INDEX "PostTranslation_postId_language_idx" ON "PostTranslation"("postId", "language");
ALTER TABLE "TelegramChannel" ADD CONSTRAINT "TelegramChannel_creatorId_fkey" FOREIGN KEY ("creatorId") REFERENCES "Creator"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "PostTranslation" ADD CONSTRAINT "PostTranslation_postId_fkey" FOREIGN KEY ("postId") REFERENCES "Post"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "Post" ADD CONSTRAINT "Post_telegramChannelId_fkey" FOREIGN KEY ("telegramChannelId") REFERENCES "TelegramChannel"("id") ON DELETE SET NULL ON UPDATE CASCADE;
