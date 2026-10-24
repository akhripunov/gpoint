import http from "node:http";
import { pathToFileURL } from "node:url";

const EXPECTED_BIND = "172.31.254.1:11434";
const UPSTREAM_HOST = "127.0.0.1";
const UPSTREAM_PORT = 11435;
const MODEL_NAME = "qwen3-embedding:0.6b";
const MAX_BODY_BYTES = 1_048_576;
const MAX_RESPONSE_BYTES = 1_048_576;
const API_CLIENT = "172.31.254.2";
const VERIFY_CLIENT = "172.31.254.3";
const EXPECTED_DIGEST = process.env.EROS_PILOT_OLLAMA_MODEL_DIGEST ??
  "ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d";

export function normalizePeerAddress(address = "") {
  return address.startsWith("::ffff:") ? address.slice(7) : address;
}

export function authorizeRelayRequest({ address, method, path }) {
  const peer = normalizePeerAddress(address);
  if (path === "/api/tags") return peer === VERIFY_CLIENT && method === "GET";
  if (path === "/api/embed") {
    return (peer === API_CLIENT || peer === VERIFY_CLIENT) && method === "POST";
  }
  return false;
}

export function isValidEmbeddingRequest(payload) {
  return payload?.model === MODEL_NAME && payload?.dimensions === 512 &&
    (typeof payload?.input === "string" ||
      (Array.isArray(payload?.input) && payload.input.length > 0 &&
       payload.input.length <= 32 && payload.input.every((item) => typeof item === "string")));
}

function sendJson(response, status, payload) {
  response.writeHead(status, { "content-type": "application/json", "cache-control": "no-store" });
  response.end(JSON.stringify(payload));
}

async function readLimitedBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new Error("body too large");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

async function proxyRequest(request, response) {
  const peer = normalizePeerAddress(request.socket.remoteAddress);
  if (!authorizeRelayRequest({ address: peer, method: request.method, path: request.url })) {
    sendJson(response, 403, { error: "forbidden" });
    return;
  }

  let body;
  if (request.method === "POST") {
    try {
      body = await readLimitedBody(request);
      const payload = JSON.parse(body.toString("utf8"));
      if (!isValidEmbeddingRequest(payload)) {
        sendJson(response, 400, { error: "invalid pilot embedding request" });
        return;
      }
    } catch {
      sendJson(response, 400, { error: "invalid pilot embedding request" });
      return;
    }
  }

  const upstream = http.request({
    host: UPSTREAM_HOST,
    port: UPSTREAM_PORT,
    path: request.url,
    method: request.method,
    headers: request.method === "POST" ? { "content-type": "application/json", "content-length": body.length } : {},
    timeout: 30_000,
  }, (upstreamResponse) => {
    const chunks = [];
    let responseBytes = 0;
    upstreamResponse.on("data", (chunk) => {
      responseBytes += chunk.length;
      if (responseBytes > MAX_RESPONSE_BYTES) {
        upstreamResponse.destroy();
        if (!response.headersSent) sendJson(response, 502, { error: "embedding response exceeds pilot limit" });
        return;
      }
      chunks.push(chunk);
    });
    upstreamResponse.on("end", () => {
      const result = Buffer.concat(chunks);
      if (request.url === "/api/tags" && upstreamResponse.statusCode === 200) {
        try {
          const payload = JSON.parse(result.toString("utf8"));
          const model = Array.isArray(payload?.models)
            ? payload.models.find((entry) => entry?.name === MODEL_NAME && entry?.digest === EXPECTED_DIGEST)
            : undefined;
          if (!model) {
            sendJson(response, 503, { error: "pilot model digest unavailable" });
            return;
          }
          sendJson(response, 200, { models: [model] });
          return;
        } catch {
          sendJson(response, 502, { error: "invalid model metadata" });
          return;
        }
      }
      response.writeHead(upstreamResponse.statusCode ?? 502, {
        "content-type": upstreamResponse.headers["content-type"] ?? "application/json",
        "cache-control": "no-store",
      });
      response.end(result);
    });
  });
  upstream.on("timeout", () => upstream.destroy(new Error("upstream timeout")));
  upstream.on("error", () => {
    if (!response.headersSent) sendJson(response, 502, { error: "embedding endpoint unavailable" });
    else response.destroy();
  });
  upstream.end(body);
}

export function createRelayServer() {
  return http.createServer((request, response) => {
    void proxyRequest(request, response);
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (process.env.EROS_PILOT_RELAY_BIND !== EXPECTED_BIND || !/^[a-f0-9]{64}$/.test(EXPECTED_DIGEST)) {
    console.error("EROS_PILOT_RELAY_CONFIGURATION_INVALID");
    process.exit(2);
  }
  const server = createRelayServer();
  server.requestTimeout = 30_000;
  server.listen(11434, "172.31.254.1", () => console.log("EROS_PILOT_EMBEDDING_RELAY=READY"));
  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => server.close(() => process.exit(0)));
  }
}
