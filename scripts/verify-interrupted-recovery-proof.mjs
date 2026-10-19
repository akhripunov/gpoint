#!/usr/bin/env node
import {verifyInterruptedRecoveryProof} from './reconcile-deployment.mjs';

const MAX_BYTES = 16 * 1024 * 1024;

async function readInput() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_BYTES) throw new Error('input too large');
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

try {
  const {proof, expected} = await readInput();
  verifyInterruptedRecoveryProof(proof, expected);
  process.stdout.write('{"verified":true}\n');
} catch {
  process.stderr.write('INTERRUPTED_RECOVERY_PROOF=BLOCKED\n');
  process.exitCode = 1;
}
