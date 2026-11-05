import assert from 'node:assert/strict';
import test from 'node:test';
import { isExpectedErosWorkerJobIdentity } from '../src/eros-worker-contract.js';

const tenantId = '7508f36f-774a-48aa-8c6b-42f6f160415d';
const expectedSystemIdentity = {
  tenantId,
  userId: '00000000-0000-4000-8000-000000000001',
  creatorId: '00000000-0000-4000-8000-000000000004',
  sessionId: '00000000-0000-4000-8000-000000000002',
  instanceId: '00000000-0000-4000-8000-000000000003',
  requestId: '61139601-c953-59cd-b2b2-d47ca203cae6',
};

test('accepts only the deterministic tenant-bound Eros system task identity', () => {
  assert.equal(isExpectedErosWorkerJobIdentity({
    job_type: 'SYSTEM_COMPLETION',
    tenant_id: tenantId,
    user_id: expectedSystemIdentity.userId,
    creator_id: expectedSystemIdentity.creatorId,
    session_id: expectedSystemIdentity.sessionId,
    instance_id: expectedSystemIdentity.instanceId,
    request_id: expectedSystemIdentity.requestId,
  }, tenantId, {
    userId: '10000000-0000-4000-8000-000000000001', creatorId: '20000000-0000-4000-8000-000000000002',
  }, expectedSystemIdentity), true);
});

test('rejects a system task if its tenant, job type, or deterministic identity differs', () => {
  const row = {
    job_type: 'SYSTEM_COMPLETION',
    tenant_id: tenantId,
    user_id: expectedSystemIdentity.userId,
    creator_id: expectedSystemIdentity.creatorId,
    session_id: expectedSystemIdentity.sessionId,
    instance_id: expectedSystemIdentity.instanceId,
    request_id: expectedSystemIdentity.requestId,
  };
  for (const mutated of [
    { ...row, tenant_id: '8508f36f-774a-48aa-8c6b-42f6f160415d' },
    { ...row, job_type: 'CHAT_COMPLETION' },
    { ...row, user_id: '10000000-0000-4000-8000-000000000001' },
    { ...row, creator_id: '20000000-0000-4000-8000-000000000002' },
    { ...row, session_id: '10000000-0000-4000-8000-000000000001' },
    { ...row, instance_id: '20000000-0000-4000-8000-000000000002' },
    { ...row, request_id: '71139601-c953-59cd-b2b2-d47ca203cae6' },
  ]) {
    assert.equal(isExpectedErosWorkerJobIdentity(mutated, tenantId, {
      userId: '10000000-0000-4000-8000-000000000001', creatorId: '20000000-0000-4000-8000-000000000002',
    }, expectedSystemIdentity), false);
  }
});

test('does not let a system identity bypass the exact pilot chat-user mapping', () => {
  assert.equal(isExpectedErosWorkerJobIdentity({
    job_type: 'CHAT_COMPLETION',
    tenant_id: tenantId,
    user_id: expectedSystemIdentity.userId,
    creator_id: expectedSystemIdentity.creatorId,
    session_id: expectedSystemIdentity.sessionId,
    instance_id: expectedSystemIdentity.instanceId,
    request_id: expectedSystemIdentity.requestId,
  }, tenantId, {
    userId: '10000000-0000-4000-8000-000000000001', creatorId: '20000000-0000-4000-8000-000000000002',
  }, expectedSystemIdentity), false);
});

test('accepts a chat job only for the configured synthetic pilot pair and exact signed identity', () => {
  const chatIdentity = {
    tenantId,
    userId: 'c0ed4418-d76d-5ccc-8b1b-4eeeaed78651',
    creatorId: 'd552a185-1e1a-54f2-a668-da366e82b4f4',
    sessionId: '30000000-0000-4000-8000-000000000003',
    instanceId: '40000000-0000-4000-8000-000000000004',
    requestId: '50000000-0000-4000-8000-000000000005',
  };
  assert.equal(isExpectedErosWorkerJobIdentity({
    job_type: 'CHAT_COMPLETION',
    tenant_id: tenantId,
    user_id: 'c0ed4418-d76d-5ccc-8b1b-4eeeaed78651',
    creator_id: 'd552a185-1e1a-54f2-a668-da366e82b4f4',
    session_id: '30000000-0000-4000-8000-000000000003',
    instance_id: '40000000-0000-4000-8000-000000000004',
    request_id: '50000000-0000-4000-8000-000000000005',
  }, tenantId, {
    userId: 'c0ed4418-d76d-5ccc-8b1b-4eeeaed78651', creatorId: 'd552a185-1e1a-54f2-a668-da366e82b4f4',
  }, null, { chatIdentity }), true);
});
