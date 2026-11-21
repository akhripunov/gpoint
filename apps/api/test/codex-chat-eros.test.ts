import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import test from 'node:test';
import { HttpException, UnauthorizedException } from '@nestjs/common';
import type { PrismaClient } from '@prisma/client';
import { hashCodexCredential } from '../src/codex-chat-auth.js';
import { approveCodexDeviceWithTelegram } from '../src/codex-chat.js';

const IDS = {
  owner: 'cowner_eros_test_1',
  creator: 'ccreator_eros_test_1',
  instance: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc1',
  session: '7f43ff08-7bf9-5647-9dc2-bb4a46f8edc2',
};

function withOwnedErosEnvironment<T>(run: () => Promise<T>) {
  const keys = [
    'NODE_ENV', 'EROS_ENABLED', 'EROS_DATA_MODE', 'GPOINT_CHAT_POLICY_TENANT_ID',
    'EROS_SERVICE_URL', 'EROS_JWT_SECRET', 'AGATA_ENABLED', 'AGATA_SERVICE_URL',
    'AGATA_INTERNAL_API_TOKEN', 'GPOINT_CODEX_OWNER_USER_IDS', 'TELEGRAM_WEBHOOK_SECRET',
  ];
  const previous = new Map(keys.map((key) => [key, process.env[key]]));
  process.env.NODE_ENV = 'production';
  process.env.EROS_ENABLED = 'true';
  process.env.EROS_DATA_MODE = 'gpoint-owned';
  process.env.GPOINT_CHAT_POLICY_TENANT_ID = '30000000-0000-4000-8000-000000000003';
  process.env.EROS_JWT_SECRET = 'disposable-test-eros-jwt-secret-32-bytes';
  process.env.GPOINT_CODEX_OWNER_USER_IDS = IDS.owner;
  process.env.TELEGRAM_WEBHOOK_SECRET = 'disposable-test-webhook-secret';
  delete process.env.AGATA_ENABLED;
  delete process.env.AGATA_SERVICE_URL;
  delete process.env.AGATA_INTERNAL_API_TOKEN;

  return run().finally(() => {
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });
}

function approvalStore(payload: string, telegramId: string, approvedCount: () => void) {
  return {
    user: {
      findUnique: async (args: any) => {
        assert.equal(args.where.telegramUserId, BigInt(telegramId));
        return { id: IDS.owner };
      },
    },
    codexDeviceAuthorization: {
      findFirst: async (args: any) => {
        assert.equal(args.where.telegramChallengeHash, hashCodexCredential(payload));
        return { id: 'authorization-1', creatorId: IDS.creator };
      },
      updateMany: async (args: any) => {
        assert.equal(args.where.id, 'authorization-1');
        approvedCount();
        return { count: 1 };
      },
    },
    creator: {
      findFirst: async (args: any) => {
        assert.equal(args.where.id, IDS.creator);
        assert.deepEqual(args.where.owners.some, { userId: IDS.owner });
        return { id: IDS.creator };
      },
    },
    erosRelationshipMapping: {
      findUnique: async (args: any) => {
        assert.deepEqual(args.where.userId_creatorId, { userId: IDS.owner, creatorId: IDS.creator });
        return { instanceId: IDS.instance };
      },
    },
  } as unknown as PrismaClient;
}

async function startErosFixture(withHistory: boolean) {
  const paths: string[] = [];
  const server = createServer(async (request, response) => {
    paths.push(request.url ?? '');
    response.setHeader('content-type', 'application/json');
    if (request.url?.endsWith('/sessions')) {
      response.end(JSON.stringify({ sessions: [{
        session_id: IDS.session,
        instance_id: IDS.instance,
        channel: 'text',
        last_active_at: new Date().toISOString(),
      }] }));
      return;
    }
    if (request.url?.includes(`/history?limit=1`)) {
      const messages = withHistory ? [{
        id: 'existing-message', role: 'user', content: 'previous conversation',
        sent_at: new Date().toISOString(),
      }] : [];
      response.end(JSON.stringify({ messages, total: messages.length }));
      return;
    }
    response.statusCode = 404;
    response.end(JSON.stringify({ code: 'not_found' }));
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address !== 'string');
  process.env.EROS_SERVICE_URL = `http://127.0.0.1:${address.port}`;
  return {
    paths,
    close: () => new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve())),
  };
}

test('Codex Telegram approval uses existing GPoint-owned Eros history when AGATA is disabled', async () => {
  const telegramId = String(Date.now() + 300_001);
  const payload = 'A'.repeat(43);
  let updates = 0;
  await withOwnedErosEnvironment(async () => {
    const eros = await startErosFixture(true);
    try {
      const result = await approveCodexDeviceWithTelegram(
        { 'x-telegram-webhook-secret': 'disposable-test-webhook-secret' },
        { payload, chat: { id: telegramId, type: 'private' }, from: { id: telegramId } },
        approvalStore(payload, telegramId, () => { updates += 1; }),
      );
      assert.deepEqual(result, { approved: true });
      assert.equal(updates, 1);
      assert.equal(eros.paths.length, 2);
      assert.ok(eros.paths.some((path) => path.endsWith('/sessions')));
      assert.ok(eros.paths.some((path) => path.includes(`/history?limit=1`)));
    } finally {
      await eros.close();
    }
  });
});

test('Codex Telegram approval refuses an Eros relationship without existing history', async () => {
  const telegramId = String(Date.now() + 300_002);
  const payload = 'B'.repeat(43);
  let updates = 0;
  await withOwnedErosEnvironment(async () => {
    const eros = await startErosFixture(false);
    try {
      await assert.rejects(
        approveCodexDeviceWithTelegram(
          { 'x-telegram-webhook-secret': 'disposable-test-webhook-secret' },
          { payload, chat: { id: telegramId, type: 'private' }, from: { id: telegramId } },
          approvalStore(payload, telegramId, () => { updates += 1; }),
        ),
        (error: unknown) => error instanceof UnauthorizedException,
      );
      assert.equal(updates, 0);
      assert.equal(eros.paths.length, 2);
    } finally {
      await eros.close();
    }
  });
});

test('Codex Telegram approval fails closed when Eros history is unavailable', async () => {
  const telegramId = String(Date.now() + 300_003);
  const payload = 'C'.repeat(43);
  let updates = 0;
  await withOwnedErosEnvironment(async () => {
    process.env.EROS_SERVICE_URL = 'http://127.0.0.1:1';
    await assert.rejects(
      approveCodexDeviceWithTelegram(
        { 'x-telegram-webhook-secret': 'disposable-test-webhook-secret' },
        { payload, chat: { id: telegramId, type: 'private' }, from: { id: telegramId } },
        approvalStore(payload, telegramId, () => { updates += 1; }),
      ),
      (error: unknown) => error instanceof HttpException
        && error.getStatus() === 503
        && (error.getResponse() as { code?: string }).code === 'EROS_UNAVAILABLE',
    );
    assert.equal(updates, 0);
  });
});
