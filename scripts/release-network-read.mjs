import fs from 'node:fs';
import path from 'node:path';
import {performance} from 'node:perf_hooks';

export const NETWORK_READ_BUDGET_SECONDS = 30 * 60;
const BACKOFF_CAP_MS = 60_000;
const observedSpend = new Map();
const stateQueues = new Map();
const TRANSIENT_CODES = new Set([
  'UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_HEADERS_TIMEOUT', 'UND_ERR_BODY_TIMEOUT',
  'UND_ERR_SOCKET', 'ETIMEDOUT', 'ECONNRESET', 'ECONNABORTED', 'EPIPE',
  'ERR_STREAM_PREMATURE_CLOSE', 'ERR_SOCKET_CONNECTION_TIMEOUT',
  'ERR_SSL_UNEXPECTED_EOF_WHILE_READING', 'ERR_TLS_HANDSHAKE_TIMEOUT',
]);
const FATAL_CERTIFICATE_CODES = new Set([
  'CERT_HAS_EXPIRED', 'DEPTH_ZERO_SELF_SIGNED_CERT', 'UNABLE_TO_VERIFY_LEAF_SIGNATURE',
  'ERR_TLS_CERT_ALTNAME_INVALID', 'CERT_SIGNATURE_FAILURE', 'ERR_TLS_CERT_SIGNATURE_ALGORITHM_UNSUPPORTED',
]);

export class NetworkReadBudgetError extends Error {
  constructor(stage) {
    super(`Network read retry budget exhausted (stage=${stage})`);
    this.name = 'NetworkReadBudgetError';
  }
}

function validateBudgetSeconds(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0)
    throw new TypeError('Network read budget seconds must be a finite positive number');
  return seconds;
}

function writeSpentSeconds(statePath, spent) {
  const temporary = `${statePath}.${process.pid}.${Math.random().toString(16).slice(2)}.tmp`;
  fs.mkdirSync(path.dirname(statePath), {recursive: true});
  fs.writeFileSync(temporary, `${spent.toFixed(6)}\n`, {mode: 0o600});
  fs.renameSync(temporary, statePath);
}

function readSpentSeconds(statePath) {
  if (!statePath) return 0;
  const key = path.resolve(statePath);
  let diskSpent = 0;
  if (fs.existsSync(statePath)) {
    let raw;
    try { raw = fs.readFileSync(statePath, 'utf8').trim(); }
    catch { throw new Error('Shared network retry budget state could not be read'); }
    if (!/^\d+(?:\.\d+)?$/.test(raw))
      throw new Error('Shared network retry budget state is invalid');
    diskSpent = Number(raw);
    if (!Number.isFinite(diskSpent) || diskSpent < 0)
      throw new Error('Shared network retry budget state is invalid');
  }
  const previous = observedSpend.get(key) ?? 0;
  const spent = Math.max(diskSpent, previous);
  observedSpend.set(key, spent);
  if (diskSpent < previous) writeSpentSeconds(statePath, spent);
  return spent;
}

function persistSpentSeconds(statePath, baseSpent, elapsedSeconds) {
  if (!statePath) return;
  const existing = readSpentSeconds(statePath);
  const spent = Math.ceil(Math.max(existing, baseSpent + Math.max(0, elapsedSeconds)) * 1_000_000) / 1_000_000;
  observedSpend.set(path.resolve(statePath), spent);
  writeSpentSeconds(statePath, spent);
}

function transientNetworkFailure(error) {
  const visited = new Set();
  let current = error;
  let transient = false;
  let fatal = false;
  for (let depth = 0; current && depth < 8 && !visited.has(current); depth++, current = current.cause) {
    visited.add(current);
    const name = typeof current.name === 'string' ? current.name : '';
    if (current instanceof SyntaxError || name === 'SyntaxError' || /(?:Gate|Integrity)Error$/.test(name)) fatal = true;
    for (const status of [current.status, current.statusCode, current.httpStatus])
      if (typeof status === 'number' && Number.isInteger(status) && status >= 400 && status <= 599) fatal = true;
    if (typeof current.code === 'string' && FATAL_CERTIFICATE_CODES.has(current.code)) fatal = true;
    if ((name === 'TimeoutError' && current.code === 23) ||
        (typeof current.code === 'string' && TRANSIENT_CODES.has(current.code))) transient = true;
  }
  return transient && !fatal;
}

async function retryNetworkReadOnce(stage, operation, {
  budgetSeconds = NETWORK_READ_BUDGET_SECONDS,
  statePath = process.env.NETWORK_RETRY_BUDGET_STATE || null,
  now = () => performance.now(),
  sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds)),
  jitter = (low, high) => low + Math.random() * (high - low),
  emit = message => console.error(message),
} = {}) {
  validateBudgetSeconds(budgetSeconds);
  if (statePath !== null && (typeof statePath !== 'string' || !statePath))
    throw new TypeError('Network read budget state path is invalid');
  if (typeof stage !== 'string' || !/^[a-z0-9_-]{1,64}$/.test(stage))
    throw new TypeError('Network read stage is invalid');
  if (typeof operation !== 'function') throw new TypeError('Network read operation is required');
  const initialNow = now();
  if (!Number.isFinite(initialNow)) throw new TypeError('Network read monotonic clock is invalid');
  const baseSpent = readSpentSeconds(statePath);
  const started = initialNow;
  const deadline = started + Math.max(0, budgetSeconds - baseSpent) * 1_000;
  let retries = 0;

  const elapsedMs = () => {
    const value = now() - started;
    if (!Number.isFinite(value) || value < 0) throw new TypeError('Network read monotonic clock is invalid');
    return value;
  };
  const remainingMs = () => deadline - now();
  const exhausted = () => new NetworkReadBudgetError(stage);
  const persist = () => persistSpentSeconds(statePath, baseSpent, elapsedMs() / 1_000);

  try {
    while (true) {
      const before = remainingMs();
      if (before <= 0) throw exhausted();
      try {
        const value = await operation(Math.max(1, Math.floor(before)));
        if (remainingMs() <= 0) {
          persist();
          emit(`NETWORK_UNAVAILABLE stage=${stage} elapsed_s=${(elapsedMs() / 1_000).toFixed(1)} budget_s=${budgetSeconds}`);
          throw exhausted();
        }
        if (retries) emit(`NETWORK_RESTORED stage=${stage} elapsed_s=${(elapsedMs() / 1_000).toFixed(1)}`);
        emit(`RELEASE_CONTINUING stage=${stage}`);
        return value;
      } catch (error) {
        if (error instanceof NetworkReadBudgetError || !transientNetworkFailure(error)) throw error;
        const elapsed = elapsedMs();
        const remaining = deadline - now();
        if (remaining <= 0) {
          persist();
          emit(`NETWORK_UNAVAILABLE stage=${stage} elapsed_s=${(elapsed / 1_000).toFixed(1)} budget_s=${budgetSeconds}`);
          throw exhausted();
        }
        const cap = Math.min(BACKOFF_CAP_MS, 1_000 * 2 ** Math.min(retries, 6));
        const proposed = jitter(0, cap);
        if (typeof proposed !== 'number' || !Number.isFinite(proposed))
          throw new TypeError('Network read backoff jitter is invalid');
        const delay = Math.min(Math.max(1, proposed), remaining);
        retries++;
        persist();
        emit(`NETWORK_RETRYING stage=${stage} retry_in_s=${(delay / 1_000).toFixed(1)} elapsed_s=${(elapsed / 1_000).toFixed(1)}`);
        if (remainingMs() <= 0) throw exhausted();
        await sleep(delay);
      }
    }
  } finally {
    if (retries) persist();
  }
}

export async function retryNetworkRead(stage, operation, options = {}) {
  const configuredPath = options.statePath ?? process.env.NETWORK_RETRY_BUDGET_STATE ?? null;
  if (!configuredPath) return retryNetworkReadOnce(stage, operation, {...options, statePath: null});
  if (typeof configuredPath !== 'string') throw new TypeError('Network read budget state path is invalid');
  const key = path.resolve(configuredPath);
  const previous = stateQueues.get(key) ?? Promise.resolve();
  let release;
  const current = new Promise(resolve => { release = resolve; });
  stateQueues.set(key, current);
  await previous;
  try {
    return await retryNetworkReadOnce(stage, operation, {...options, statePath: configuredPath});
  } finally {
    release();
    if (stateQueues.get(key) === current) stateQueues.delete(key);
  }
}
