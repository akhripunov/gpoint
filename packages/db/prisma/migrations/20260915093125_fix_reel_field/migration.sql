/*
  Warnings:

  - You are about to drop the column `isReel` on the `CreatorBot` table. All the data in the column will be lost.
  - You are about to drop the column `profileViews` on the `User` table. All the data in the column will be lost.

*/
-- AlterTable
ALTER TABLE "CreatorBot" DROP COLUMN "isReel";

-- AlterTable
ALTER TABLE "Post" ADD COLUMN     "isReel" BOOLEAN NOT NULL DEFAULT false;

-- AlterTable
ALTER TABLE "User" DROP COLUMN "profileViews";
