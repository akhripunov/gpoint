import assert from "node:assert/strict";
import { authorizeRelayRequest, isValidEmbeddingRequest, normalizePeerAddress } from "./eros-pilot-embedding-relay.mjs";

assert.equal(normalizePeerAddress("::ffff:172.31.254.2"), "172.31.254.2");
assert.equal(authorizeRelayRequest({ address: "172.31.254.2", method: "POST", path: "/api/embed" }), true);
assert.equal(authorizeRelayRequest({ address: "172.31.254.1", method: "GET", path: "/healthz" }), true);
assert.equal(authorizeRelayRequest({ address: "172.31.254.2", method: "GET", path: "/healthz" }), false);
assert.equal(authorizeRelayRequest({ address: "172.31.254.1", method: "POST", path: "/healthz" }), false);
assert.equal(authorizeRelayRequest({ address: "172.31.254.3", method: "GET", path: "/api/tags" }), true);
assert.equal(authorizeRelayRequest({ address: "172.31.254.3", method: "POST", path: "/api/embed" }), true);
assert.equal(authorizeRelayRequest({ address: "172.31.254.2", method: "GET", path: "/api/tags" }), false);
assert.equal(authorizeRelayRequest({ address: "172.31.254.3", method: "POST", path: "/api/tags" }), false);
assert.equal(authorizeRelayRequest({ address: "172.31.254.4", method: "POST", path: "/api/embed" }), false);
assert.equal(authorizeRelayRequest({ address: "127.0.0.1", method: "POST", path: "/api/embed" }), false);
assert.equal(authorizeRelayRequest({ address: "172.31.254.2", method: "POST", path: "/api/generate" }), false);
assert.equal(isValidEmbeddingRequest({ model: "qwen3-embedding:0.6b", dimensions: 512, input: "synthetic probe" }), true);
assert.equal(isValidEmbeddingRequest({ model: "other", dimensions: 512, input: "synthetic probe" }), false);
assert.equal(isValidEmbeddingRequest({ model: "qwen3-embedding:0.6b", dimensions: 384, input: "synthetic probe" }), false);
assert.equal(isValidEmbeddingRequest({ model: "qwen3-embedding:0.6b", dimensions: 512, input: new Array(33).fill("x") }), false);

console.log("EROS_PILOT_RELAY_POLICY_TESTS=PASS cases=16");
