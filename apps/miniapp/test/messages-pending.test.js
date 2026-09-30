import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { mergePendingMessages } from "../src/messages-pending.js";

test("persisted history replaces its optimistic pending copy by client message id", () => {
  const pending = [{ id: "client-1", text: "hello", status: "failed", createdAt: 1_000 }];
  const history = [{ id: "server-1", clientMessageId: "client-1", role: "user", text: "hello", occurredAt: "1970-01-01T00:00:02.000Z" }];

  const merged = mergePendingMessages(history, pending);

  assert.equal(merged.length, 1);
  assert.equal(merged[0].id, "server-1");
  assert.equal(merged[0].status, "accepted");
});

test("messages with identical text but different client ids are never deduplicated", () => {
  const pending = [{ id: "client-2", text: "hello", status: "sending", createdAt: 1_000 }];
  const history = [{ id: "server-1", clientMessageId: "other-client", role: "user", text: "hello", occurredAt: "1970-01-01T00:00:02.000Z" }];

  const merged = mergePendingMessages(history, pending);

  assert.equal(merged.length, 2);
  assert.deepEqual(merged.map((item) => item.clientMessageId), ["other-client", "client-2"]);
});

test("accepted pending sends are polled from history, never automatically POSTed again", async () => {
  const source = await readFile(
    fileURLToPath(new URL("../src/messages-ui.tsx", import.meta.url)),
    "utf8",
  );
  assert.match(source, /refetchInterval:\s*3000/);
  assert.doesNotMatch(source, /setTimeout\s*\(\s*\(\)\s*=>\s*send\.mutate/);
});
