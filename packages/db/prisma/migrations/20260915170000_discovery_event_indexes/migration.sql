CREATE INDEX "ProductEvent_eventType_createdAt_idx" ON "ProductEvent"("eventType", "createdAt");
CREATE INDEX "ProductEvent_creatorId_eventType_createdAt_idx" ON "ProductEvent"("creatorId", "eventType", "createdAt");
CREATE INDEX "ProductEvent_userId_entityType_entityId_eventType_createdAt_idx" ON "ProductEvent"("userId", "entityType", "entityId", "eventType", "createdAt");
