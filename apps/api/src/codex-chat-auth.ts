import { createHash, randomBytes, randomInt, timingSafeEqual } from 'node:crypto';
import { isIP } from 'node:net';

export const CODEX_DEVICE_TTL_MS = 5 * 60_000;
export const CODEX_ACCESS_TTL_MS = 15 * 60_000;
export const CODEX_REFRESH_TTL_MS = 90 * 24 * 60 * 60_000;

export function parseCodexOwnerUserIds(value: string | undefined): ReadonlySet<string> {
  return new Set((value ?? '').split(',').map(item => item.trim()).filter(Boolean));
}

export function isCodexOwnerAllowed(userId: string, allowlist: ReadonlySet<string>): boolean {
  return Boolean(userId) && allowlist.has(userId);
}

export async function isCodexRefreshOwnerCurrent(userId: string, creatorId: string, ownerUserIds: ReadonlySet<string>, isCurrentCreatorOwner: (userId: string, creatorId: string) => Promise<boolean>): Promise<boolean> {
  if (!isCodexOwnerAllowed(userId, ownerUserIds)) return false;
  return isCurrentCreatorOwner(userId, creatorId);
}

export function hashCodexCredential(value: string): string {
  return createHash('sha256').update(value).digest('hex');
}

export function validateTelegramCodexApproval(input: { payload?: unknown; chat?: { id?: unknown; type?: unknown }; from?: { id?: unknown } }): { payload: string; telegramUserId: string } | null {
  const payload = input?.payload;
  const sender = input?.from?.id;
  const chatId = input?.chat?.id;
  const senderId = typeof sender === 'number' || typeof sender === 'string' ? String(sender) : '';
  const privateChatId = typeof chatId === 'number' || typeof chatId === 'string' ? String(chatId) : '';
  if (typeof payload !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(payload) || input?.chat?.type !== 'private' || !/^\d{1,20}$/.test(senderId) || senderId === '0' || BigInt(senderId) > 9_223_372_036_854_775_807n || senderId !== privateChatId) return null;
  return { payload, telegramUserId: senderId };
}

export function validTelegramWebhookSecret(received: string | undefined, expected: string | undefined): boolean {
  return Boolean(received && expected && constantTimeEqual(received, expected));
}

export function parseCodexTrustedProxyIps(value: string | undefined): ReadonlySet<string> | null {
  if (value === undefined || value.trim() === '') return null;
  if (value.trim().toLowerCase() === 'none') return new Set();
  const ips = value.split(',').map(item => item.trim()).filter(Boolean);
  if (!ips.length || ips.some(ip => isIP(ip) === 0)) return null;
  return new Set(ips);
}

export function resolveCodexDeviceStartRateKey(request: { peerIp?: string; forwardedFor?: string }, config: { production: boolean; trustedProxyIps: ReadonlySet<string> | null }): string | null {
  if (!config.trustedProxyIps && config.production) return null;
  const peerIp = request.peerIp?.trim();
  if (!peerIp || isIP(peerIp) === 0) return null;
  if (!config.trustedProxyIps?.has(peerIp)) return peerIp;
  const forwarded = request.forwardedFor?.split(',').map(value => value.trim()).filter(Boolean) ?? [];
  if (forwarded.length === 0 || forwarded.some(ip => isIP(ip) === 0)) return null;
  // The ingress must append the observed socket peer. Walk from the API
  // outward across explicitly trusted hops; earlier client-supplied prefixes
  // are ignored once the first untrusted address is reached.
  for (const ip of forwarded.reverse()) if (!config.trustedProxyIps.has(ip)) return ip;
  return null;
}

export function constantTimeEqual(left: string, right: string): boolean {
  const a = Buffer.from(left);
  const b = Buffer.from(right);
  return a.length === b.length && timingSafeEqual(a, b);
}

export function createCodexDeviceAuthorizationStore(options: {
  now?: () => number;
  randomSecret?: () => string;
  randomUserCode?: () => string;
} = {}) {
  const now = options.now ?? Date.now;
  const randomSecret = options.randomSecret ?? (() => randomBytes(32).toString('base64url'));
  const randomUserCode = options.randomUserCode ?? (() => `${randomInt(0, 0x10000).toString(16).padStart(4, '0')}-${randomInt(0, 0x10000).toString(16).padStart(4, '0')}`.toUpperCase());
  const requests = new Map<string, { userCode: string; deviceCodeHash: string; expiresAt: number; grant?: { userId: string; creatorId: string }; consumed: boolean }>();

  function prune() {
    for (const [key, value] of requests) if (value.expiresAt <= now() || value.consumed) requests.delete(key);
  }
  return {
    start() {
      prune();
      const deviceCode = randomSecret();
      const userCode = randomUserCode();
      const expiresAt = now() + CODEX_DEVICE_TTL_MS;
      requests.set(userCode, { userCode, deviceCodeHash: hashCodexCredential(deviceCode), expiresAt, consumed: false });
      return { deviceCode, userCode, expiresAt };
    },
    approve(userCode: string, approval: { userId: string; creatorId: string; isOwner: boolean; creatorApproved: boolean; hasExistingConversation: boolean }) {
      prune();
      const request = requests.get(userCode.trim().toUpperCase());
      if (!request || request.grant || !approval.isOwner || !approval.creatorApproved || !approval.hasExistingConversation) return false;
      request.grant = { userId: approval.userId, creatorId: approval.creatorId };
      return true;
    },
    consume(deviceCode: string) {
      prune();
      const digest = hashCodexCredential(deviceCode);
      const request = [...requests.values()].find(value => constantTimeEqual(value.deviceCodeHash, digest));
      if (!request?.grant || request.consumed || request.expiresAt <= now()) return null;
      request.consumed = true;
      return request.grant;
    },
    poll(deviceCode: string) {
      prune();
      const digest = hashCodexCredential(deviceCode);
      const request = [...requests.values()].find(value => constantTimeEqual(value.deviceCodeHash, digest));
      if (!request) return { status: 'expired' as const };
      if (!request.grant) return { status: 'pending' as const };
      return { status: 'approved' as const };
    },
  };
}

export function isAllowedCodexMessageRequest(method: string, path: string, creatorId: string): boolean {
  const normalized = path.split('?')[0];
  const expected = `/messages/creator/${encodeURIComponent(creatorId)}`;
  return normalized === expected && (method.toUpperCase() === 'GET' || method.toUpperCase() === 'POST');
}

export async function resolveCodexMessageSession(token: string | undefined, creatorId: string, action: 'read' | 'send' | 'revoke', deps: {
  now: () => number;
  ownerUserIds: ReadonlySet<string>;
  findByAccessTokenHash: (hash: string) => Promise<{ id: string; userId: string; creatorId: string; accessTokenHash: string; accessExpiresAt: number | Date; revokedAt: Date | null } | null>;
  isCurrentCreatorOwner?: (userId: string, creatorId: string) => Promise<boolean>;
}) {
  if (!token) return null;
  const row = await deps.findByAccessTokenHash(hashCodexCredential(token));
  const expiry = row?.accessExpiresAt instanceof Date ? row.accessExpiresAt.getTime() : row?.accessExpiresAt;
  if (!row || !constantTimeEqual(row.accessTokenHash, hashCodexCredential(token)) || row.creatorId !== creatorId || row.revokedAt || expiry === undefined || expiry <= deps.now() || !isCodexOwnerAllowed(row.userId, deps.ownerUserIds)) return null;
  if (action !== 'revoke' && (!deps.isCurrentCreatorOwner || !(await deps.isCurrentCreatorOwner(row.userId, row.creatorId)))) return null;
  return { connectionId: row.id, userId: row.userId };
}
