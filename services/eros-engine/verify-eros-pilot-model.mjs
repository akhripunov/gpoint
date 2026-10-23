const modelName = "qwen3-embedding:0.6b";
const expectedDigest = process.env.EROS_PILOT_OLLAMA_MODEL_DIGEST ?? "";
const ollamaBaseUrl = process.env.EROS_PILOT_OLLAMA_URL ?? "http://ollama:11434";
const requestTimeoutMs = 30_000;

if (!/^[a-f0-9]{64}$/.test(expectedDigest)) {
  console.error("EROS_PILOT_MODEL_DIGEST_INVALID");
  process.exit(2);
}

let response;
try {
  response = await fetch(`${ollamaBaseUrl}/api/tags`, {
    signal: AbortSignal.timeout(10_000),
  });
} catch {
  console.error("EROS_PILOT_OLLAMA_TAGS_UNAVAILABLE");
  process.exit(1);
}

if (!response.ok) {
  console.error("EROS_PILOT_OLLAMA_TAGS_UNAVAILABLE");
  process.exit(1);
}

let payload;
try {
  payload = await response.json();
} catch {
  console.error("EROS_PILOT_OLLAMA_TAGS_INVALID");
  process.exit(1);
}

const model = Array.isArray(payload?.models)
  ? payload.models.find((entry) => entry?.name === modelName)
  : undefined;
if (model?.digest !== expectedDigest) {
  console.error("EROS_PILOT_MODEL_DIGEST_MISMATCH");
  process.exit(1);
}

let embeddingResponse;
try {
  embeddingResponse = await fetch(`${ollamaBaseUrl}/api/embed`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      model: modelName,
      input: "synthetic Eros VPS pilot embedding readiness check",
      dimensions: 512,
      truncate: false,
    }),
    signal: AbortSignal.timeout(requestTimeoutMs),
  });
} catch {
  console.error("EROS_PILOT_OLLAMA_EMBEDDING_UNAVAILABLE");
  process.exit(1);
}

if (!embeddingResponse.ok) {
  console.error("EROS_PILOT_OLLAMA_EMBEDDING_UNAVAILABLE");
  process.exit(1);
}

let embeddingPayload;
try {
  embeddingPayload = await embeddingResponse.json();
} catch {
  console.error("EROS_PILOT_OLLAMA_EMBEDDING_INVALID");
  process.exit(1);
}

const vector = embeddingPayload?.embeddings?.[0];
if (!Array.isArray(vector) || vector.length !== 512 ||
    !vector.every((value) => typeof value === "number" && Number.isFinite(value))) {
  console.error("EROS_PILOT_OLLAMA_EMBEDDING_DIMENSION_MISMATCH");
  process.exit(1);
}

console.log("EROS_PILOT_MODEL_DIGEST=PASS");
console.log("EROS_PILOT_EMBEDDING_DIMENSIONS=512 PASS");
