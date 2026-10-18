#!/usr/bin/env node
// Called only against the private disposable daemon created by Prepare.
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import { preparedImageCaptureCommand, preparedImageParityCommand } from './release-bundle.mjs';

const [receipt, candidate, scratch] = process.argv.slice(2);
const proof = JSON.parse(fs.readFileSync(receipt, 'utf8'));
const run = command => execFileSync('bash', ['-o', 'pipefail', '-c', command], { stdio: 'inherit', timeout: 900_000 });
run(preparedImageCaptureCommand(proof, candidate, scratch));
run(preparedImageParityCommand(proof, candidate, scratch));
for (const [role, repository] of [['app', 'gpoint-runtime'], ['agata', 'gpoint-agata-runtime'], ['gsales', 'gpoint-g-sales-runtime']]) {
  const tag = `${repository}:${candidate}`;
  const native = execFileSync('docker', ['image', 'inspect', tag, '--format', '{{.Id}}'], { encoding: 'utf8' }).trim();
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
