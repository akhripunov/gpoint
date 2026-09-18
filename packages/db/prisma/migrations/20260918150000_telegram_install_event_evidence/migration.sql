CREATE TABLE "CreatorChannelInstallAttempt" (
    "id" TEXT NOT NULL,
    "gpointUserId" TEXT NOT NULL,
    "telegramUserId" BIGINT NOT NULL,
    "state" TEXT NOT NULL DEFAULT 'WAITING_FOR_TELEGRAM',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "expiresAt" TIMESTAMP(3) NOT NULL,
    "completedAt" TIMESTAMP(3),
    CONSTRAINT "CreatorChannelInstallAttempt_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "CreatorChannelInstallAttempt_gpointUserId_fkey" FOREIGN KEY ("gpointUserId") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE
);
CREATE TABLE "TelegramChannelInstallEvidence" (
    "id" TEXT NOT NULL,
    "telegramUpdateId" INTEGER NOT NULL,
    "attemptId" TEXT,
    "telegramChatId" TEXT NOT NULL,
    "chatType" TEXT NOT NULL,
    "chatTitle" TEXT,
    "actorTelegramUserId" BIGINT NOT NULL,
    "oldBotStatus" TEXT,
    "newBotStatus" TEXT NOT NULL,
    "botRightsSnapshot" JSONB,
    "eventDate" TIMESTAMP(3),
    "receivedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "TelegramChannelInstallEvidence_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "TelegramChannelInstallEvidence_telegramUpdateId_key" UNIQUE ("telegramUpdateId"),
    CONSTRAINT "TelegramChannelInstallEvidence_attemptId_fkey" FOREIGN KEY ("attemptId") REFERENCES "CreatorChannelInstallAttempt"("id") ON DELETE SET NULL ON UPDATE CASCADE
);
CREATE INDEX "CreatorChannelInstallAttempt_telegramUserId_state_expiresAt_idx" ON "CreatorChannelInstallAttempt"("telegramUserId", "state", "expiresAt");
CREATE INDEX "CreatorChannelInstallAttempt_gpointUserId_state_expiresAt_idx" ON "CreatorChannelInstallAttempt"("gpointUserId", "state", "expiresAt");
CREATE INDEX "TelegramChannelInstallEvidence_actorTelegramUserId_receivedAt_idx" ON "TelegramChannelInstallEvidence"("actorTelegramUserId", "receivedAt");
CREATE INDEX "TelegramChannelInstallEvidence_telegramChatId_receivedAt_idx" ON "TelegramChannelInstallEvidence"("telegramChatId", "receivedAt");
