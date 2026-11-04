import assert from 'node:assert/strict';
import test from 'node:test';
import { HttpException } from '@nestjs/common';
import { GpointWorkerGatewayController, assertGpointWorkerAuth } from '../src/gpoint-worker-gateway.js';

const ids = {
  user: '10000000-0000-4000-8000-000000000001',
  creator: '20000000-0000-4000-8000-000000000002',
  tenant: '30000000-0000-4000-8000-000000000003',
};

test('GPoint Product Worker gateway accepts only its dedicated bearer token', () => {
  const previous = process.env.GPOINT_EROS_WORKER_TOKEN;
  process.env.GPOINT_EROS_WORKER_TOKEN = 'dedicated-test-token';
  try {
    assert.doesNotThrow(() => assertGpointWorkerAuth({ authorization: 'Bearer dedicated-test-token' }));
    assert.throws(() => assertGpointWorkerAuth({}), { status: 401 });
    assert.throws(() => assertGpointWorkerAuth({ authorization: 'Bearer wrong-token' }), { status: 401 });
  } finally {
    if (previous === undefined) delete process.env.GPOINT_EROS_WORKER_TOKEN;
    else process.env.GPOINT_EROS_WORKER_TOKEN = previous;
  }
});

test('GPoint worker health fails closed when runtime allowlist is expanded', () => {
  const previous = { ...process.env };
  process.env.NODE_ENV = 'production';
  process.env.EROS_ENABLED = 'true';
  process.env.EROS_PRODUCTION_TEST_MODE = 'true';
  process.env.EROS_DATA_MODE = 'synthetic';
  process.env.EROS_PRODUCTION_TEST_USER_IDS = ids.user;
  process.env.EROS_PRODUCTION_TEST_CREATOR_IDS = ids.creator;
  process.env.GPOINT_EROS_WORKER_TOKEN = 'worker-test-token';
  process.env.GPOINT_CHAT_POLICY_TENANT_ID = ids.tenant;
  process.env.EROS_WORKER_TENANT_ID = ids.tenant;
  try {
    const controller = new GpointWorkerGatewayController();
    assert.deepEqual(controller.health({ authorization: 'Bearer worker-test-token' }), {
      status: 'ok', lane: 'GPOINT', provider: 'mac_product_worker',
    });
    process.env.EROS_PRODUCTION_TEST_USER_IDS = `${ids.user},${ids.user}`;
    assert.throws(() => controller.health({ authorization: 'Bearer worker-test-token' }), (error: unknown) =>
      error instanceof HttpException && error.getStatus() === 503 &&
      (error.getResponse() as { code?: string }).code === 'EROS_PILOT_RUNTIME_UNAVAILABLE');
  } finally {
    for (const key of Object.keys(process.env)) if (!(key in previous)) delete process.env[key];
    for (const [key, value] of Object.entries(previous)) process.env[key] = value;
  }
});
