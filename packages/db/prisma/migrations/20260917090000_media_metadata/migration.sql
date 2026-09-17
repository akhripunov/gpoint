ALTER TABLE "MediaAsset" ADD COLUMN "aspectRatio" DOUBLE PRECISION;
ALTER TABLE "MediaAsset" ADD COLUMN "blurHash" TEXT;
ALTER TABLE "MediaAsset" ADD COLUMN "safetyProvider" TEXT;
ALTER TABLE "MediaAsset" ADD COLUMN "safetyProviderVersion" TEXT;
ALTER TABLE "MediaAsset" ADD COLUMN "safetyConfidence" DOUBLE PRECISION;
ALTER TABLE "MediaAsset" ADD COLUMN "safetyEvaluatedAt" TIMESTAMP(3);
ALTER TABLE "MediaAsset" ADD COLUMN "safetyFailureReason" TEXT;
