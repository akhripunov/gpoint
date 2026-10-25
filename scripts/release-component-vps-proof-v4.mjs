import { verifyVPSNativeImages } from './release-component-bundle-v4.mjs';

const SHA = /^[a-f0-9]{40}$/;
const IMAGE = /^sha256:[a-f0-9]{64}$/;
const roles = {
  app: { tag: 'gpoint-runtime', containers: ['compose-api-1', 'compose-bot-1', 'compose-worker-1'] },
  agata: { tag: 'gpoint-agata-runtime', containers: ['compose-agata-api-1', 'compose-agata-memory-worker-1', 'compose-gsales-agata-api-1', 'compose-gsales-agata-memory-worker-1'] },
  gsales: { tag: 'gpoint-g-sales-runtime', containers: ['compose-gsales-api-1'] },
};

// Output contains only immutable image identities and container IDs. Secrets,
// environment variables and tenant/user content are not printed.
export function buildReadOnlyVPSProofCommand(inventory) {
  for (const role of Object.keys(roles)) {
    const r = inventory?.[role];
    if (!SHA.test(r?.sourceSha ?? '') || !IMAGE.test(r?.imageId ?? '') ||
        (r.nativeId !== undefined && (!IMAGE.test(r.nativeId) || r.nativeId === r.imageId)) ||
        r?.platform !== 'linux/amd64')
      throw new Error('untrusted native image verification input');
  }
  const steps = ['set -eu'];
  for (const [role, info] of Object.entries(roles)) {
    const rev = inventory[role].sourceSha;
    const ref = info.tag + ':' + rev;
    steps.push('docker image inspect ' + ref +
      ' --format ' +
      "'COMPONENT_V4_IMAGE=" + role +
      "|{{.Id}}|{{.Os}}|{{.Architecture}}|{{index .Config.Labels \"org.opencontainers.image.revision\"}}'");
    for (const container of info.containers) {
      steps.push('docker inspect ' + container + ' --format ' +
        "'COMPONENT_V4_CONTAINER=" + role + '|' + container + "|{{.Image}}|{{.State.Status}}|{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.State.ExitCode}}|{{.State.OOMKilled}}'");
    }
  }
  return steps.join('; ');
}

export function parseReadOnlyVPSProof(output, inventory) {
  if (typeof output !== 'string') throw new Error('no VPS proof');
  const result = {}, checkedContainers = new Set(), gsalesModes = new Set();
  // Legacy V3/V4 imageId is native. V5 keeps OCI config and containerd native
  // digests distinct; the latter is verified against the attested rehearsal.
  const expectedNative = role => inventory?.[role]?.nativeId ?? inventory?.[role]?.imageId;
  for (const line of output.split(/\r?\n/)) {
    if (line.startsWith('COMPONENT_V4_IMAGE=')) {
      const [role, id, os, arch, revision, ...extra] = line.slice(19).split('|');
      if (extra.length || !roles[role] || result[role] ||
          !IMAGE.test(id) || os !== 'linux' || arch !== 'amd64' ||
          revision !== inventory?.[role]?.sourceSha ||
          id !== expectedNative(role))
        throw new Error('unexpected native Docker role image proof');
      result[role] = { nativeImageId: id, revisionLabel: revision,
        os, architecture: arch,
        name: roles[role].tag + ':' + inventory[role].sourceSha };
    } else if (line.startsWith('COMPONENT_V4_CONTAINER=')) {
      const [role, container, id, status, running, health, exitCode, oomKilled, ...extra] = line.slice(23).split('|');
      const key = role + ':' + container;
      const gsalesConsumer = container?.startsWith('compose-gsales-');
      const runtimeValid = gsalesConsumer
        ? (status === 'running' && running === 'true' && health === 'healthy') ||
          (status === 'exited' && running === 'false' && ['healthy', 'none'].includes(health) && exitCode === '0' && oomKilled === 'false')
        : status === 'running' && running === 'true' && health === 'healthy';
      if (extra.length || !roles[role]?.containers.includes(container) ||
          checkedContainers.has(key) || id !== expectedNative(role) ||
          !runtimeValid)
        throw new Error('unexpected native Docker container proof');
      if (gsalesConsumer) gsalesModes.add(status);
      checkedContainers.add(key);
    }
  }
  if (Object.keys(result).length !== Object.keys(roles).length ||
      checkedContainers.size !== Object.values(roles).reduce((n, x) => n + x.containers.length, 0) ||
      gsalesModes.size !== 1)
    throw new Error('missing VPS image or container proof');
  return result;
}

export function attestCurrentProductionOnVPS(manifest, output, { candidate, baseline }) {
  const result = parseReadOnlyVPSProof(output, manifest.images);
  return verifyVPSNativeImages(manifest, result, { candidate, baseline });
}
