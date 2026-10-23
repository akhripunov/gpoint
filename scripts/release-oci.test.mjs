import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import { validateOciReceipt, createOciPullScript, createOciVerifyScript,
  verifyOciReadAccess } from './release-oci.mjs';

const sha = 'a'.repeat(40);
const configDigest = 'sha256:' + 'c'.repeat(64);
const manifestJson = (config = configDigest) => JSON.stringify({
  schemaVersion: 2,
  mediaType: 'application/vnd.oci.image.manifest.v1+json',
  config: { mediaType: 'application/vnd.oci.image.config.v1+json', digest: config, size: 1 },
  layers: [],
});
const digestOf = (value) => `sha256:${createHash('sha256').update(value).digest('hex')}`;
const digest = digestOf(manifestJson());
const receipt = (manifestDigest = digest) => ({
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
    digest: manifestDigest,
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

test('GHCR pull verifies the manifest config descriptor before accepting Docker image-store identity', () => {
  const validated = validateOciReceipt(receipt(), sha, { runId: 123, runAttempt: 1 });
  const script = createOciPullScript(validated, sha, { username: 'akhripunov' });
  assert.match(script, /docker buildx imagetools inspect --raw/);
  assert.match(script, /GHCR_CONFIG_DIGEST_MISMATCH/);
  assert.match(script, /GHCR_IMAGE_ID_MISMATCH/);
  assert.match(script, /RepoDigests/);
});

test('GHCR pull accepts Docker image stores that report the verified manifest digest as Id', () => {
  const validated = validateOciReceipt(receipt(), sha, { runId: 123, runAttempt: 1 });
  const script = createOciPullScript(validated, sha, { username: 'akhripunov' });
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpoint-oci-test-'));
  try {
    const docker = path.join(root, 'docker');
    fs.writeFileSync(docker, `#!/bin/sh
set -eu
case "$1 $2" in
  'login ghcr.io') cat >/dev/null; exit 0 ;;
  'logout ghcr.io') exit 0 ;;
  'buildx imagetools')
    case "$5" in
      *"@$FAKE_CHILD_DIGEST") printf '%s' "$FAKE_CHILD_MANIFEST" ;;
      *) printf '%s' "$FAKE_MANIFEST" ;;
    esac
    exit 0 ;;
  'pull '*) exit 0 ;;
  'image inspect')
    ref=$3
    format=$5
    case "$format" in
      '{{.Os}}/{{.Architecture}}') printf '%s\\n' "\${FAKE_PLATFORM:-linux/amd64}" ;;
      '{{.Id}}') printf '%s\\n' "$FAKE_IMAGE_ID" ;;
      '{{index .Config.Labels "org.opencontainers.image.revision"}}') printf '%s\\n' "$FAKE_REVISION" ;;
      '{{index .Config.Labels "org.opencontainers.image.source"}}') printf '%s\\n' "\${FAKE_SOURCE:-https://github.com/akhripunov/gpoint}" ;;
      '{{range .RepoDigests}}{{println .}}{{end}}') printf '%s\\n' "$ref" ;;
      *) exit 90 ;;
    esac
    ;;
  'image tag') exit 0 ;;
  *) exit 91 ;;
esac
`);
    fs.chmodSync(docker, 0o755);
    const run = (scriptText, imageId, rawManifest = manifestJson(), overrides = {}) => spawnSync('/bin/bash', ['-c', scriptText], {
      encoding: 'utf8',
      input: 'dummy-read-only-token',
      env: {
        ...process.env,
        PATH: `${root}:${process.env.PATH}`,
        FAKE_MANIFEST: rawManifest,
        FAKE_CHILD_MANIFEST: overrides.FAKE_CHILD_MANIFEST || '',
        FAKE_CHILD_DIGEST: overrides.FAKE_CHILD_DIGEST || 'no-child-manifest',
        FAKE_IMAGE_ID: imageId,
        FAKE_REVISION: sha,
        ...overrides,
      },
    });

    const result = run(script, digest);
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /GPOINT_GHCR_IMAGES_VERIFIED=YES/);
    assert.doesNotMatch(result.stdout + result.stderr, /dummy-read-only-token/);

    const configIdStore = run(script, configDigest);
    assert.equal(configIdStore.status, 0, configIdStore.stderr);
    assert.match(configIdStore.stdout, /GPOINT_GHCR_IMAGES_VERIFIED=YES/);

    const childManifest = manifestJson();
    const childDigest = digestOf(childManifest);
    const indexManifest = JSON.stringify({ schemaVersion: 2, mediaType: 'application/vnd.oci.image.index.v1+json',
      manifests: [{ digest: childDigest, platform: { os: 'linux', architecture: 'amd64' } }] });
    const indexDigest = digestOf(indexManifest);
    const indexScript = createOciPullScript(
      validateOciReceipt(receipt(indexDigest), sha, { runId: 123, runAttempt: 1 }), sha,
      { username: 'akhripunov' },
    );
    const indexPull = run(indexScript, indexDigest, indexManifest, {
      FAKE_CHILD_MANIFEST: childManifest, FAKE_CHILD_DIGEST: childDigest,
    });
    assert.equal(indexPull.status, 0, indexPull.stderr);
    assert.match(indexPull.stdout, /GPOINT_GHCR_IMAGES_VERIFIED=YES/);

    const wrongChildManifest = manifestJson('sha256:' + 'd'.repeat(64));
    const wrongChildDigest = digestOf(wrongChildManifest);
    const wrongIndexManifest = JSON.stringify({ schemaVersion: 2, mediaType: 'application/vnd.oci.image.index.v1+json',
      manifests: [{ digest: wrongChildDigest, platform: { os: 'linux', architecture: 'amd64' } }] });
    const wrongIndexDigest = digestOf(wrongIndexManifest);
    const wrongIndexScript = createOciPullScript(
      validateOciReceipt(receipt(wrongIndexDigest), sha, { runId: 123, runAttempt: 1 }), sha,
      { username: 'akhripunov' },
    );
    const badIndexConfig = run(wrongIndexScript, wrongIndexDigest, wrongIndexManifest, {
      FAKE_CHILD_MANIFEST: wrongChildManifest, FAKE_CHILD_DIGEST: wrongChildDigest,
    });
    assert.notEqual(badIndexConfig.status, 0);
    assert.match(badIndexConfig.stdout + badIndexConfig.stderr, /GHCR_CONFIG_DIGEST_MISMATCH/);

    const tamperedRoot = run(script, digest, manifestJson().replace(configDigest, 'sha256:' + 'd'.repeat(64)));
    assert.notEqual(tamperedRoot.status, 0);
    assert.match(tamperedRoot.stdout + tamperedRoot.stderr, /GHCR_MANIFEST_DIGEST_OR_CONTENT_MISMATCH/);

    const tamperedChild = run(indexScript, indexDigest, indexManifest, {
      FAKE_CHILD_MANIFEST: childManifest.replace(configDigest, 'sha256:' + 'd'.repeat(64)),
      FAKE_CHILD_DIGEST: childDigest,
    });
    assert.notEqual(tamperedChild.status, 0);
    assert.match(tamperedChild.stdout + tamperedChild.stderr, /GHCR_CHILD_MANIFEST_DIGEST_OR_CONTENT_MISMATCH/);

    const wrongConfig = manifestJson('sha256:' + 'd'.repeat(64));
    const wrongConfigDigest = digestOf(wrongConfig);
    const wrongConfigScript = createOciPullScript(
      validateOciReceipt(receipt(wrongConfigDigest), sha, { runId: 123, runAttempt: 1 }), sha,
      { username: 'akhripunov' },
    );
    const badConfig = run(wrongConfigScript, wrongConfigDigest, wrongConfig);
    assert.notEqual(badConfig.status, 0);
    assert.match(badConfig.stdout + badConfig.stderr, /GHCR_CONFIG_DIGEST_MISMATCH/);

    const badImageId = run(script, 'sha256:' + 'e'.repeat(64));
    assert.notEqual(badImageId.status, 0);
    assert.match(badImageId.stdout + badImageId.stderr, /GHCR_IMAGE_ID_MISMATCH/);

    const badPlatform = run(script, configDigest, manifestJson(), { FAKE_PLATFORM: 'linux/arm64' });
    assert.notEqual(badPlatform.status, 0);
    assert.match(badPlatform.stdout + badPlatform.stderr, /GHCR_PLATFORM_MISMATCH/);

    const badRevision = run(script, configDigest, manifestJson(), { FAKE_REVISION: 'd'.repeat(40) });
    assert.notEqual(badRevision.status, 0);
    assert.match(badRevision.stdout + badRevision.stderr, /GHCR_REVISION_LABEL_MISMATCH/);

    const badSource = run(script, configDigest, manifestJson(), { FAKE_SOURCE: 'https://attacker.invalid/repository' });
    assert.notEqual(badSource.status, 0);
    assert.match(badSource.stdout + badSource.stderr, /GHCR_SOURCE_LABEL_MISMATCH/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
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
  assert.equal(requests[0].url, `https://ghcr.io/v2/akhripunov/gpoint-runtime/manifests/${digest}`);
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
  assert.equal((script.match(/grep -Fxq '[^']+@sha256:[a-f0-9]{64}'/g) || []).length, 3);
  assert.equal((script.match(/case "\$image_id" in/g) || []).length, 6);
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
    assert.match(url, /^https:\/\/ghcr\.io\/v2\/akhripunov\/[a-z0-9-]+\/manifests\/sha256:[a-f0-9]{64}$/);
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

function instantRetry() {
  let time = 0;
  const events = [];
  return { events, options: { now: () => time, sleep: async ms => { time += ms; },
    jitter: () => 1, emit: message => events.push(message), statePath: null, budgetSeconds: 1 } };
}

for (const point of ['manifest', 'token', 'token_body', 'bearer_manifest']) {
  test(`GHCR read recovers a transient ${point} failure with the exact digest still required`, async () => {
    const retry = instantRetry();
    let failed = false;
    const requests = [];
    const fetcher = async (url, options) => {
      requests.push({ url, options });
      assert.ok(options.signal instanceof AbortSignal);
      const isToken = url.includes('/token?');
      const current = isToken ? 'token' : options.headers.Authorization ? 'bearer_manifest' : 'manifest';
      const interrupt = () => { failed = true; throw Object.assign(new TypeError('secret-url token=secret'),
        { cause: Object.assign(new Error('secret'), { code: 'UND_ERR_CONNECT_TIMEOUT' }) }); };
      if (!failed && current === point) interrupt();
      if (isToken) return { ok: true, status: 200, url, arrayBuffer: async () => {
        if (!failed && point === 'token_body') interrupt();
        return new TextEncoder().encode('{"token":"bearer-secret"}').buffer;
      } };
      if (!options.headers.Authorization) return { ok: false, status: 401, url,
        headers: new Headers({ 'WWW-Authenticate': `Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:${url.split('/v2/')[1].split('/manifests/')[0]}:pull"` }) };
      return { ok: true, status: 200, url, headers: new Headers({ 'Docker-Content-Digest': digest }) };
    };
    assert.equal(await verifyOciReadAccess(receipt(), 'akhripunov', 'secret', fetcher, retry.options), 'PASS');
    assert.equal(failed, true);
    assert.ok(retry.events.some(value => value.includes('NETWORK_RESTORED')));
    assert.doesNotMatch(retry.events.join('\n'), /secret|https:/);
    assert.ok(requests.every(({ options }) => options.method === 'GET' && options.redirect === 'error'));
  });
}

for (const failure of ['http', 'certificate', 'digest']) {
  test(`GHCR ${failure} failure is not retried and reports only safe diagnostics`, async () => {
    const retry = instantRetry();
    let calls = 0;
    const fetcher = async url => {
      calls++;
      if (failure === 'certificate') throw Object.assign(new Error('secret'), { code: 'CERT_HAS_EXPIRED' });
      return { ok: failure !== 'http', status: failure === 'http' ? 403 : 200, url,
        headers: new Headers({ 'Docker-Content-Digest': 'sha256:' + 'f'.repeat(64) }) };
    };
    await assert.rejects(verifyOciReadAccess(receipt(), 'akhripunov', 'secret', fetcher, retry.options), error => {
      assert.match(error.message, /GHCR exact-digest read check failed.*role=app/);
      assert.match(error.message, failure === 'http' ? /HTTP_403/ : failure === 'certificate' ? /CERT_HAS_EXPIRED/ : /DIGEST_MISMATCH/);
      assert.doesNotMatch(error.stack, /secret|https:/);
      assert.equal(error.cause, undefined);
      return true;
    });
    assert.equal(calls, 1);
    assert.ok(!retry.events.some(value => value.includes('NETWORK_RETRYING')));
  });
}

test('GHCR image roles consume one persisted retry budget and stop before another request', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ghcr-read-budget-'));
  const statePath = path.join(directory, 'budget');
  fs.writeFileSync(statePath, '0.99\n');
  let time = 0;
  let calls = 0;
  const options = { statePath, budgetSeconds: 1, now: () => time,
    sleep: async ms => { time += ms; }, jitter: () => 1, emit: () => {} };
  try {
    await assert.rejects(verifyOciReadAccess(receipt(), 'akhripunov', 'secret', async url => {
      if (++calls === 1) {
        time += 8;
        throw Object.assign(new Error('secret'), {code: 'ECONNRESET'});
      }
      time += 1;
      return {ok: true, status: 200, url, headers: new Headers({'Docker-Content-Digest': digest})};
    }, options), /NETWORK_BUDGET_EXHAUSTED/);
    assert.equal(calls, 2);
    assert.ok(Number(fs.readFileSync(statePath, 'utf8')) >= 1);
  } finally { fs.rmSync(directory, {recursive: true, force: true}); }
});

test('GHCR read timeout is capped and reduced to remaining budget', async () => {
  const original = AbortSignal.timeout;
  const caps = [];
  AbortSignal.timeout = milliseconds => { caps.push(milliseconds); return original(milliseconds); };
  const fetcher = async url => ({ok: true, status: 200, url,
    headers: new Headers({'Docker-Content-Digest': digest})});
  try {
    await verifyOciReadAccess(receipt(), 'akhripunov', 'secret', fetcher, {...instantRetry().options, budgetSeconds: 100});
    assert.deepEqual(caps, [30_000, 30_000, 30_000]);
    caps.length = 0;
    await verifyOciReadAccess(receipt(), 'akhripunov', 'secret', fetcher, {...instantRetry().options, budgetSeconds: 0.05});
    assert.deepEqual(caps, [50, 50, 50]);
  } finally { AbortSignal.timeout = original; }
});
