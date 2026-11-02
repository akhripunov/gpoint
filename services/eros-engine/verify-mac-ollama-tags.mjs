const modelName = "qwen3-embedding:0.6b";
const expectedDigest = "ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d";
const scope = process.argv[2] === "--stdin" ? process.argv[3] ?? "vps-loopback" : process.argv[2] ?? "local";

let payload;
try {
  if (process.argv[2] === "--stdin") {
    payload = JSON.parse(await new Promise((resolve, reject) => {
      let body = "";
      process.stdin.setEncoding("utf8");
      process.stdin.on("data", chunk => { body += chunk; });
      process.stdin.on("end", () => resolve(body));
      process.stdin.on("error", reject);
    }));
  } else {
    const baseUrl = process.env.EROS_PILOT_OLLAMA_URL ?? "http://127.0.0.1:11434";
    const parsed = new URL(baseUrl);
    if (!new Set(["127.0.0.1", "localhost", "::1"]).has(parsed.hostname)) {
      throw new Error("Ollama endpoint must be loopback-only");
    }
    const response = await fetch(`${baseUrl}/api/tags`, {signal: AbortSignal.timeout(5000)});
    if (!response.ok) throw new Error("Ollama tags unavailable");
    payload = await response.json();
  }
} catch {
  console.error("EROS_PILOT_QWEN3_TAGS_UNAVAILABLE");
  process.exit(1);
}

const model = Array.isArray(payload?.models)
  ? payload.models.find(entry => entry?.name === modelName)
  : undefined;
if (model?.digest !== expectedDigest) {
  console.error("EROS_PILOT_QWEN3_TAGS_MISMATCH");
  process.exit(1);
}
console.log(`EROS_PILOT_QWEN3_TAGS=PASS scope=${scope}`);
