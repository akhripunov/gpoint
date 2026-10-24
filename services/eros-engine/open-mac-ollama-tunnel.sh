#!/bin/sh
set -eu

if [ -z "${EROS_PILOT_VPS_SSH_TARGET:-}" ]; then
  echo "EROS_PILOT_VPS_SSH_TARGET_REQUIRED" >&2
  exit 2
fi

node --input-type=module <<'NODE'
const expected = "ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d";
try {
  const response = await fetch("http://127.0.0.1:11434/api/tags", { signal: AbortSignal.timeout(5000) });
  const payload = await response.json();
  const model = payload.models?.find((entry) => entry.name === "qwen3-embedding:0.6b");
  if (!response.ok || model?.digest !== expected) throw new Error("model mismatch");
  console.log("EROS_PILOT_LOCAL_QWEN_DIGEST=PASS");
} catch {
  console.error("EROS_PILOT_LOCAL_OLLAMA_MODEL_UNAVAILABLE_OR_MISMATCH");
  process.exit(1);
}
NODE

exec ssh -N -T \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  -o ClearAllForwardings=yes \
  -o ExitOnForwardFailure=yes \
  -o GatewayPorts=no \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -R 127.0.0.1:11435:127.0.0.1:11434 \
  "$EROS_PILOT_VPS_SSH_TARGET"
