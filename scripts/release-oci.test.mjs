import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {fileURLToPath} from 'node:url';
import { validateOciReceipt, createOciPullScript, createOciVerifyScript,
  verifyOciReadAccess } from './release-oci.mjs';

const sha = 'a'.repeat(40);
const digest = 'sha256:' + 'b'.repeat(64);
const configDigest = 'sha256:' + 'c'.repeat(64);
const receipt = () => ({
  schema: 3,
  status: 'READY',
  candidateSha: sha,
  runId: 123,
  runAttempt: 1,
  transport: 'ghcr-oci',
  images: Object.fromEntries([
    ['app', 'gpoint-runtime'], ['agata', 'gpoint-agata-runtime'], ['gsales', 'gpoint-g-sales-runtime'],
  ].map(([role, name]) => [role, {
    repository: `ghcr.io/akhripunov/${name}`,
    digest,
    configDigest,
    sourceSha: sha,
    platform: 'linux/amd64',
  }])),
});

test('accepts exact candidate and run-bound GHCR digests for every runtime image', () => {
  const validated = validateOciReceipt(receipt(), sha, { runId: 123, runAttempt: 1 });
  assert.equal(validated.images.app.reference, `ghcr.io/akhripunov/gpoint-runtime@${digest}`);
  assert.deepEqual(Object.keys(validated.images).sort(), ['agata', 'app', 'gsales']);
});

test('rejects a receipt from a different candidate or Prepare attempt', () => {
  assert.throws(() => validateOciReceipt({ ...receipt(), candidateSha: 'd'.repeat(40) }, sha,
    { runId: 123, runAttempt: 1 }), /candidate/i);
  assert.throws(() => validateOciReceipt(receipt(), sha,
    { runId: 123, runAttempt: 2 }), /attempt/i);
});

test('rejects mutable tags, wrong registries, missing roles, and invalid digests', () => {
  const mutable = receipt(); mutable.images.app.digest = 'latest';
  assert.throws(() => validateOciReceipt(mutable, sha, { runId: 123, runAttempt: 1 }), /digest/i);
  const wrongRegistry = receipt(); wrongRegistry.images.app.repository = 'docker.io/akhripunov/gpoint-runtime';
  assert.throws(() => validateOciReceipt(wrongRegistry, sha, { runId: 123, runAttempt: 1 }), /repository/i);
  const missing = receipt(); delete missing.images.gsales;
  assert.throws(() => validateOciReceipt(missing, sha, { runId: 123, runAttempt: 1 }), /image/i);
});

test('rejects platform or source identity mismatch', () => {
  const wrongArch = receipt(); wrongArch.images.agata.platform = 'linux/arm64';
  assert.throws(() => validateOciReceipt(wrongArch, sha, { runId: 123, runAttempt: 1 }), /platform/i);
  const wrongSource = receipt(); wrongSource.images.gsales.sourceSha = 'd'.repeat(40);
  assert.throws(() => validateOciReceipt(wrongSource, sha, { runId: 123, runAttempt: 1 }), /source/i);
});

test('pull plan addresses exact digests, verifies labels, and never embeds credentials', () => {
  const validated = validateOciReceipt(receipt(), sha, { runId: 123, runAttempt: 1 });
  const secret = 'never-in-the-script-or-logs';
  const script = createOciPullScript(validated, sha, { username: 'akhripunov' });
  assert.equal((script.match(/docker pull /g) || []).length, 3);
  assert.equal((script.match(/docker pull '[^']+@sha256:/g) || []).length, 3);
  assert.match(script, /org\.opencontainers\.image\.revision/);
  assert.match(script, /org\.opencontainers\.image\.source/);
  assert.match(script, /docker image inspect '[^']+' --format .*\| grep -Fx/);
  assert.doesNotMatch(script, /test "\$\(docker image inspect[^\n]+\)" \| grep/);
  assert.match(script, /linux\/amd64/);
  assert.match(script, /docker login ghcr\.io --username 'akhripunov' --password-stdin/);
  assert.doesNotMatch(script, new RegExp(secret));
  assert.doesNotMatch(script, /Bearer\s/i);
  assert.throws(() => createOciPullScript(validated, 'd'.repeat(40)), /candidate/i);
});

test('workflow actor identity is used for GITHUB_TOKEN GHCR access and Docker pulls', async () => {
  const validated = validateOciReceipt(receipt(), sha, { runId: 123, runAttempt: 1 });
  const username = 'github-actions[bot]';
  const script = createOciPullScript(validated, sha, { username });
  assert.match(script, /docker login ghcr\.io --username 'github-actions\[bot\]' --password-stdin/);

  const requests = [];
  const fetcher = async (url, options) => {
    requests.push({url, options});
    if (url.includes('/manifests/')) {
      if (options.headers.Authorization === 'Bearer bearer-secret') return {
        ok: true, status: 200, url, headers: new Headers({'Docker-Content-Digest': digest}),
      };
      return {ok: false, status: 401, url,
        headers: new Headers({ 'WWW-Authenticate': `Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:${url.split('/v2/')[1].split('/manifests/')[0]}:pull"` })};
    }
    return {ok: true, status: 200, url, headers: new Headers(),
      arrayBuffer: async () => new TextEncoder().encode('{"token":"bearer-secret"}').buffer};
  };
  assert.equal(await verifyOciReadAccess(validated, username, 'workflow-token', fetcher), 'PASS');
  const basic = requests.find(request => request.url.startsWith('https://ghcr.io/token?')).options.headers.Authorization;
  assert.equal(Buffer.from(basic.slice('Basic '.length), 'base64').toString('utf8'), `${username}:workflow-token`);

  const workflowPath = fileURLToPath(new URL('../.github/workflows/gpoint-deploy.yml', import.meta.url));
  const workflow = fs.readFileSync(workflowPath, 'utf8');
  assert.match(workflow, /GHCR_READ_USERNAME:\s*\$\{\{\s*github\.actor\s*\}\}/);
  assert.match(workflow, /GHCR_READ_TOKEN:\s*\$\{\{\s*github\.token\s*\}\}/);
});

test('GHCR username validation permits GitHub Actions bot identity and rejects shell metacharacters', async () => {
  const validated = validateOciReceipt(receipt(), sha, { runId: 123, runAttempt: 1 });
  assert.doesNotThrow(() => createOciPullScript(validated, sha, {username: 'github-actions[bot]'}));
  assert.throws(() => createOciPullScript(validated, sha, {username: "bad';touch /tmp/pwned;#"}), /username/i);
  await assert.rejects(verifyOciReadAccess(validated, "bad';touch /tmp/pwned;#", 'token', fetch), /GHCR exact-digest read check failed/);
});

test('post-switch proof checks local tags against the same exact OCI digests and config IDs', () => {
  const validated = validateOciReceipt(receipt(), sha, { runId: 123, runAttempt: 1 });
  const script = createOciVerifyScript(validated, sha);
  assert.equal((script.match(/RepoDigests/g) || []).length, 3);
  assert.equal((script.match(/grep -Fx '[^']+@sha256:[a-f0-9]{64}'/g) || []).length, 3);
  assert.equal((script.match(/linux\/amd64/g) || []).length, 3);
  assert.equal((script.match(/org\.opencontainers\.image\.source/g) || []).length, 3);
  assert.equal((script.match(/org\.opencontainers\.image\.revision/g) || []).length, 3);
  assert.match(script, /GPOINT_GHCR_POST_SWITCH_PARITY=YES/);
  assert.throws(() => createOciVerifyScript(validated, 'd'.repeat(40)), /candidate/i);
});

test('preflight verifies public exact manifest digests without downloading layers', async () => {
  const validated = validateOciReceipt(receipt(), sha, { runId: 123, runAttempt: 1 });
  const requests = [];
  const fetcher = async (url, options) => {
    requests.push({ url, options });
    return { ok: true, status: 200, url, headers: new Headers({ 'Docker-Content-Digest': digest }) };
  };
  assert.equal(await verifyOciReadAccess(validated, 'akhripunov', 'read-token', fetcher), 'PASS');
  assert.equal(requests.length, 3);
  for (const { url, options } of requests) {
    assert.match(url, new RegExp(`/manifests/${digest.replace(':', ':')}$`));
    assert.equal(options.headers.Authorization, undefined);
    assert.equal(options.method, 'GET');
    assert.equal(options.redirect, 'error');
  }
});

test('preflight follows only the exact GHCR pull-scope Bearer challenge', async () => {
  const validated = validateOciReceipt(receipt(), sha, { runId: 123, runAttempt: 1 });
  const requests = [];
  const fetcher = async (url, options) => {
    requests.push({ url, options });
    if (url.includes('/manifests/')) {
      if (!options.headers.Authorization) return {
        ok: false, status: 401, url,
        headers: new Headers({ 'WWW-Authenticate': `Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:${url.split('/v2/')[1].split('/manifests/')[0]}:pull"` }),
      };
      return { ok: true, status: 200, url, headers: new Headers({ 'Docker-Content-Digest': digest }) };
    }
    return { ok: true, status: 200, url, headers: new Headers(),
      arrayBuffer: async () => new TextEncoder().encode('{"token":"bearer-secret"}').buffer };
  };
  assert.equal(await verifyOciReadAccess(validated, 'akhripunov', 'read-token', fetcher), 'PASS');
  assert.equal(requests.length, 9);
  assert.match(requests[1].url, /^https:\/\/ghcr\.io\/token\?/);
  assert.match(requests[1].options.headers.Authorization, /^Basic /);
  assert.doesNotMatch(requests[1].options.headers.Authorization, /read-token/);
  assert.equal(requests[2].options.headers.Authorization, 'Bearer bearer-secret');
  for (const request of requests) assert.equal(request.options.redirect, 'error');
});

test('preflight blocks bad credentials or digest response without exposing credentials', async () => {
  const validated = validateOciReceipt(receipt(), sha, { runId: 123, runAttempt: 1 });
  await assert.rejects(
    verifyOciReadAccess(validated, 'akhripunov', 'secret', async (url) => ({
      ok: false, status: 401, url, headers: new Headers(),
    })),
    (error) => error.message.includes('GHCR exact-digest read check failed') && !error.message.includes('secret'),
  );
});
