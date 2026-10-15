ALTER TABLE "CodexDeviceAuthorization" RENAME COLUMN "userCodeHash" TO "telegramChallengeHash";
ALTER INDEX "CodexDeviceAuthorization_userCodeHash_key" RENAME TO "CodexDeviceAuthorization_telegramChallengeHash_key";
