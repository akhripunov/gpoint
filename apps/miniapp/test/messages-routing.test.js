import test from "node:test";
import assert from "node:assert/strict";
import { messagesPath, messagesRouteFromLocation } from "../src/messages-routing.js";

test("messages inbox and creator chat routes are stable", () => {
  assert.deepEqual(messagesRouteFromLocation("/messages"), { screen: "messages" });
  assert.deepEqual(messagesRouteFromLocation("/messages/models-hub"), {
    screen: "chat",
    slug: "models-hub",
  });
  assert.equal(messagesPath("model name"), "/messages/model%20name");
});

test("non-messages routes are not claimed", () => {
  assert.equal(messagesRouteFromLocation("/model/alina"), null);
  assert.equal(messagesRouteFromLocation("/messages/a/b"), null);
});
