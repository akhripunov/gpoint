#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { execFileSync, spawnSync } from 'node:child_process';
import { verifyReleaseBundle } from './release-bundle.mjs';
import { persistReleaseBookkeeping, loadReleaseBookkeeping } from './release-bookkeeping.mjs';
import { fileURLToPath } from 'node:url';
import { inspectProductionReadOnly } from './release-gpoint-vps-adapter.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const sha = process.env.CANDIDATE_SHA;
const mode = process.env.RELEASE_MODE;
if (!['preflight', 'production'].includes(mode)) throw new Error('Explicit release mode is required');
if (mode === 'production' && process.env.APPROVE_PRODUCTION !== 'true') throw new Error('Explicit production approval is required');
const proof = verifyReleaseBundle(process.env.GPOINT_RELEASE_BUNDLE, sha);
if (String(proof.runId) !== process.env.PREPARE_RUN_ID) throw new Error('Receipt does not belong to the approved GitHub run');
const git = (args, input) => execFileSync('git', args, { cwd: root, encoding: 'utf8', input, timeout: 30_000 }).trim();
if (git(['rev-parse', 'HEAD']) !== sha || git(['status', '--porcelain'])) throw new Error('Release checkout must be clean and exact');
const workerSource = path.join(root, 'services/agata/worker/gpoint_mac_worker.py');
const workerInstalled = path.join(os.homedir(), 'Library/Application Support/GPoint/agata-codex-worker/gpoint_mac_worker.py');
const checksum = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
if (!fs.existsSync(workerInstalled) || checksum(workerSource) !== checksum(workerInstalled)) {
  throw new Error('Installed Mac worker differs from candidate. Install the published candidate worker before apply.');
}
if (!process.env.RELEASE_RESULT || !process.env.GITHUB_RUN_ID) throw new Error('Release evidence paths are required');
const ledger = loadReleaseBookkeeping({ root, git, destination: process.env.GPOINT_RELEASE_EVIDENCE });
process.env.GPOINT_RELEASE_STATE_FILE = ledger.stateFile;
process.env.GPOINT_RELEASE_MANIFEST_DIR = ledger.manifestDir;
const previous = new Set(fs.readdirSync(ledger.manifestDir));
let releaseCode = 1;
let preflightStatus = 'BLOCKED';
try {
  // First GSales release includes an explicitly approved protected setup.
  // Read-only preflight never provisions identities or changes the Mac worker.
  if (mode === 'production') {
    const facts = inspectProductionReadOnly();
    if (/^LIVE_GSALES_HEAD=ABSENT$/m.test(facts)) {
      const setup = spawnSync('pnpm', ['release:gpoint', '--provision-gsales-config', '--candidate', sha, '--owner-approved-sha', sha],
        { cwd: root, stdio: 'inherit', timeout: 300_000 });
      if (setup.status !== 0) throw new Error('Approved GSales provisioning blocked the release');
      const worker = spawnSync('pnpm', ['release:gpoint', '--configure-gsales-worker', '--candidate', sha, '--owner-approved-sha', sha],
      { cwd: root, stdio: 'inherit', timeout: 150_000 });
      if (worker.status !== 0) throw new Error('Approved Mac lane configuration blocked the release');
    }
  }
  // Docker artifacts already passed CI. Canonical dry-run still independently
  // checks the live baseline, config, GitHub SHA and isolated source tree.
  const preflight = spawnSync('pnpm', ['release:gpoint', '--dry-run', '--candidate', sha], {
    cwd: root, env: { ...process.env, GPOINT_RELEASE_SKIP_BUILD: '1' }, stdio: 'inherit', timeout: 2_100_000,
  });
  preflightStatus = preflight.status === 0 ? 'PLANNED' : 'FAILED';
  if (preflight.status !== 0) throw new Error('Canonical preflight blocked the release');
  if (mode === 'preflight') {
    releaseCode = 0;
  } else {
    const result = spawnSync('pnpm', ['release:gpoint', '--apply', '--candidate', sha, '--confirm-production-apply'], {
      cwd: root, stdio: 'inherit', timeout: 1_800_000,
    });
    releaseCode = result.status ?? 1;
  }
} finally {
  const directory = ledger.manifestDir;
  const records = fs.readdirSync(directory).filter(name => !previous.has(name)).map(name => ({
    name, data: JSON.parse(fs.readFileSync(path.join(directory, name), 'utf8')),
  })).filter(record => record.data.candidateSha === sha);
  const setupStates = new Set(['CONFIG_PROVISION_BASELINE_RECORDED', 'GSales_CONFIG_PROVISIONED', 'WORKER_CONFIGURED']);
  const attempt = (mode === 'preflight' ? records : records.filter(r => r.data.status !== 'PLANNED' && !setupStates.has(r.data.status))).at(-1);
  const evidence = attempt?.data ?? { candidateSha: sha, status: 'BLOCKED', productionRef: null };
  fs.writeFileSync(process.env.RELEASE_RESULT, JSON.stringify({ ...evidence,
    protectedSetupEvidence: records.filter(r => setupStates.has(r.data.status)).map(r => r.data),
    operator: process.env.GITHUB_ACTOR, automationRunId: process.env.GITHUB_RUN_ID, automationRunAttempt: process.env.GITHUB_RUN_ATTEMPT,
    applicationVerification: mode === 'preflight' ? 'NOT_DEPLOYED' : 'OWNER_SMOKE_PENDING',
  }, null, 2) + '\n');
  if (process.env.GITHUB_OUTPUT) {
    fs.appendFileSync(process.env.GITHUB_OUTPUT, `preflight=${preflightStatus}\nstatus=${evidence.status}\n`);
  }
  // Persist canonical evidence to the dedicated ledger, never application main.
  const bookkeeping = persistReleaseBookkeeping({ mode, records, evidence, git, ...ledger });
  console.log(`RELEASE_BOOKKEEPING=${bookkeeping}`);
}
process.exitCode = releaseCode;
