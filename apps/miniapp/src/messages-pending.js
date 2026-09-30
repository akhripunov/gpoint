/** Merge local send state into authoritative history by idempotency identity. */
export function mergePendingMessages(historyItems, pendingItems) {
  const pendingByClientId = new Map(pendingItems.map((item) => [item.id, item]));
  const persistedIds = new Set();
  const history = historyItems.map((item) => {
    if (item.role !== "user" || !item.clientMessageId) return item;
    const pending = pendingByClientId.get(item.clientMessageId);
    if (!pending) return item;
    persistedIds.add(item.clientMessageId);
    return { ...item, status: "accepted" };
  });
  const unsaved = pendingItems
    .filter((item) => !persistedIds.has(item.id))
    .map((item) => ({
      id: item.id,
      clientMessageId: item.id,
      role: "user",
      text: item.text,
      occurredAt: new Date(item.createdAt).toISOString(),
      status: item.status,
    }));
  return [...history, ...unsaved];
}
