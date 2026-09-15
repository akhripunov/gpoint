-- AlterTable
ALTER TABLE "CreatorBot" ADD COLUMN     "isReel" BOOLEAN NOT NULL DEFAULT false;

-- AlterTable
ALTER TABLE "User" ADD COLUMN     "profileViews" INTEGER NOT NULL DEFAULT 0;
