const SHA40 = /^[a-f0-9]{40}$/;
const DIGEST = /^sha256:[a-f0-9]{64}$/;
const GHCR_USERNAME = /^[A-Za-z0-9-]{1,39}(?:\[bot\])?$/;
const IMAGE_REPOSITORIES = Object.freeze({
  app: 'ghcr.io/akhripunov/gpoint-runtime',
  agata: 'ghcr.io/akhripunov/gpoint-agata-runtime',
  gsales: 'ghcr.io/akhripunov/gpoint-g-sales-runtime',
});

function fail(message) { throw new Error(message); }

export function validateOciReceipt(receipt, candidate, { runId, runAttempt } = {}) {
  if (!SHA40.test(candidate ?? '') || receipt?.schema !== 3 || receipt?.transport !== 'ghcr-oci' ||
      receipt?.status !== 'READY' || receipt?.candidateSha !== candidate) fail('OCI receipt candidate or schema mismatch');
  if (!Number.isSafeInteger(receipt.runId) || receipt.runId < 1 || String(receipt.runId) !== String(runId) ||
      !Number.isSafeInteger(receipt.runAttempt) || receipt.runAttempt < 1 ||
      String(receipt.runAttempt) !== String(runAttempt)) fail('OCI receipt run or attempt mismatch');
  const keys = Object.keys(receipt.images ?? {}).sort();
  if (keys.join(',') !== Object.keys(IMAGE_REPOSITORIES).sort().join(',')) fail('OCI receipt image roles are incomplete or unexpected');
  const images = {};
  for (const [role, expectedRepository] of Object.entries(IMAGE_REPOSITORIES)) {
    const image = receipt.images[role];
    if (!image || image.repository !== expectedRepository) fail(`OCI image repository mismatch: ${role}`);
    if (!DIGEST.test(image.digest ?? '') || !DIGEST.test(image.configDigest ?? '')) fail(`OCI image digest is invalid: ${role}`);
    if (image.sourceSha !== candidate) fail(`OCI image source SHA mismatch: ${role}`);
    if (image.platform !== 'linux/amd64') fail(`OCI image platform mismatch: ${role}`);
    images[role] = { ...image, reference: `${expectedRepository}@${image.digest}` };
  }
  return { ...receipt, images };
}

export async function verifyOciReadAccess(receipt, username, token, fetcher = fetch) {
  if (!GHCR_USERNAME.test(username ?? '') || typeof token !== 'string' || !token.trim()) {
    fail('GHCR exact-digest read check failed');
  }
  if (receipt?.schema !== 3 || receipt?.transport !== 'ghcr-oci') fail('GHCR exact-digest read check failed');
  try {
    for (const [role, expectedRepository] of Object.entries(IMAGE_REPOSITORIES)) {
      const image = receipt.images?.[role];
      if (!image || image.repository !== expectedRepository || !DIGEST.test(image.digest ?? '')) {
        fail('GHCR exact-digest read check failed');
      }
      const url = `https://${expectedRepository}/v2/${expectedRepository.slice('ghcr.io/'.length)}/manifests/${image.digest}`;
      const headers = {
        Accept: [
          'application/vnd.oci.image.index.v1+json',
          'application/vnd.oci.image.manifest.v1+json',
          'application/vnd.docker.distribution.manifest.list.v2+json',
          'application/vnd.docker.distribution.manifest.v2+json',
        ].join(', '),
      };
      const request = (requestHeaders = headers) => fetcher(url, {
        method: 'GET',
        redirect: 'error',
        headers: requestHeaders,
      });
      let response = await request();
      if (response.status === 401) {
        const challenge = response.headers.get('www-authenticate') ?? '';
        if (!challenge.startsWith('Bearer ')) fail('GHCR exact-digest read check failed');
        const attributes = Object.fromEntries([...challenge.slice(7).matchAll(/([A-Za-z][A-Za-z0-9_-]*)="([^"]*)"/g)]
          .map((match) => [match[1], match[2]]));
        const scope = `repository:${expectedRepository.slice('ghcr.io/'.length)}:pull`;
        if (attributes.realm !== 'https://ghcr.io/token' || attributes.service !== 'ghcr.io' || attributes.scope !== scope) {
          fail('GHCR exact-digest read check failed');
        }
        const tokenUrl = new URL(attributes.realm);
        tokenUrl.searchParams.set('service', attributes.service);
        tokenUrl.searchParams.set('scope', scope);
        const basic = Buffer.from(`${username}:${token.trim()}`, 'utf8').toString('base64');
        const tokenResponse = await fetcher(tokenUrl.toString(), {
          method: 'GET', redirect: 'error',
          headers: { Authorization: `Basic ${basic}`, Accept: 'application/json' },
        });
        if (!tokenResponse.ok || tokenResponse.url !== tokenUrl.toString()) fail('GHCR exact-digest read check failed');
        const body = new Uint8Array(await tokenResponse.arrayBuffer());
        if (body.byteLength > 65536) fail('GHCR exact-digest read check failed');
        const payload = JSON.parse(new TextDecoder().decode(body));
        const bearer = payload?.token ?? payload?.access_token;
        if (typeof bearer !== 'string' || !/^[^\s\r\n]{1,16384}$/.test(bearer)) fail('GHCR exact-digest read check failed');
        response = await request({ ...headers, Authorization: `Bearer ${bearer}` });
      }
      if (!response.ok || response.url !== url || response.headers.get('docker-content-digest') !== image.digest) {
        fail('GHCR exact-digest read check failed');
      }
    }
  } catch {
    fail('GHCR exact-digest read check failed');
  }
  return 'PASS';
}

export function createOciPullScript(receipt, candidate, { username = 'akhripunov' } = {}) {
  if (!SHA40.test(candidate ?? '') || receipt?.candidateSha !== candidate || receipt?.schema !== 3) {
    fail('OCI pull candidate mismatch');
  }
  if (!GHCR_USERNAME.test(username)) fail('GHCR username is invalid');
  const lines = [
    'set -eu',
    'set +x',
    'docker_config=$(mktemp -d /tmp/gpoint-ghcr-auth.XXXXXX)',
    'chmod 700 "$docker_config"',
    'export DOCKER_CONFIG="$docker_config"',
    'trap \'docker logout ghcr.io >/dev/null 2>&1 || true; rm -rf "$docker_config"\' EXIT',
    `docker login ghcr.io --username '${username}' --password-stdin`,
  ];
  for (const [role, localName] of Object.entries({
    app: 'gpoint-runtime', agata: 'gpoint-agata-runtime', gsales: 'gpoint-g-sales-runtime',
  })) {
    const image = receipt.images?.[role];
    const expectedRepository = IMAGE_REPOSITORIES[role];
    if (!image || image.repository !== expectedRepository || !DIGEST.test(image.digest ?? '') ||
        !DIGEST.test(image.configDigest ?? '') || image.sourceSha !== candidate || image.platform !== 'linux/amd64') {
      fail(`OCI pull image proof invalid: ${role}`);
    }
    const reference = `${expectedRepository}@${image.digest}`;
    const target = `${localName}:${candidate}`;
    lines.push(`docker pull '${reference}'`);
    lines.push(`test "$(docker image inspect '${reference}' --format '{{.Os}}/{{.Architecture}}')" = 'linux/amd64'`);
    lines.push(`test "$(docker image inspect '${reference}' --format '{{.Id}}')" = '${image.configDigest}'`);
    lines.push(`test "$(docker image inspect '${reference}' --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = '${candidate}'`);
    lines.push(`test "$(docker image inspect '${reference}' --format '{{index .Config.Labels "org.opencontainers.image.source"}}')" = 'https://github.com/akhripunov/gpoint'`);
    lines.push(`docker image inspect '${reference}' --format '{{range .RepoDigests}}{{println .}}{{end}}' | grep -Fx '${reference}'`);
    lines.push(`docker image tag '${reference}' '${target}'`);
    lines.push(`test "$(docker image inspect '${target}' --format '{{.Id}}')" = '${image.configDigest}'`);
  }
  lines.push('echo GPOINT_GHCR_IMAGES_VERIFIED=YES');
  return lines.join('\n');
}

export function createOciVerifyScript(receipt, candidate) {
  if (!SHA40.test(candidate ?? '') || receipt?.candidateSha !== candidate || receipt?.schema !== 3) {
    fail('OCI verification candidate mismatch');
  }
  const lines = ['set -eu', 'set +x'];
  for (const [role, localName] of Object.entries({
    app: 'gpoint-runtime', agata: 'gpoint-agata-runtime', gsales: 'gpoint-g-sales-runtime',
  })) {
    const image = receipt.images?.[role];
    const expectedRepository = IMAGE_REPOSITORIES[role];
    if (!image || image.repository !== expectedRepository || !DIGEST.test(image.digest ?? '') ||
        !DIGEST.test(image.configDigest ?? '') || image.sourceSha !== candidate || image.platform !== 'linux/amd64') {
      fail(`OCI verification image proof invalid: ${role}`);
    }
    const reference = `${expectedRepository}@${image.digest}`;
    const target = `${localName}:${candidate}`;
    lines.push(`test "$(docker image inspect '${reference}' --format '{{.Id}}')" = '${image.configDigest}'`);
    lines.push(`test "$(docker image inspect '${target}' --format '{{.Id}}')" = '${image.configDigest}'`);
    lines.push(`test "$(docker image inspect '${target}' --format '{{.Os}}/{{.Architecture}}')" = 'linux/amd64'`);
    lines.push(`test "$(docker image inspect '${target}' --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = '${candidate}'`);
    lines.push(`test "$(docker image inspect '${target}' --format '{{index .Config.Labels "org.opencontainers.image.source"}}')" = 'https://github.com/akhripunov/gpoint'`);
    lines.push(`docker image inspect '${reference}' --format '{{range .RepoDigests}}{{println .}}{{end}}' | grep -Fx '${reference}'`);
  }
  lines.push('echo GPOINT_GHCR_POST_SWITCH_PARITY=YES');
  return lines.join('\n');
}

export { IMAGE_REPOSITORIES };
