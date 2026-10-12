import test from "node:test";
import assert from "node:assert/strict";
import { filterMessageThreads } from "../src/messages-search.js";

const threads = [
  { creator: { id: "1", displayName: "Models Hub", slug: "models-hub" }, lastMessagePreview: "See you tomorrow", lastMessageRole: "assistant", lastMessageAt: "2026-10-10T09:00:00Z", unreadCount: 0 },
  { creator: { id: "2", displayName: "Anya Belle", slug: "anya-belle" }, lastMessagePreview: "A long weekend plan", lastMessageRole: "user", lastMessageAt: "2026-10-09T09:00:00Z", unreadCount: 1 },
];

test("message search matches creator identity and last message without changing thread order", () => {
  assert.deepEqual(filterMessageThreads(threads, "ANYA"), [threads[1]]);
  assert.deepEqual(filterMessageThreads(threads, "tomorrow"), [threads[0]]);
});

test("message search returns no threads when the loaded conversations have no match", () => {
  assert.deepEqual(filterMessageThreads(threads, "not here"), []);
});
