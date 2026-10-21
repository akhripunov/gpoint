ALTER TABLE "CreatorProfile"
  ADD COLUMN "erosGenomeId" UUID;

CREATE UNIQUE INDEX "CreatorProfile_erosGenomeId_key"
  ON "CreatorProfile"("erosGenomeId");

CREATE TABLE "ErosRelationshipMapping" (
  "userId" TEXT NOT NULL,
  "creatorId" TEXT NOT NULL,
  "instanceId" UUID NOT NULL,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt" TIMESTAMP(3) NOT NULL,
  CONSTRAINT "ErosRelationshipMapping_pkey" PRIMARY KEY ("userId", "creatorId")
);

CREATE UNIQUE INDEX "ErosRelationshipMapping_instanceId_key"
  ON "ErosRelationshipMapping"("instanceId");
CREATE INDEX "ErosRelationshipMapping_creatorId_idx"
  ON "ErosRelationshipMapping"("creatorId");

ALTER TABLE "ErosRelationshipMapping"
  ADD CONSTRAINT "ErosRelationshipMapping_userId_fkey"
  FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
ALTER TABLE "ErosRelationshipMapping"
  ADD CONSTRAINT "ErosRelationshipMapping_creatorId_fkey"
  FOREIGN KEY ("creatorId") REFERENCES "Creator"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
