import { execFileSync } from 'node:child_process';
import { homedir } from 'node:os';
import { join, normalize } from 'node:path';
import { statSync } from 'node:fs';

const PINNED_UPSTREAM = 'd1a140083321687212fb7b17c09cc0c3c9639f5e';
const PINNED_SOURCE = 'https://github.com/etherfunlab/eros-engine';

export function validateDockerEndpoint(endpoint, {
  home = homedir(),
  githubActions = false,
  dockerContext,
  runnerTemp,
  runtimeBasedir,
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
  const expectedRuntime = runnerTemp ? join(runnerTemp, 'gpoint-setup-docker') : null;
  const runnerSocket = githubActions
    && dockerContext === 'gpoint-setup-docker'
    && expectedRuntime !== null
    && runtimeBasedir === expectedRuntime
    && socket.startsWith(`${expectedRuntime}/`)
    && /^run-[a-f0-9]{8}\/docker\.sock$/i.test(socket.slice(expectedRuntime.length + 1));
  if (!allowed.has(socket) && !runnerSocket) throw new Error('unapproved_docker_socket');
  if (!socketExists(socket)) throw new Error('docker_socket_unavailable');
  return socket;
}

export function validateErosImageInspection(reference, metadata) {
  const stockSourceTag = /^gpoint-eros:stock-([a-f0-9]{40})$/i.exec(reference ?? '');
  const pilotCandidateTag = /^gpoint-eros-pilot:([a-f0-9]{40})$/i.exec(reference ?? '');
  if (!stockSourceTag && !pilotCandidateTag) throw new Error('stock_eros_image_tag_required');
  if (!metadata || !/^sha256:[0-9a-f]{64}$/.test(metadata.id ?? '')) {
    throw new Error('immutable_image_id_required');
  }
  if (metadata.os !== 'linux' || !['amd64', 'arm64'].includes(metadata.architecture)) throw new Error('eros_platform_mismatch');
  if (metadata.source !== PINNED_SOURCE || metadata.upstreamRevision !== PINNED_UPSTREAM) {
    throw new Error('eros_upstream_provenance_mismatch');
  }
  if (metadata.patchSha256 !== 'none') throw new Error('eros_downstream_patch_forbidden');
  if (!/^[0-9a-f]{40}$/.test(metadata.buildRevision ?? '')) {
    throw new Error('eros_build_revision_missing');
  }
  if (stockSourceTag && stockSourceTag[1] !== metadata.upstreamRevision) {
    throw new Error('eros_upstream_tag_mismatch');
  }
  if (pilotCandidateTag && pilotCandidateTag[1] !== metadata.buildRevision) {
    throw new Error('eros_build_revision_mismatch');
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
  validateDockerEndpoint(endpoint, {
    githubActions: process.env.GITHUB_ACTIONS === 'true',
    dockerContext: selected,
    runnerTemp: process.env.RUNNER_TEMP,
    runtimeBasedir: process.env.GPOINT_DOCKER_RUNTIME_BASEDIR,
  });
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
