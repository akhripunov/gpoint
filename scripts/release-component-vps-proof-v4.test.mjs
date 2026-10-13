import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildReadOnlyVPSProofCommand, parseReadOnlyVPSProof } from './release-component-vps-proof-v4.mjs';

const sha = 'a'.repeat(40);
const images = {
  app: { imageId: 'sha256:' + '1'.repeat(64), sourceSha: sha, platform: 'linux/amd64' },
  agata: { imageId: 'sha256:' + '2'.repeat(64), sourceSha: sha, platform: 'linux/amd64' },
  gsales: { imageId: 'sha256:' + '3'.repeat(64), sourceSha: sha, platform: 'linux/amd64' },
};
const containers = {
  app: ['compose-api-1', 'compose-bot-1', 'compose-worker-1'],
  agata: ['compose-agata-api-1', 'compose-agata-memory-worker-1',
          'compose-gsales-agata-api-1', 'compose-gsales-agata-memory-worker-1'],
  gsales: ['compose-gsales-api-1'],
};
function facts() {
  const lines=[];
  for (const [role, data] of Object.entries(images)) {
    lines.push('COMPONENT_V4_IMAGE=' + [role,data.imageId,'linux','amd64',sha].join('|'));
    for (const name of containers[role])
      lines.push('COMPONENT_V4_CONTAINER=' + [role,name,data.imageId,'true'].join('|'));
  }
  return lines.join('\n');
}
test('VPS proof command is read-only and includes every AGATA consumer', () => {
  const command=buildReadOnlyVPSProofCommand(images);
  assert.match(command,/compose-gsales-agata-memory-worker-1/);
  assert.match(command,/docker image inspect/);
  assert.doesNotMatch(command,/\bdocker\s+(run|rm|stop|restart|tag|load|build)\b/);
});
test('native VPS facts are checked exactly', () => {
  const result=parseReadOnlyVPSProof(facts(), images);
  assert.equal(result.agata.nativeImageId, images.agata.imageId);
  assert.equal(Object.keys(result).length,3);
});
test('missing or mismatched Docker container fails closed', () => {
  assert.throws(() => parseReadOnlyVPSProof(facts().replace('COMPONENT_V4_CONTAINER=agata|compose-gsales-agata-api-1', 'OMITTED=agata|compose-gsales-agata-api-1'), images));
  assert.throws(() => parseReadOnlyVPSProof(facts().replace('compose-bot-1|' + images.app.imageId, 'compose-bot-1|sha256:' + '9'.repeat(64)),images));
  assert.throws(() => parseReadOnlyVPSProof(facts().replace('compose-gsales-api-1|' + images.gsales.imageId + '|true','compose-gsales-api-1|' + images.gsales.imageId + '|false'),images));
});
test('unknown duplicate or malformed proof fails closed', () => {
  assert.throws(() => parseReadOnlyVPSProof(facts()+'\nCOMPONENT_V4_IMAGE=app|bad|linux|amd64|'+sha,images));
  assert.throws(() => parseReadOnlyVPSProof(facts().replace('linux|amd64','darwin|amd64'),images));
});
