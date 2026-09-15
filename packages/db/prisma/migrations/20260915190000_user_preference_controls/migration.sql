ALTER TABLE "User" ADD COLUMN "personalizationResetAt" TIMESTAMP(3);
CREATE TABLE "UserPreference" (
  "id" TEXT NOT NULL,
  "userId" TEXT NOT NULL,
  "preferenceType" TEXT NOT NULL,
  "entityType" TEXT NOT NULL,
  "entityId" TEXT NOT NULL,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt" TIMESTAMP(3) NOT NULL,
  CONSTRAINT "UserPreference_pkey" PRIMARY KEY ("id"),
  CONSTRAINT "UserPreference_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE UNIQUE INDEX "UserPreference_userId_preferenceType_entityType_entityId_key" ON "UserPreference"("userId", "preferenceType", "entityType", "entityId");
CREATE INDEX "UserPreference_userId_preferenceType_idx" ON "UserPreference"("userId", "preferenceType");
