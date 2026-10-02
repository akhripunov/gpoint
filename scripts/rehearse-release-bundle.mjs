#!/usr/bin/env node
// Called only against the private disposable daemon created by Prepare.
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { importBundleCommand, preparedImageCaptureCommand, preparedImageParityCommand, reuseBundleCommand } from './release-bundle.mjs';

const [receipt, candidate, scratch] = process.argv.slice(2);
const proof = JSON.parse(fs.readFileSync(receipt, 'utf8'));
const run = command => execFileSync('bash', ['-o', 'pipefail', '-c', command], { stdio: 'inherit', timeout: 900_000 });
run(importBundleCommand(proof, candidate, scratch));
run(preparedImageCaptureCommand(proof, candidate, scratch));
run(preparedImageParityCommand(proof, candidate, scratch));
const reusable = execFileSync('bash', ['-c', reuseBundleCommand(proof, candidate)], { encoding: 'utf8', timeout: 90_000 });
if (!reusable.includes('GPOINT_CI_IMAGES_REUSABLE')) throw new Error('Imported candidate cannot be reused');
for (const [role, repository] of [['app', 'gpoint-runtime'], ['agata', 'gpoint-agata-runtime']]) {
  const tag = `${repository}:${candidate}`;
  const native = execFileSync('docker', ['image', 'inspect', tag, '--format', '{{.Id}}'], { encoding: 'utf8' }).trim();
  // This is the real production-store regression, not a mocked Docker response.
  if (native === proof.images[role].id) throw new Error('Rehearsal did not exercise manifest/config identity difference');
  const file = path.join(scratch, `gpoint-${role === 'agata' ? 'agata-' : ''}image-id-${candidate}`);
  const captured = fs.readFileSync(file, 'utf8');
  try {
    fs.writeFileSync(file, proof.images[role].id + '\n');
    const rejected = spawnSync('bash', ['-c', preparedImageParityCommand(proof, candidate, scratch)], { encoding: 'utf8', timeout: 90_000 });
    if (rejected.status === null || rejected.status === 0) throw new Error('Parity accepted the old config-as-native regression');
  } finally { fs.writeFileSync(file, captured); }
  console.log(`PREPARE_IDENTITY_REGRESSION_REJECTED=${role}`);
  // Prove runtime identity without starting a poller, provider, or DB client.
  const name = `gpoint-rehearsal-${role}-${candidate.slice(0, 12)}`;
  const id = execFileSync('docker', ['create', '--name', name, '--network', 'none', '--entrypoint', '/bin/true', tag], { encoding: 'utf8' }).trim();
  try {
    const actual = execFileSync('docker', ['inspect', id, '--format', '{{.Image}}'], { encoding: 'utf8' }).trim();
    if (actual !== native) throw new Error(`Runtime image parity mismatch: ${role}`);
  } finally { execFileSync('docker', ['rm', id], { stdio: 'ignore' }); }
}
run(preparedImageParityCommand(proof, candidate, scratch));
console.log('PREPARE_DEPLOY_REHEARSAL=PASS');
