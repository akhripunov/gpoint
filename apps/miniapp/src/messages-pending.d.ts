type PendingChatMessage = {
  id: string;
  text: string;
  status: "sending" | "waiting" | "failed";
  createdAt: number;
};

export function mergePendingMessages<
  T extends { id: string; clientMessageId?: string; role: "user" | "assistant" | "system" },
>(historyItems: T[], pendingItems: PendingChatMessage[]): Array<T & { status?: "accepted" | PendingChatMessage["status"] }>;
