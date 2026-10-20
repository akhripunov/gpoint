import { execFileSync } from 'node:child_process';
import { homedir } from 'node:os';
import { join, normalize } from 'node:path';
import { statSync } from 'node:fs';

export const PINNED_EROS_REFERENCE = 'gpoint-eros:d1a140083321687212fb7b17c09cc0c3c9639f5e-d738ff7b7be7-linux-amd64';
const PINNED_UPSTREAM = 'd1a140083321687212fb7b17c09cc0c3c9639f5e';
const PINNED_PATCHSET = 'd738ff7b7be77e961e126fa49c36fc84f23c9006b3caae51ec24974882626414';
const PINNED_SOURCE = 'https://github.com/akhripunov/gpoint';

export function validateDockerEndpoint(endpoint, {
  home = homedir(),
  socketExists = (path) => {
    try { return statSync(path).isSocket(); } catch { return false; }
  },
} = {}) {
  if (typeof endpoint !== 'string' || !endpoint.startsWith('unix:///')) {
    throw new Error('local_unix_socket_required');
  }
  const socket = normalize(endpoint.slice('unix://'.length));
  const allowed = new Set([
    join(home, '.docker/run/docker.sock'),
    join(home, '.docker/desktop/docker.sock'),
    '/var/run/docker.sock',
  ]);
  if (!allowed.has(socket)) throw new Error('unapproved_docker_socket');
  if (!socketExists(socket)) throw new Error('docker_socket_unavailable');
  return socket;
}

export function validateErosImageInspection(reference, metadata) {
  if (reference !== PINNED_EROS_REFERENCE) throw new Error('pinned_eros_reference_required');
  if (!metadata || !/^sha256:[0-9a-f]{64}$/.test(metadata.id ?? '')) {
    throw new Error('immutable_image_id_required');
  }
  if (metadata.os !== 'linux' || metadata.architecture !== 'amd64') {
    throw new Error('eros_platform_mismatch');
  }
  if (metadata.source !== PINNED_SOURCE || metadata.upstreamRevision !== PINNED_UPSTREAM) {
    throw new Error('eros_upstream_provenance_mismatch');
  }
  if (metadata.patchSha256 !== PINNED_PATCHSET) throw new Error('eros_patch_provenance_mismatch');
  if (!/^[0-9a-f]{40}$/.test(metadata.buildRevision ?? '')) {
    throw new Error('eros_build_revision_missing');
  }
  return metadata.id;
}

function cleanDockerEnvironment(context) {
  const env = { ...process.env, DOCKER_CONTEXT: context };
  delete env.DOCKER_HOST;
  delete env.DOCKER_TLS_VERIFY;
  delete env.DOCKER_CERT_PATH;
  return env;
}

function getLocalContext() {
  if (process.env.DOCKER_HOST) throw new Error('docker_host_override_not_allowed');
  const env = { ...process.env };
  delete env.DOCKER_HOST;
  delete env.DOCKER_TLS_VERIFY;
  delete env.DOCKER_CERT_PATH;
  const selected = process.env.DOCKER_CONTEXT
    ?? execFileSync('docker', ['context', 'show'], { env, encoding: 'utf8' }).trim();
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$/.test(selected)) throw new Error('docker_context_invalid');
  const endpoint = execFileSync('docker', [
    'context', 'inspect', selected,
    '--format', '{{(index .Endpoints "docker").Host}}',
  ], { env: cleanDockerEnvironment(selected), encoding: 'utf8' }).trim();
  validateDockerEndpoint(endpoint);
  return selected;
}

function inspectImage(reference) {
  const context = getLocalContext();
  const format = [
    '{{.Id}}', '{{.Os}}', '{{.Architecture}}',
    '{{index .Config.Labels "org.opencontainers.image.source"}}',
    '{{index .Config.Labels "com.gpoint.eros.upstream-revision"}}',
    '{{index .Config.Labels "com.gpoint.eros.patch-set.sha256"}}',
    '{{index .Config.Labels "org.opencontainers.image.revision"}}',
  ].join('\t');
  const output = execFileSync('docker', ['image', 'inspect', reference, '--format', format], {
    env: cleanDockerEnvironment(context),
    encoding: 'utf8',
  }).trim();
  const [id, os, architecture, source, upstreamRevision, patchSha256, buildRevision] = output.split('\t');
  return validateErosImageInspection(reference, {
    id, os, architecture, source, upstreamRevision, patchSha256, buildRevision,
  });
}

if (import.meta.url === `file://${process.argv[1]}`) {
  try {
    if (process.argv[2] === 'context') console.log(getLocalContext());
    else if (process.argv[2] === 'image' && process.argv[3]) console.log(inspectImage(process.argv[3]));
    else throw new Error('usage: local-test-policy.mjs context | image <pinned-reference>');
  } catch (error) {
    console.error(error instanceof Error ? error.message : 'local_test_policy_failed');
    process.exitCode = 1;
  }
}
