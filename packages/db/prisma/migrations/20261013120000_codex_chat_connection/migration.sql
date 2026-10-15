CREATE TABLE "CodexChatConnection" (
    "id" TEXT NOT NULL,
    "userId" TEXT NOT NULL,
    "creatorId" TEXT NOT NULL,
    "accessTokenHash" TEXT NOT NULL,
    "refreshTokenHash" TEXT NOT NULL,
    "accessExpiresAt" TIMESTAMP(3) NOT NULL,
    "refreshExpiresAt" TIMESTAMP(3) NOT NULL,
    "revokedAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,
    CONSTRAINT "CodexChatConnection_pkey" PRIMARY KEY ("id")
);
CREATE TABLE "CodexDeviceAuthorization" (
    "id" TEXT NOT NULL,
    "deviceCodeHash" TEXT NOT NULL,
    "userCodeHash" TEXT NOT NULL,
    "sourceHash" TEXT NOT NULL,
    "expiresAt" TIMESTAMP(3) NOT NULL,
    "userId" TEXT,
    "creatorId" TEXT,
    "consumedAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "CodexDeviceAuthorization_pkey" PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX "CodexChatConnection_accessTokenHash_key" ON "CodexChatConnection"("accessTokenHash");
CREATE UNIQUE INDEX "CodexChatConnection_refreshTokenHash_key" ON "CodexChatConnection"("refreshTokenHash");
CREATE INDEX "CodexChatConnection_userId_creatorId_revokedAt_idx" ON "CodexChatConnection"("userId", "creatorId", "revokedAt");
CREATE INDEX "CodexChatConnection_refreshExpiresAt_idx" ON "CodexChatConnection"("refreshExpiresAt");
CREATE UNIQUE INDEX "CodexDeviceAuthorization_deviceCodeHash_key" ON "CodexDeviceAuthorization"("deviceCodeHash");
CREATE UNIQUE INDEX "CodexDeviceAuthorization_userCodeHash_key" ON "CodexDeviceAuthorization"("userCodeHash");
CREATE INDEX "CodexDeviceAuthorization_sourceHash_createdAt_idx" ON "CodexDeviceAuthorization"("sourceHash", "createdAt");
CREATE INDEX "CodexDeviceAuthorization_expiresAt_idx" ON "CodexDeviceAuthorization"("expiresAt");
ALTER TABLE "CodexChatConnection" ADD CONSTRAINT "CodexChatConnection_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "CodexChatConnection" ADD CONSTRAINT "CodexChatConnection_creatorId_fkey" FOREIGN KEY ("creatorId") REFERENCES "Creator"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "CodexDeviceAuthorization" ADD CONSTRAINT "CodexDeviceAuthorization_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "CodexDeviceAuthorization" ADD CONSTRAINT "CodexDeviceAuthorization_creatorId_fkey" FOREIGN KEY ("creatorId") REFERENCES "Creator"("id") ON DELETE CASCADE ON UPDATE CASCADE;
