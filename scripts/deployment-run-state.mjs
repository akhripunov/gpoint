import fs from 'node:fs';
import path from 'node:path';
import {inspectProductionReadOnly} from './release-gpoint-vps-adapter.mjs';

const SHA40 = /^[a-f0-9]{40}$/;
const GSALES_ROLES = ['API', 'AGATA_API', 'AGATA_MEMORY_WORKER'];

export function assertGsalesRuntimeState(facts) {
  const states = GSALES_ROLES.map(role => {
    const prefix = `LIVE_GSALES_${role}`;
    const status = facts[`${prefix}_STATUS`];
    const running = facts[`${prefix}_RUNNING`];
    const health = facts[`${prefix}_HEALTH`];
    if (status === 'running' && running === 'true' && health === 'healthy'
        && facts[`${prefix}_EXIT_CODE`] === '0' && facts[`${prefix}_OOM_KILLED`] === 'false') return 'running';
    if (status === 'exited' && running === 'false' && ['healthy', 'none'].includes(health)
        && facts[`${prefix}_EXIT_CODE`] === '0' && facts[`${prefix}_OOM_KILLED`] === 'false') return 'stopped';
    throw new Error(`VPS GSales ${role} state is unknown or damaged`);
  });
  if (!states.every(state => state === states[0]))
    throw new Error('VPS GSales components are in a mixed runtime state');
  return states[0];
}

export function gsalesRuntimeFingerprint(facts) {
  const mode = assertGsalesRuntimeState(facts);
  return {mode, migrationHead: facts.LIVE_GSALES_HEAD,
    migrationSha256: facts.LIVE_GSALES_MIGRATION_SHA256,
    containers: Object.fromEntries(GSALES_ROLES.map(role => {
      const prefix = `LIVE_GSALES_${role}`;
      return [role, {sourceSha: facts[`${prefix}_SHA`], imageId: facts[`${prefix}_IMAGE_ID`],
        status: facts[`${prefix}_STATUS`], running: facts[`${prefix}_RUNNING`],
        health: facts[`${prefix}_HEALTH`], exitCode: facts[`${prefix}_EXIT_CODE`],
        oomKilled: facts[`${prefix}_OOM_KILLED`]}];
    }))};
}

export function factsFromReadOnlyInspection(output) {
  const facts = {};
  for (const line of output.split(/\r?\n/)) {
    for (const match of line.matchAll(/(?:^|\s)([A-Z][A-Z0-9_]+)=([^\s]+)/g)) facts[match[1]] = match[2];
  }
  return facts;
}

export function stateFromLiveVps(facts, {gsalesBaselineSha} = {}) {
  const sha = facts.LIVE_RELEASE_ENV_SHA;
  if (!SHA40.test(sha ?? '')) throw new Error('VPS baseline has no verified full RELEASE_SHA');
  for (const key of ['LIVE_API_SHA', 'LIVE_WORKER_SHA', 'LIVE_BOT_SHA', 'LIVE_MINIAPP_SHA',
    'LIVE_AGATA_API_SHA', 'LIVE_AGATA_WORKER_SHA']) {
    if (facts[key] !== sha) throw new Error(`First-deploy VPS baseline is mixed or unverified at ${key}`);
  }
  for (const role of ['API', 'WORKER', 'BOT']) {
    if (facts[`LIVE_${role}_RUNNING`] !== 'true') throw new Error(`VPS ${role} service is not running`);
    if (role === 'API' && facts.LIVE_API_HEALTH !== 'healthy') throw new Error('VPS API health is not healthy');
    if (!/^sha256:[a-f0-9]{64}$/.test(facts[`LIVE_${role}_IMAGE`] ?? ''))
      throw new Error(`VPS ${role} immutable Docker image ID is unavailable`);
  }
  for (const role of ['API', 'WORKER']) {
    if (facts[`LIVE_AGATA_${role}_RUNNING`] !== 'true'
        || facts[`LIVE_AGATA_${role}_HEALTH`] !== 'healthy'
        || !/^sha256:[a-f0-9]{64}$/.test(facts[`LIVE_AGATA_${role}_IMAGE_ID`] ?? ''))
      throw new Error(`VPS AGATA ${role} identity or health is not verified`);
  }
  assertGsalesRuntimeState(facts);
  for (const role of GSALES_ROLES) {
    const prefix = `LIVE_GSALES_${role}`;
    if (facts[`${prefix}_SHA`] !== (gsalesBaselineSha ?? sha)
        || !/^sha256:[a-f0-9]{64}$/.test(facts[`${prefix}_IMAGE_ID`] ?? ''))
      throw new Error(`VPS GSales ${role} source or image identity is not verified`);
  }
  const prisma = facts.LIVE_PRISMA_HEAD;
  if (!prisma || ['NONE', 'READ_ONLY_DB_INSPECTION_FAILED'].includes(prisma))
    throw new Error('VPS baseline has no verified Prisma migration head');
  if (!/^[A-Za-z0-9_-]{1,32}$/.test(facts.LIVE_AGATA_HEAD ?? '') || facts.LIVE_AGATA_HEAD === 'ABSENT')
    throw new Error('VPS baseline has no verified AGATA Alembic head');
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(facts.LIVE_GSALES_HEAD ?? '') ||
      !/^[a-f0-9]{64}$/.test(facts.LIVE_GSALES_MIGRATION_SHA256 ?? ''))
    throw new Error('VPS baseline has no verified GSales migration identity');
  return {currentProductionSha: sha, currentProductionDbHead: prisma,
    miniappSourceSha: facts.LIVE_MINIAPP_SHA, activeRuntimeSha: facts.LIVE_API_SHA,
    agataRuntimeSha: facts.LIVE_AGATA_API_SHA, agataAlembicHead: facts.LIVE_AGATA_HEAD,
    gsalesMigrationHead: facts.LIVE_GSALES_HEAD, gsalesMigrationSha256: facts.LIVE_GSALES_MIGRATION_SHA256,
    gsalesRuntimeSha: gsalesBaselineSha ?? sha,
    gsalesRuntimeState: assertGsalesRuntimeState(facts), gsalesRuntime: gsalesRuntimeFingerprint(facts),
    lastReleaseStatus: 'COMPLETE',
    serverDeployStatus: 'COMPLETE', currentProductionRef: null,
    releaseOrigin: 'read-only-vps-baseline'};
}

export function initializeRunScopedState({directory, facts = factsFromReadOnlyInspection(inspectProductionReadOnly())}) {
  fs.mkdirSync(directory, {recursive: true, mode: 0o700});
  const state = stateFromLiveVps(facts);
  const stateFile = path.join(directory, 'production-state.json');
  const manifestDir = path.join(directory, 'manifests');
  fs.mkdirSync(manifestDir, {mode: 0o700});
  fs.writeFileSync(stateFile, `${JSON.stringify(state, null, 2)}\n`, {flag: 'wx', mode: 0o600});
  return {state, stateFile, manifestDir, facts};
}
