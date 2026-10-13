import fs from 'node:fs';
import path from 'node:path';
import {performance} from 'node:perf_hooks';

const LEDGER_REF = 'refs/heads/gpoint-release-ledger';
const NETWORK_BUDGET_SECONDS = 30 * 60;
const COMMAND_TIMEOUT_MS = 30_000;
const MAX_BACKOFF_SECONDS = 60;
const TRANSIENT_CODES = new Set([
  'ETIMEDOUT', 'ECONNRESET', 'ECONNREFUSED', 'EHOSTUNREACH', 'ENETUNREACH', 'EAI_AGAIN',
]);

function retryStage(args) {
  if (!args.includes('origin') || !args.includes(LEDGER_REF)) return null;
  if (args[0] === 'ls-remote') return 'release_ledger_ls_remote';
  if (args[0] === 'fetch') return 'release_ledger_fetch';
  return null;
}

function isTransient(error) {
  if (TRANSIENT_CODES.has(error?.code)) return true;
  const stderr = Buffer.isBuffer(error?.stderr) ? error.stderr.toString('utf8') : String(error?.stderr ?? '');
  if (/authentication failed|could not read username|permission denied|repository not found|\bHTTP 40[13]\b/i.test(stderr)) return false;
  return /failed to connect|connection timed out|operation timed out|could not resolve (?:host|hostname|proxy)|connection reset|network is unreachable|no route to host|temporary failure in name resolution|tls connection.*terminated|recv failure/i.test(stderr);
}

function readSpent(statePath) {
  if (!statePath || !fs.existsSync(statePath)) return 0;
  const value = Number(fs.readFileSync(statePath, 'utf8').trim());
  if (!Number.isFinite(value) || value < 0) throw new Error('Invalid shared network retry budget state');
  return value;
}

function persistSpent(statePath, seconds) {
  if (!statePath) return;
  fs.mkdirSync(path.dirname(statePath), {recursive: true});
  const temporary = `${statePath}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, `${seconds.toFixed(6)}\n`);
  fs.renameSync(temporary, statePath);
}

export function createRetryingGit(runGit, {
  budgetSeconds = NETWORK_BUDGET_SECONDS,
  statePath = process.env.NETWORK_RETRY_BUDGET_STATE,
  clock = () => performance.now() / 1000,
  sleep = seconds => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, seconds * 1000),
  jitter = cap => Math.random() * cap,
  emit = message => console.log(message),
} = {}) {
  return (args, input) => {
    const stage = retryStage(args);
    if (!stage) return runGit(args, input, COMMAND_TIMEOUT_MS);

    const spentBefore = readSpent(statePath);
    const started = clock();
    let retries = 0;
    try {
      while (true) {
        const elapsed = Math.max(0, clock() - started);
        const remaining = budgetSeconds - spentBefore - elapsed;
        if (remaining <= 0) {
          emit(`NETWORK_UNAVAILABLE stage=${stage} elapsed_s=${(spentBefore + elapsed).toFixed(1)} budget_s=${budgetSeconds}`);
          throw new Error(`Git network retry budget exhausted stage=${stage}`);
        }
        try {
          const result = runGit(args, input, Math.min(COMMAND_TIMEOUT_MS, remaining * 1000));
          const total = spentBefore + Math.max(0, clock() - started);
          if (retries) emit(`NETWORK_RESTORED stage=${stage} attempts=${retries + 1} elapsed_s=${total.toFixed(1)}`);
          return result;
        } catch (error) {
          if (!isTransient(error)) throw error;
          const afterAttempt = Math.max(0, clock() - started);
          const remainingAfterAttempt = budgetSeconds - spentBefore - afterAttempt;
          if (remainingAfterAttempt <= 0) {
            emit(`NETWORK_UNAVAILABLE stage=${stage} elapsed_s=${(spentBefore + afterAttempt).toFixed(1)} budget_s=${budgetSeconds}`);
            throw new Error(`Git network retry budget exhausted stage=${stage}`);
          }
          const cap = Math.min(MAX_BACKOFF_SECONDS, 2 ** Math.min(retries, 6));
          const delay = Math.min(Math.max(0, jitter(cap)), remainingAfterAttempt);
          emit(`NETWORK_RETRYING stage=${stage} attempt=${retries + 1} error=${error.code ?? `git-exit-${error.status ?? 'unknown'}`} retry_in_s=${delay.toFixed(1)} elapsed_s=${(spentBefore + afterAttempt).toFixed(1)}`);
          retries += 1;
          sleep(delay);
        }
      }
    } finally {
      if (retries) persistSpent(statePath, spentBefore + Math.max(0, clock() - started));
    }
  };
}
