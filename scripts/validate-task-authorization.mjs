#!/usr/bin/env node
import fs from 'node:fs';
import {execFileSync} from 'node:child_process';
import {authorizationRecordPath, authorizationStateRef, candidateScopeDiff, validateTaskAuthorization} from './release-authorization.mjs';

const repository = 'akhripunov/gpoint';
const request = async (pathname, token) => {
  const response = await fetch(`https://api.github.com/repos/${repository}/${pathname}`, {
    headers: {authorization: `Bearer ${token}`, accept: 'application/vnd.github+json', 'x-github-api-version': '2022-11-28'},
    signal: AbortSignal.timeout(15_000),
  });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`GitHub authorization state read failed (HTTP ${response.status})`);
  return response.json();
};

const main = async () => {
  const id = process.env.TASK_AUTHORIZATION_ID ?? '';
  const candidate = process.env.CANDIDATE_SHA ?? '';
  const token = process.env.GITHUB_TOKEN ?? '';
  if (!token || !id) throw new Error('Task authorization ID and existing GITHUB_TOKEN are required');
  const record = JSON.parse(fs.readFileSync(authorizationRecordPath(id), 'utf8'));
  const changedPaths = candidateScopeDiff(process.cwd(), id, record.production_baseline_sha, candidate);
  const proof = validateTaskAuthorization({record, project: 'gpoint', authorizationId: id,
    candidateSha: candidate, currentMainSha: process.env.TASK_CURRENT_MAIN_SHA ?? '',
    productionSha: record.production_baseline_sha, changedPaths});
  for (const state of ['revoked', 'consumed']) {
    const ref = authorizationStateRef('gpoint', id, state).replace('refs/tags/', 'git/ref/tags/');
    if (await request(ref, token)) throw new Error(`Task authorization is already ${state}`);
  }
  if (process.env.GITHUB_OUTPUT) {
    fs.appendFileSync(process.env.GITHUB_OUTPUT,
      `task_authorization_id=${proof.authorizationId}\ntask_authorization_record_sha256=${proof.recordSha256}\ntask_authorization_changed_paths=${proof.changedPaths}\n`);
  }
  console.log(`TASK_SCOPED_AUTO_AUTHORIZATION=PASS ID=${proof.authorizationId} SHA=${proof.candidateSha} BASELINE=${proof.productionBaselineSha} RECORD_SHA256=${proof.recordSha256}`);
  return proof;
};

main().catch(error => { console.error(`TASK_SCOPED_AUTO_AUTHORIZATION=BLOCKED TYPE=${error?.name ?? 'Error'}`); process.exitCode = 1; });
