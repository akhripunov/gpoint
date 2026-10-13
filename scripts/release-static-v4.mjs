import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';
import { execFileSync } from 'node:child_process';
import { deriveV3ProductionParent, verifyComponentReceiptV4 } from './release-component-bundle-v4.mjs';
import { buildReadOnlyVPSProofCommand, parseReadOnlyVPSProof } from './release-component-vps-proof-v4.mjs';

const SHA = /^[a-f0-9]{40}$/;
const ROLES = ['app', 'agata', 'gsales'];

export function expectedStaticParent(state) {
  const sha = state?.currentProductionSha;
  if (!SHA.test(sha ?? '') || state.lastReleaseStatus !== 'COMPLETE' ||
      state.serverDeployStatus !== 'COMPLETE' ||
      state.releaseOrigin !== 'durable-production-release' ||
      state.recoveryRequired !== false)
    throw new Error('V4 static parent is not complete');
  if (!state.componentImages) return deriveV3ProductionParent(state);
  const images = state.componentImages;
  if (Object.keys(images).sort().join(',') !== ROLES.slice().sort().join(','))
    throw new Error('incomplete component image ledger');
  for (const role of ROLES) {
    if (!SHA.test(images[role]?.sourceSha ?? '') ||
        !/^sha256:[a-f0-9]{64}$/.test(images[role]?.imageId ?? '') ||
        images[role]?.platform !== 'linux/amd64')
      throw new Error('bad inherited source image proof');
  }
  if (state.miniappSourceSha !== sha)
    throw new Error('Mini App source identity drift');
  return {
    currentProductionSha: sha, lastReleaseStatus: 'COMPLETE',
    serverDeployStatus: 'COMPLETE', releaseOrigin: 'durable-production-release',
    recoveryRequired: false, images,
  };
}

export function staticReceiptAndImages(proof, state) {
  if (proof?.schema !== 2 || proof?.route !== 'STATIC_UI_ONLY' ||
      proof?.productionBaselineSha !== state?.currentProductionSha ||
      !SHA.test(proof.candidateSha ?? '') ||
      !SHA.test(proof.productionBaselineSha ?? '') ||
      proof.candidateSha === proof.productionBaselineSha)
    throw new Error('V4 static receipt not anchored to current production');
  const parent = expectedStaticParent(state);
  const images = Object.fromEntries(ROLES.map(role => [
    role,
    { name: { app: 'gpoint-runtime', agata: 'gpoint-agata-runtime',
              gsales: 'gpoint-g-sales-runtime' }[role],
      ...parent.images[role], kind: 'inherited' },
  ]));
  const staticRef = { kind: 'gpoint-miniapp-static',
                      candidateSha: proof.candidateSha };
  const manifest = {
    schema: 1, route: 'STATIC_UI_CANDIDATE',
    candidateSha: proof.candidateSha,
    previousSourceSha: proof.productionBaselineSha,
    mayDeploySelectively: false,
    immutableParentRequired: true,
    requiresLiveNativeImageProof: true,
    requiresV3ExactShaApproval: true,
    requiresV3PreflightAndRollback: true,
    images, static: staticRef,
  };
  verifyComponentReceiptV4(manifest, {
    candidate: proof.candidateSha, baseline: proof.productionBaselineSha,
    completeParent: parent, candidateArtifact: { static: staticRef },
  });
  return manifest;
}

export function verifyStaticLiveBaseline(state, proof, facts, nativeProof) {
  const manifest = staticReceiptAndImages(proof, state);
  if (facts?.LIVE_RELEASE_ENV_SHA !== manifest.images.app.sourceSha ||
      facts?.LIVE_MINIAPP_SHA !== state.currentProductionSha ||
      facts?.LIVE_PRISMA_HEAD !== state.currentProductionDbHead ||
      facts?.LIVE_AGATA_HEAD !== proof.agataHead ||
      facts?.LIVE_GSALES_HEAD !== proof.gsalesMigrationHead ||
      facts?.LIVE_GSALES_MIGRATION_SHA256 !== proof.gsalesMigrationSha256 ||
      facts?.LIVE_API_HEALTH !== 'healthy')
    throw new Error('static V4 baseline, DB identity or API health drift');
  for (const service of ['API', 'BOT', 'WORKER']) {
    if (facts['LIVE_' + service + '_SHA'] !== manifest.images.app.sourceSha ||
        facts['LIVE_' + service + '_RUNNING'] !== 'true')
      throw new Error('GPoint runtime source SHA/running mismatch');
  }
  for (const service of ['API', 'WORKER']) {
    if (facts['LIVE_AGATA_' + service + '_SHA'] !== manifest.images.agata.sourceSha ||
        facts['LIVE_AGATA_' + service + '_HEALTH'] !== 'healthy' ||
        facts['LIVE_AGATA_' + service + '_RUNNING'] !== 'true')
      throw new Error('AGATA runtime source SHA/readiness drift');
  }
  if (!nativeProof || Object.keys(nativeProof).length !== 3) throw new Error('missing live native images');
  for (const role of ROLES) {
    if (nativeProof[role].nativeImageId !== manifest.images[role].imageId ||
        nativeProof[role].revisionLabel !== manifest.images[role].sourceSha)
      throw new Error('live image differs from immutable ledger');
  }
  return manifest;
}

export function v4NativeProofOnVPS(parentImages, remoteCommand) {
  const output = remoteCommand(buildReadOnlyVPSProofCommand(parentImages));
  return parseReadOnlyVPSProof(output, parentImages);
}

export function verifyV4SourceAtCheckout({root, candidate, baseline, impact}) {
  if (!SHA.test(candidate ?? '') || !SHA.test(baseline ?? ''))
    throw new Error('bad static checkout SHA');
  const source = "import json,sys;from pathlib import Path;from release_component_impact_v4 import verified_plan;print(json.dumps(verified_plan(Path(sys.argv[1]),sys.argv[2],sys.argv[3],baseline_verified=True)))";
  let actual;
  try {
    actual = JSON.parse(execFileSync('python3', ['-c', source, root, baseline, candidate],
      { cwd: path.join(root, 'scripts'), encoding: 'utf8', timeout: 30_000 }));
  } catch { throw new Error('static checkout impact recomputation failed'); }
  if (!isDeepStrictEqual(actual, impact) ||
      actual.classification !== 'COMPONENT_CANDIDATE' ||
      actual.component !== 'ui' || actual.mayDeploySelectively !== false)
    throw new Error('static source proof differs from exact candidate diff');
  return actual;
}
