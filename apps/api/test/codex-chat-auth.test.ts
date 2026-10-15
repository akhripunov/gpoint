import assert from 'node:assert/strict';
import test from 'node:test';

test('OWNER allowlist is exact and fails closed when unset', async () => {
  const auth = await import('../src/codex-chat-auth.js');
  assert.deepEqual([...auth.parseCodexOwnerUserIds(undefined)], []);
  const allowlist = auth.parseCodexOwnerUserIds('owner-1, owner-2');
  assert.equal(auth.isCodexOwnerAllowed('owner-1', allowlist), true);
  assert.equal(auth.isCodexOwnerAllowed('owner-3', allowlist), false);
  assert.equal(auth.isCodexOwnerAllowed('', allowlist), false);
});

test('device approval requires allowlist, approved creator and existing owner history', async () => {
  const { createCodexDeviceAuthorizationStore } = await import('../src/codex-chat-auth.js');
  let now = 1_000;
  const store = createCodexDeviceAuthorizationStore({
    now: () => now,
    randomSecret: () => 'a'.repeat(43),
    randomUserCode: () => 'ABCD-EFGH',
  });
  const request = store.start();
  assert.equal(store.approve(request.userCode, {
    userId: 'owner-1', creatorId: 'creator-1', isOwner: false, creatorApproved: true, hasExistingConversation: true,
  }), false);
  assert.equal(store.approve(request.userCode, {
    userId: 'owner-1', creatorId: 'creator-1', isOwner: true, creatorApproved: false, hasExistingConversation: true,
  }), false);
  assert.equal(store.approve(request.userCode, {
    userId: 'owner-1', creatorId: 'creator-1', isOwner: true, creatorApproved: true, hasExistingConversation: false,
  }), false);
  assert.equal(store.approve(request.userCode, {
    userId: 'owner-1', creatorId: 'creator-1', isOwner: true, creatorApproved: true, hasExistingConversation: true,
  }), true);
  assert.deepEqual(store.consume(request.deviceCode), { userId: 'owner-1', creatorId: 'creator-1' });
  assert.equal(store.consume(request.deviceCode), null);
  now += 1;
});

test('device code expires and cannot be approved or exchanged afterward', async () => {
  const { createCodexDeviceAuthorizationStore } = await import('../src/codex-chat-auth.js');
  let now = 10_000;
  const store = createCodexDeviceAuthorizationStore({ now: () => now, randomSecret: () => 'b'.repeat(43), randomUserCode: () => 'WXYZ-1234' });
  const request = store.start();
  now = request.expiresAt;
  assert.equal(store.approve(request.userCode, {
    userId: 'owner-1', creatorId: 'creator-1', isOwner: true, creatorApproved: true, hasExistingConversation: true,
  }), false);
  assert.equal(store.consume(request.deviceCode), null);
});

test('Codex credential is restricted to history and send for its exact creator', async () => {
  const { isAllowedCodexMessageRequest } = await import('../src/codex-chat-auth.js');
  assert.equal(isAllowedCodexMessageRequest('GET', '/messages/creator/creator-1?limit=50', 'creator-1'), true);
  assert.equal(isAllowedCodexMessageRequest('POST', '/messages/creator/creator-1', 'creator-1'), true);
  assert.equal(isAllowedCodexMessageRequest('GET', '/messages/creator/creator-2', 'creator-1'), false);
  assert.equal(isAllowedCodexMessageRequest('POST', '/messages/creator/creator-1/read', 'creator-1'), false);
  assert.equal(isAllowedCodexMessageRequest('GET', '/messages', 'creator-1'), false);
  assert.equal(isAllowedCodexMessageRequest('POST', '/creator/me/profile', 'creator-1'), false);
});

test('credential storage uses a one-way digest, not the bearer text', async () => {
  const { hashCodexCredential } = await import('../src/codex-chat-auth.js');
  const first = hashCodexCredential('opaque-secret');
  assert.notEqual(first, 'opaque-secret');
  assert.equal(hashCodexCredential('opaque-secret'), first);
  assert.notEqual(hashCodexCredential('other-secret'), first);
});

test('Telegram authorization requires an exact private chat identity and unmodified deep-link challenge', async () => {
  const { validateTelegramCodexApproval } = await import('../src/codex-chat-auth.js');
  const payload = 'A'.repeat(43);
  assert.deepEqual(validateTelegramCodexApproval({ payload, chat: { id: 123, type: 'private' }, from: { id: 123 } }), { payload, telegramUserId: '123' });
  assert.equal(validateTelegramCodexApproval({ payload, chat: { id: 123, type: 'group' }, from: { id: 123 } }), null);
  assert.equal(validateTelegramCodexApproval({ payload, chat: { id: 124, type: 'private' }, from: { id: 123 } }), null);
  assert.equal(validateTelegramCodexApproval({ payload: `${payload}=`, chat: { id: 123, type: 'private' }, from: { id: 123 } }), null);
  assert.equal(validateTelegramCodexApproval({ payload, chat: { id: 123, type: 'private' } }), null);
});

test('bot callback webhook secret is required and compared exactly', async () => {
  const { validTelegramWebhookSecret } = await import('../src/codex-chat-auth.js');
  assert.equal(validTelegramWebhookSecret('secret', 'secret'), true);
  assert.equal(validTelegramWebhookSecret(undefined, 'secret'), false);
  assert.equal(validTelegramWebhookSecret('secret', undefined), false);
  assert.equal(validTelegramWebhookSecret('Secret', 'secret'), false);
});

test('device-start rate key trusts forwarded IP only from explicitly trusted proxies', async () => {
  const { parseCodexTrustedProxyIps, resolveCodexDeviceStartRateKey } = await import('../src/codex-chat-auth.js');
  assert.equal(parseCodexTrustedProxyIps(undefined), null);
  assert.equal(parseCodexTrustedProxyIps('not-an-ip'), null);
  assert.deepEqual([...parseCodexTrustedProxyIps('none')!], []);
  assert.deepEqual([...parseCodexTrustedProxyIps('198.51.100.10, ::1')!], ['198.51.100.10', '::1']);
  assert.equal(resolveCodexDeviceStartRateKey({ peerIp: '198.51.100.10', forwardedFor: '203.0.113.7' }, { production: true, trustedProxyIps: null }), null);
  assert.equal(resolveCodexDeviceStartRateKey({ peerIp: '198.51.100.10', forwardedFor: '203.0.113.7' }, { production: true, trustedProxyIps: new Set() }), '198.51.100.10');
  assert.equal(resolveCodexDeviceStartRateKey({ peerIp: '198.51.100.10', forwardedFor: '203.0.113.7' }, { production: true, trustedProxyIps: new Set(['198.51.100.10']) }), '203.0.113.7');
  assert.equal(resolveCodexDeviceStartRateKey({ peerIp: '198.51.100.10', forwardedFor: '192.0.2.99, 203.0.113.7, 192.0.2.9' }, { production: true, trustedProxyIps: new Set(['198.51.100.10', '192.0.2.9']) }), '203.0.113.7');
  assert.equal(resolveCodexDeviceStartRateKey({ peerIp: '198.51.100.10', forwardedFor: '203.0.113.7' }, { production: true, trustedProxyIps: new Set(['198.51.100.11']) }), '198.51.100.10');
  assert.equal(resolveCodexDeviceStartRateKey({ peerIp: '198.51.100.10', forwardedFor: 'not-an-ip' }, { production: true, trustedProxyIps: new Set(['198.51.100.10']) }), null);
  assert.equal(resolveCodexDeviceStartRateKey({ peerIp: '198.51.100.10', forwardedFor: '192.0.2.9' }, { production: true, trustedProxyIps: new Set(['198.51.100.10', '192.0.2.9']) }), null);
  assert.equal(resolveCodexDeviceStartRateKey({ peerIp: '198.51.100.10', forwardedFor: '203.0.113.7' }, { production: false, trustedProxyIps: null }), '198.51.100.10');
});

test('scoped message resolution rejects wrong creator, expired, revoked and non-allowlisted grants', async () => {
  const { hashCodexCredential, resolveCodexMessageSession } = await import('../src/codex-chat-auth.js');
  const now = 5_000;
  const row = {
    id: 'connection-1', userId: 'owner-1', creatorId: 'creator-1',
    accessTokenHash: hashCodexCredential('access-1'), accessExpiresAt: now + 1_000,
    refreshExpiresAt: now + 50_000, revokedAt: null,
  };
  const deps = {
    now: () => now,
    ownerUserIds: new Set(['owner-1']),
    findByAccessTokenHash: async (hash: string) => hash === row.accessTokenHash ? row : null,
  };
  assert.deepEqual(await resolveCodexMessageSession('access-1', 'creator-1', 'read', deps), {
    connectionId: 'connection-1', userId: 'owner-1',
  });
  assert.equal(await resolveCodexMessageSession('access-1', 'creator-2', 'read', deps), null);
  assert.deepEqual(await resolveCodexMessageSession('access-1', 'creator-1', 'revoke', deps), { connectionId: 'connection-1', userId: 'owner-1' });
  assert.equal(await resolveCodexMessageSession('unknown', 'creator-1', 'read', deps), null);
  assert.equal(await resolveCodexMessageSession('access-1', 'creator-1', 'read', {
    ...deps, ownerUserIds: new Set(['another-owner']),
  }), null);
  assert.equal(await resolveCodexMessageSession('access-1', 'creator-1', 'read', {
    ...deps, findByAccessTokenHash: async () => ({ ...row, accessExpiresAt: now }),
  }), null);
  assert.equal(await resolveCodexMessageSession('access-1', 'creator-1', 'read', {
    ...deps, findByAccessTokenHash: async () => ({ ...row, revokedAt: new Date(now) }),
  }), null);
});
