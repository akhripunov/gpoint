#!/usr/bin/env node
// Called only against the private disposable daemon created by Prepare.
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { preparedImageCaptureCommand, preparedImageParityCommand } from './release-bundle.mjs';

const [receipt, candidate, scratch] = process.argv.slice(2);
const proof = JSON.parse(fs.readFileSync(receipt, 'utf8'));
const run = command => execFileSync('bash', ['-o', 'pipefail', '-c', command], { stdio: 'inherit', timeout: 900_000 });
run(preparedImageCaptureCommand(proof, candidate, scratch));
run(preparedImageParityCommand(proof, candidate, scratch));
for (const [role, repository] of [['app', 'gpoint-runtime'], ['agata', 'gpoint-agata-runtime'], ['gsales', 'gpoint-g-sales-runtime']]) {
  const tag = `${repository}:${candidate}`;
  const native = execFileSync('docker', ['image', 'inspect', tag, '--format', '{{.Id}}'], { encoding: 'utf8' }).trim();
  const expectedConfig = proof.images?.[role]?.id;
  if (!/^sha256:[a-f0-9]{64}$/.test(native) || native === expectedConfig)
    throw new Error(`Containerd manifest/config identities were not distinguished: ${role}`);
  const identityFile = path.join(scratch, `gpoint-${role === 'app' ? '' : role === 'agata' ? 'agata-' : 'g-sales-'}image-id-${candidate}`);
  const capturedIdentity = fs.readFileSync(identityFile, 'utf8');
  if (capturedIdentity.trim() !== native) throw new Error(`Captured native image identity changed: ${role}`);
  try {
    // A config digest must never be mistaken for the native containerd image ID.
    fs.writeFileSync(identityFile, expectedConfig + '\n');
    const rejected = spawnSync('bash', ['-c', preparedImageParityCommand(proof, candidate, scratch)],
      { encoding: 'utf8', timeout: 90_000 });
    const expectedRejection = `GPOINT_CI_PARITY_IDENTITY_MISMATCH=${role}`;
    if (rejected.status !== 42 || !`${rejected.stdout}\n${rejected.stderr}`.includes(expectedRejection))
      throw new Error(`Containerd parity did not report the expected identity mismatch: ${role}`);
  } finally { fs.writeFileSync(identityFile, capturedIdentity); }
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
