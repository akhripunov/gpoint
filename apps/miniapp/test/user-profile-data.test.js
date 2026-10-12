import test from "node:test";
import assert from "node:assert/strict";
import { userProfileCounts } from "../src/user-profile-data.js";

test("profile counts stay unavailable when the current-user contract has no count fields", () => {
  assert.deepEqual(userProfileCounts({ id: "u1", firstName: "Alex", telegramUsername: "alex", isPremium: false }), {
    saved: null,
    following: null,
  });
});

test("profile counts accept only non-negative integer data supplied by an API", () => {
  assert.deepEqual(userProfileCounts({ savedCount: 0, followingCount: 4 }), {
    saved: 0,
    following: 4,
  });
  assert.deepEqual(userProfileCounts({ savedCount: -1, followingCount: 2.5 }), {
    saved: null,
    following: null,
  });
});
