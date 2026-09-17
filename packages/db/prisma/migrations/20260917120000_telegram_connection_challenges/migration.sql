CREATE TABLE "TelegramConnectionChallenge" (
    "id" TEXT NOT NULL,
    "requestId" INTEGER NOT NULL,
    "userId" TEXT NOT NULL,
    "expiresAt" TIMESTAMP(3) NOT NULL,
    "usedAt" TIMESTAMP(3),
    "telegramChatId" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "TelegramConnectionChallenge_pkey" PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX "TelegramConnectionChallenge_requestId_key" ON "TelegramConnectionChallenge"("requestId");
CREATE INDEX "TelegramConnectionChallenge_userId_expiresAt_idx" ON "TelegramConnectionChallenge"("userId", "expiresAt");
ALTER TABLE "TelegramConnectionChallenge" ADD CONSTRAINT "TelegramConnectionChallenge_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE CASCADE ON UPDATE CASCADE;
