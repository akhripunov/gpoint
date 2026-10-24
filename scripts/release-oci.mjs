import {retryNetworkRead} from './release-network-read.mjs';

const SHA40 = /^[a-f0-9]{40}$/;
const DIGEST = /^sha256:[a-f0-9]{64}$/;
const GHCR_USERNAME = /^[A-Za-z0-9-]{1,39}(?:\[bot\])?$/;
const IMAGE_REPOSITORIES = Object.freeze({
  app: 'ghcr.io/akhripunov/gpoint-runtime',
  agata: 'ghcr.io/akhripunov/gpoint-agata-runtime',
  gsales: 'ghcr.io/akhripunov/gpoint-g-sales-runtime',
});
const EROS_PILOT_REPOSITORY = 'ghcr.io/akhripunov/gpoint-eros-pilot';
const EROS_UPSTREAM_SHA = 'd1a140083321687212fb7b17c09cc0c3c9639f5e';

function fail(message) { throw new Error(message); }

const OCI_MANIFEST_CONFIG_PYTHON = [
  'import hashlib,json,re,sys; p=sys.argv[1]; expected=sys.argv[2]; raw=open(p,"rb").read(); actual="sha256:"+hashlib.sha256(raw).hexdigest(); assert actual==expected,"manifest digest mismatch"; m=json.loads(raw); ds=m.get("manifests"); matches=[d for d in (ds if isinstance(ds,list) else []) if isinstance(d,dict) and d.get("platform",{}).get("os")=="linux" and d.get("platform",{}).get("architecture")=="amd64"]; d=("manifest:"+matches[0].get("digest","") if "manifests" in m and len(matches)==1 else ((m.get("config") or {}).get("digest","") if "manifests" not in m else "")); assert re.fullmatch(r"(?:manifest:)?sha256:[a-f0-9]{64}",d); print(d)',
].join('');

const OCI_CHILD_CONFIG_PYTHON = [
  'import hashlib,json,re,sys; p=sys.argv[1]; expected=sys.argv[2]; raw=open(p,"rb").read(); actual="sha256:"+hashlib.sha256(raw).hexdigest(); assert actual==expected,"child manifest digest mismatch"; m=json.loads(raw); d=(m.get("config") or {}).get("digest",""); assert "manifests" not in m and re.fullmatch(r"sha256:[a-f0-9]{64}",d); print(d)',
].join('');

function appendOciManifestConfigCheck(lines, image, reference, role) {
  const repository = image.repository;
  const manifestFile = `$oci_tempdir/${role}-manifest.json`;
  const childFile = `$oci_tempdir/${role}-child-manifest.json`;
  lines.push(`docker buildx imagetools inspect --raw '${reference}' > "${manifestFile}" 2>/dev/null || { echo 'GHCR_MANIFEST_READ_FAILED role=${role}'; exit 1; }`);
  lines.push(`manifest_config=$(python3 -c '${OCI_MANIFEST_CONFIG_PYTHON}' "${manifestFile}" '${image.digest}' 2>/dev/null) || { echo 'GHCR_MANIFEST_DIGEST_OR_CONTENT_MISMATCH role=${role}'; exit 1; }`);
  lines.push('case "$manifest_config" in');
  lines.push('  manifest:*)');
  lines.push(`    child_digest="\${manifest_config#manifest:}"`);
  lines.push(`    docker buildx imagetools inspect --raw '${repository}@'"$child_digest" > "${childFile}" 2>/dev/null || { echo 'GHCR_CHILD_MANIFEST_READ_FAILED role=${role}'; exit 1; }`);
  lines.push(`    manifest_config=$(python3 -c '${OCI_CHILD_CONFIG_PYTHON}' "${childFile}" "$child_digest" 2>/dev/null) || { echo 'GHCR_CHILD_MANIFEST_DIGEST_OR_CONTENT_MISMATCH role=${role}'; exit 1; }`);
  lines.push('    ;;');
  lines.push('esac');
  lines.push(`test "$manifest_config" = '${image.configDigest}' || { echo 'GHCR_CONFIG_DIGEST_MISMATCH role=${role}'; exit 1; }`);
}

function appendDockerImageIdentityCheck(lines, reference, image, role, label) {
  lines.push(`image_id=$(docker image inspect '${reference}' --format '{{.Id}}' 2>/dev/null) || { echo 'GHCR_IMAGE_INSPECT_FAILED role=${role} point=${label}'; exit 1; }`);
  lines.push(`case "$image_id" in '${image.configDigest}'|'${image.digest}') ;; *) echo 'GHCR_IMAGE_ID_MISMATCH role=${role} point=${label}'; exit 1 ;; esac`);
}

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
  let pilotImages;
  if (receipt.pilotImage !== undefined || receipt.pilotImages !== undefined) {
    if (receipt.gates?.erosPilotImage !== 'PASS' || receipt.gates?.erosPilotGhcrReadback !== 'PASS')
      fail('Eros pilot image is missing its Prepare or GHCR readback gate');
    if (receipt.gates?.erosStockMigrationApiE2E !== 'PASS')
      fail('Eros image receipt is missing the stock Eros migration/API E2E gate');
    const pilot = receipt.pilotImages?.eros;
    const preparedPilot = receipt.pilotImage;
    if (!pilot || Object.keys(receipt.pilotImages).join(',') !== 'eros' ||
        pilot.repository !== EROS_PILOT_REPOSITORY || !DIGEST.test(pilot.digest ?? '') ||
        !DIGEST.test(pilot.configDigest ?? '') || pilot.sourceSha !== candidate ||
        pilot.upstreamSha !== EROS_UPSTREAM_SHA || pilot.patchSetSha256 !== 'none' ||
        pilot.platform !== 'linux/amd64' || preparedPilot?.id !== pilot.configDigest ||
        preparedPilot?.sourceSha !== candidate || preparedPilot?.upstreamSha !== EROS_UPSTREAM_SHA ||
        preparedPilot?.patchSetSha256 !== 'none' || preparedPilot?.platform !== 'linux/amd64')
      fail('Eros pilot image proof is invalid');
    pilotImages = { eros: { ...pilot, reference: `${EROS_PILOT_REPOSITORY}@${pilot.digest}` } };
  }
  return { ...receipt, images, ...(pilotImages ? { pilotImages } : {}) };
}

export async function verifyOciReadAccess(receipt, username, token, fetcher = fetch, retryOptions = {}) {
  if (!GHCR_USERNAME.test(username ?? '') || typeof token !== 'string' || !token.trim()) {
    fail('GHCR exact-digest read check failed');
  }
  if (receipt?.schema !== 3 || receipt?.transport !== 'ghcr-oci') fail('GHCR exact-digest read check failed');
  const repositories = { ...IMAGE_REPOSITORIES };
  if (receipt.pilotImages?.eros) repositories.eros = EROS_PILOT_REPOSITORY;
  for (const [role, expectedRepository] of Object.entries(repositories)) {
    let phase = 'manifest';
    try {
      await retryNetworkRead(`ghcr_${role}_read`, async remainingMs => {
        phase = 'manifest';
        const signal = AbortSignal.timeout(Math.min(30_000, remainingMs));
        const image = role === 'eros' ? receipt.pilotImages?.eros : receipt.images?.[role];
        if (!image || image.repository !== expectedRepository || !DIGEST.test(image.digest ?? '')) {
          fail('GHCR exact-digest read check failed');
        }
        const url = `https://ghcr.io/v2/${expectedRepository.slice('ghcr.io/'.length)}/manifests/${image.digest}`;
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
          headers: requestHeaders, signal,
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
          phase = 'token';
          const tokenResponse = await fetcher(tokenUrl.toString(), {
            method: 'GET', redirect: 'error', signal,
            headers: { Authorization: `Basic ${basic}`, Accept: 'application/json' },
          });
          if (!tokenResponse.ok) throw Object.assign(new Error('GHCR token HTTP failure'), {status: tokenResponse.status});
          if (tokenResponse.url !== tokenUrl.toString()) fail('GHCR token endpoint mismatch');
          phase = 'token_body';
          const body = new Uint8Array(await tokenResponse.arrayBuffer());
          if (body.byteLength > 65536) fail('GHCR exact-digest read check failed');
          const payload = JSON.parse(new TextDecoder().decode(body));
          const bearer = payload?.token ?? payload?.access_token;
          if (typeof bearer !== 'string' || !/^[^\s\r\n]{1,16384}$/.test(bearer)) fail('GHCR exact-digest read check failed');
          phase = 'bearer_manifest';
          response = await request({ ...headers, Authorization: `Bearer ${bearer}` });
        }
        if (!response.ok) throw Object.assign(new Error('GHCR manifest HTTP failure'), {status: response.status});
        if (response.url !== url) fail('GHCR manifest endpoint mismatch');
        if (response.headers.get('docker-content-digest') !== image.digest) {
          throw Object.assign(new Error('GHCR digest mismatch'), {code: 'DIGEST_MISMATCH'});
        }
      }, retryOptions);
    } catch (error) {
      // Never expose fetch messages, URLs, headers, token bodies or causes.
      const allowedCodes = new Set(['UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_HEADERS_TIMEOUT',
        'UND_ERR_BODY_TIMEOUT', 'UND_ERR_SOCKET', 'ETIMEDOUT', 'ECONNRESET', 'ECONNABORTED',
        'EPIPE', 'ERR_STREAM_PREMATURE_CLOSE', 'ERR_SOCKET_CONNECTION_TIMEOUT',
        'ERR_SSL_UNEXPECTED_EOF_WHILE_READING', 'ERR_TLS_HANDSHAKE_TIMEOUT',
        'CERT_HAS_EXPIRED', 'DEPTH_ZERO_SELF_SIGNED_CERT', 'UNABLE_TO_VERIFY_LEAF_SIGNATURE',
        'ERR_TLS_CERT_ALTNAME_INVALID', 'CERT_SIGNATURE_FAILURE',
        'ERR_TLS_CERT_SIGNATURE_ALGORITHM_UNSUPPORTED', 'DIGEST_MISMATCH']);
      let code = 'READ_OR_VALIDATION_FAILURE';
      if (Number.isInteger(error?.status) && error.status >= 400 && error.status <= 599) code = `HTTP_${error.status}`;
      else if (error?.name === 'NetworkReadBudgetError') code = 'NETWORK_BUDGET_EXHAUSTED';
      else if (error?.name === 'TimeoutError') code = 'REQUEST_TIMEOUT';
      else if (error instanceof SyntaxError) code = 'INVALID_JSON';
      else for (const value of [error?.code, error?.cause?.code]) {
        if (allowedCodes.has(value)) { code = value; break; }
      }
      fail(`GHCR exact-digest read check failed role=${role} phase=${phase} code=${code}`);
    }
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
    'oci_tempdir=$(mktemp -d /tmp/gpoint-ghcr-manifests.XXXXXX)',
    'chmod 700 "$oci_tempdir"',
    'export DOCKER_CONFIG="$docker_config"',
    'trap \'docker logout ghcr.io >/dev/null 2>&1 || true; rm -rf "$docker_config" "$oci_tempdir"\' EXIT',
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
    lines.push(`docker pull '${reference}' >/dev/null 2>&1 || { echo 'GHCR_PULL_FAILED role=${role}'; exit 1; }`);
    lines.push(`test "$(docker image inspect '${reference}' --format '{{.Os}}/{{.Architecture}}' 2>/dev/null)" = 'linux/amd64' || { echo 'GHCR_PLATFORM_MISMATCH role=${role}'; exit 1; }`);
    appendOciManifestConfigCheck(lines, image, reference, role);
    appendDockerImageIdentityCheck(lines, reference, image, role, 'pulled');
    lines.push(`test "$(docker image inspect '${reference}' --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null)" = '${candidate}' || { echo 'GHCR_REVISION_LABEL_MISMATCH role=${role}'; exit 1; }`);
    lines.push(`test "$(docker image inspect '${reference}' --format '{{index .Config.Labels "org.opencontainers.image.source"}}' 2>/dev/null)" = 'https://github.com/akhripunov/gpoint' || { echo 'GHCR_SOURCE_LABEL_MISMATCH role=${role}'; exit 1; }`);
    lines.push(`if ! docker image inspect '${reference}' --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null | grep -Fxq '${reference}'; then echo 'GHCR_MANIFEST_DIGEST_MISMATCH role=${role}'; exit 1; fi`);
    lines.push(`docker image tag '${reference}' '${target}'`);
    appendDockerImageIdentityCheck(lines, target, image, role, 'tagged');
  }
  if (receipt.pilotImages?.eros) {
    const image = receipt.pilotImages.eros;
    const reference = `${EROS_PILOT_REPOSITORY}@${image.digest}`;
    if (receipt.gates?.erosPilotImage !== 'PASS' || receipt.gates?.erosPilotGhcrReadback !== 'PASS' ||
        receipt.gates?.erosStockMigrationApiE2E !== 'PASS' || image.repository !== EROS_PILOT_REPOSITORY ||
        image.sourceSha !== candidate || image.platform !== 'linux/amd64' ||
        !DIGEST.test(image.digest ?? '') || !DIGEST.test(image.configDigest ?? ''))
      fail('permanent Eros OCI proof is invalid');
    lines.push(`docker pull '${reference}' >/dev/null 2>&1 || { echo 'GHCR_PULL_FAILED role=eros'; exit 1; }`);
    lines.push(`test "$(docker image inspect '${reference}' --format '{{.Os}}/{{.Architecture}}' 2>/dev/null)" = 'linux/amd64' || { echo 'GHCR_PLATFORM_MISMATCH role=eros'; exit 1; }`);
    appendOciManifestConfigCheck(lines, image, reference, 'eros');
    appendDockerImageIdentityCheck(lines, reference, image, 'eros', 'pulled');
    lines.push(`test "$(docker image inspect '${reference}' --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null)" = '${candidate}' || { echo 'GHCR_REVISION_LABEL_MISMATCH role=eros'; exit 1; }`);
    lines.push(`if ! docker image inspect '${reference}' --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null | grep -Fxq '${reference}'; then echo 'GHCR_MANIFEST_DIGEST_MISMATCH role=eros'; exit 1; fi`);
  }
  lines.push('echo GPOINT_GHCR_IMAGES_VERIFIED=YES');
  return lines.join('\n');
}

export function createErosPilotOciPullScript(receipt, candidate, { username = 'akhripunov' } = {}) {
  const image = receipt?.pilotImages?.eros;
  const reference = image && `${EROS_PILOT_REPOSITORY}@${image.digest}`;
  const app = receipt?.images?.app;
  const appReference = app && `${IMAGE_REPOSITORIES.app}@${app.digest}`;
  if (!SHA40.test(candidate ?? '') || receipt?.candidateSha !== candidate || receipt?.schema !== 3 ||
      receipt?.gates?.erosPilotImage !== 'PASS' || receipt?.gates?.erosPilotGhcrReadback !== 'PASS' ||
      !image || image.repository !== EROS_PILOT_REPOSITORY || image.sourceSha !== candidate ||
      image.upstreamSha !== EROS_UPSTREAM_SHA || image.patchSetSha256 !== 'none' ||
      image.platform !== 'linux/amd64' || !DIGEST.test(image.digest ?? '') ||
      !DIGEST.test(image.configDigest ?? '') || !GHCR_USERNAME.test(username) ||
      !app || app.repository !== IMAGE_REPOSITORIES.app || app.sourceSha !== candidate ||
      app.platform !== 'linux/amd64' || !DIGEST.test(app.digest ?? '') || !DIGEST.test(app.configDigest ?? '')) {
    fail('Eros Pilot OCI pull proof is invalid');
  }
  const lines = [
    'set -eu', 'set +x',
    'docker_config=$(mktemp -d /tmp/gpoint-eros-pilot-ghcr-auth.XXXXXX)',
    'chmod 700 "$docker_config"',
    'oci_tempdir=$(mktemp -d /tmp/gpoint-eros-pilot-ghcr-manifests.XXXXXX)',
    'chmod 700 "$oci_tempdir"',
    'export DOCKER_CONFIG="$docker_config"',
    'trap \'docker logout ghcr.io >/dev/null 2>&1 || true; rm -rf "$docker_config" "$oci_tempdir"\' EXIT',
    `docker login ghcr.io --username '${username}' --password-stdin`,
    `docker pull '${appReference}' >/dev/null 2>&1 || { echo 'GHCR_PULL_FAILED role=app'; exit 1; }`,
    `test "$(docker image inspect '${appReference}' --format '{{.Os}}/{{.Architecture}}' 2>/dev/null)" = 'linux/amd64' || { echo 'GHCR_PLATFORM_MISMATCH role=app'; exit 1; }`,
  ];
  appendOciManifestConfigCheck(lines, app, appReference, 'pilot-app');
  appendDockerImageIdentityCheck(lines, appReference, app, 'pilot-app', 'pulled');
  lines.push(`test "$(docker image inspect '${appReference}' --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null)" = '${candidate}' || { echo 'GPOINT_APP_REVISION_MISMATCH'; exit 1; }`);
  lines.push(`docker image tag '${appReference}' 'gpoint-runtime:${candidate}'`);
  appendDockerImageIdentityCheck(lines, `gpoint-runtime:${candidate}`, app, 'pilot-app', 'tagged');
  lines.push(
    `docker pull '${reference}' >/dev/null 2>&1 || { echo 'GHCR_PULL_FAILED role=eros'; exit 1; }`,
    `test "$(docker image inspect '${reference}' --format '{{.Os}}/{{.Architecture}}' 2>/dev/null)" = 'linux/amd64' || { echo 'GHCR_PLATFORM_MISMATCH role=eros'; exit 1; }`,
  );
  appendOciManifestConfigCheck(lines, image, reference, 'eros');
  appendDockerImageIdentityCheck(lines, reference, image, 'eros', 'pulled');
  lines.push(`test "$(docker image inspect '${reference}' --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null)" = '${EROS_UPSTREAM_SHA}' || { echo 'EROS_UPSTREAM_REVISION_MISMATCH'; exit 1; }`);
  lines.push(`test "$(docker image inspect '${reference}' --format '{{index .Config.Labels "org.opencontainers.image.source"}}' 2>/dev/null)" = 'https://github.com/etherfunlab/eros-engine' || { echo 'EROS_UPSTREAM_SOURCE_MISMATCH'; exit 1; }`);
  lines.push(`test "$(docker image inspect '${reference}' --format '{{index .Config.Labels "com.gpoint.integration.revision"}}' 2>/dev/null)" = '${candidate}' || { echo 'EROS_CANDIDATE_REVISION_MISMATCH'; exit 1; }`);
  lines.push(`test "$(docker image inspect '${reference}' --format '{{index .Config.Labels "com.gpoint.eros.patch-set.sha256"}}' 2>/dev/null)" = 'none' || { echo 'EROS_SOURCE_PATCH_DETECTED'; exit 1; }`);
  lines.push(`if ! docker image inspect '${reference}' --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null | grep -Fxq '${reference}'; then echo 'GHCR_MANIFEST_DIGEST_MISMATCH role=eros'; exit 1; fi`);
  lines.push('echo EROS_PILOT_GHCR_IMAGE_VERIFIED=YES GPOINT_PILOT_API_IMAGE_VERIFIED=YES');
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
    appendDockerImageIdentityCheck(lines, reference, image, role, 'remote-reference');
    appendDockerImageIdentityCheck(lines, target, image, role, 'tagged');
    lines.push(`test "$(docker image inspect '${target}' --format '{{.Os}}/{{.Architecture}}' 2>/dev/null)" = 'linux/amd64' || { echo 'GHCR_PLATFORM_MISMATCH role=${role}'; exit 1; }`);
    lines.push(`test "$(docker image inspect '${target}' --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null)" = '${candidate}' || { echo 'GHCR_REVISION_LABEL_MISMATCH role=${role}'; exit 1; }`);
    lines.push(`test "$(docker image inspect '${target}' --format '{{index .Config.Labels "org.opencontainers.image.source"}}' 2>/dev/null)" = 'https://github.com/akhripunov/gpoint' || { echo 'GHCR_SOURCE_LABEL_MISMATCH role=${role}'; exit 1; }`);
    lines.push(`if ! docker image inspect '${reference}' --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null | grep -Fxq '${reference}'; then echo 'GHCR_MANIFEST_DIGEST_MISMATCH role=${role}'; exit 1; fi`);
  }
  lines.push('echo GPOINT_GHCR_POST_SWITCH_PARITY=YES');
  return lines.join('\n');
}

export { IMAGE_REPOSITORIES };
