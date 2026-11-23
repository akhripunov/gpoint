CREATE TABLE "ErosGenomeProvisioningJob" (
  "creatorId" TEXT NOT NULL,
  "genomeId" UUID NOT NULL,
  "syncRequested" BOOLEAN NOT NULL DEFAULT false,
  "generation" INTEGER NOT NULL DEFAULT 1,
  "attempts" INTEGER NOT NULL DEFAULT 0,
  "nextAttemptAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "leaseToken" UUID,
  "leaseExpiresAt" TIMESTAMP(3),
  "lastErrorCode" TEXT,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt" TIMESTAMP(3) NOT NULL,
  CONSTRAINT "ErosGenomeProvisioningJob_pkey" PRIMARY KEY ("creatorId"),
  CONSTRAINT "ErosGenomeProvisioningJob_creatorId_fkey" FOREIGN KEY ("creatorId") REFERENCES "Creator"("id") ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT "ErosGenomeProvisioningJob_lease_check" CHECK (("leaseToken" IS NULL) = ("leaseExpiresAt" IS NULL)),
  CONSTRAINT "ErosGenomeProvisioningJob_attempts_check" CHECK ("attempts" >= 0),
  CONSTRAINT "ErosGenomeProvisioningJob_generation_check" CHECK ("generation" >= 1)
);
CREATE UNIQUE INDEX "ErosGenomeProvisioningJob_genomeId_key"
  ON "ErosGenomeProvisioningJob"("genomeId");
CREATE INDEX "ErosGenomeProvisioningJob_nextAttemptAt_leaseExpiresAt_idx"
  ON "ErosGenomeProvisioningJob"("nextAttemptAt", "leaseExpiresAt");
