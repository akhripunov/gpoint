import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

const script = fileURLToPath(new URL("./verify-eros-pilot-model.mjs", import.meta.url));
const digest = "a".repeat(64);

function runVerifier(returnedDigest, vectorSize = 512, expectedDigest = digest) {
  const mockedFetch = `globalThis.fetch = async (url, options = {}) => {
    if (url === "http://ollama:11434/api/tags" && (!options.method || options.method === "GET")) {
      return new Response(JSON.stringify({ models: [{ name: "qwen3-embedding:0.6b", digest: ${JSON.stringify(returnedDigest)} }] }), { status: 200 });
    }
    if (url === "http://ollama:11434/api/embed" && options.method === "POST") {
      const request = JSON.parse(options.body);
      if (request.model !== "qwen3-embedding:0.6b" || request.dimensions !== 512 || request.truncate !== false || !request.input.startsWith("synthetic ")) throw new Error("unexpected synthetic probe");
      return new Response(JSON.stringify({ embeddings: [Array(${vectorSize}).fill(0.125)] }), { status: 200 });
    }
    throw new Error("unexpected verifier URL");
  };
  await import(${JSON.stringify(pathToFileURL(script).href)});`;
  return spawnSync(process.execPath, ["--input-type=module", "-e", mockedFetch], {
    encoding: "utf8",
    timeout: 5000,
    env: {
      ...process.env,
      EROS_PILOT_OLLAMA_MODEL_DIGEST: expectedDigest,
      EROS_PILOT_OLLAMA_URL: "http://ollama:11434",
    },
  });
}

const exact = runVerifier(digest);
assert.equal(exact.status, 0, exact.stderr);
assert.match(exact.stdout, /EROS_PILOT_MODEL_DIGEST=PASS/);
assert.match(exact.stdout, /EROS_PILOT_EMBEDDING_DIMENSIONS=512 PASS/);

const mismatch = runVerifier("b".repeat(64));
assert.equal(mismatch.status, 1);
assert.match(mismatch.stderr, /EROS_PILOT_MODEL_DIGEST_MISMATCH/);
assert.doesNotMatch(mismatch.stderr, /qwen3|digest [a-f0-9]{64}/i);

const wrongDimensions = runVerifier(digest, 1024);
assert.equal(wrongDimensions.status, 1);
assert.match(wrongDimensions.stderr, /EROS_PILOT_OLLAMA_EMBEDDING_DIMENSION_MISMATCH/);

const invalid = runVerifier(digest, 512, "invalid");
assert.equal(invalid.status, 2);
assert.match(invalid.stderr, /EROS_PILOT_MODEL_DIGEST_INVALID/);

console.log("EROS_PILOT_MODEL_VERIFIER_TESTS=PASS cases=4");
