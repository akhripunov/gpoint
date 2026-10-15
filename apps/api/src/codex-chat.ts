import { randomBytes } from 'node:crypto';
import { BadRequestException, Body, Controller, Headers, HttpException, Param, Post, Req, UnauthorizedException } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';
import { createAgataIntegration } from './agata-integration.js';
import { hashCodexCredential, isCodexOwnerAllowed, parseCodexOwnerUserIds, parseCodexTrustedProxyIps, resolveCodexDeviceStartRateKey, resolveCodexMessageSession, validateTelegramCodexApproval, validTelegramWebhookSecret, CODEX_ACCESS_TTL_MS, CODEX_DEVICE_TTL_MS, CODEX_REFRESH_TTL_MS } from './codex-chat-auth.js';
import { FixedWindowRateLimiter } from './rate-limit.js';

const prisma = new PrismaClient();
const approvalLimiter = new FixedWindowRateLimiter(20, 60_000);
const ownerIds = () => parseCodexOwnerUserIds(process.env.GPOINT_CODEX_OWNER_USER_IDS);
const secret = () => randomBytes(32).toString('base64url');
const challenge = () => randomBytes(32).toString('base64url');
function bearer(body: unknown, field: 'deviceCode' | 'refreshToken') {
  if (!body || typeof body !== 'object' || typeof (body as Record<string, unknown>)[field] !== 'string' || ((body as Record<string, string>)[field]).length < 32 || ((body as Record<string, string>)[field]).length > 256) throw new BadRequestException({ code: 'invalid_codex_authorization' });
  return (body as Record<string, string>)[field];
}

@Controller('auth/codex')
export class CodexChatController {
  @Post('device/start') async start(@Req() request: { ip?: string; headers?: Record<string, string | string[] | undefined> }, @Body() body: { creatorSlug?: unknown }) {
    const sourceIp = resolveCodexDeviceStartRateKey({ peerIp: request.ip, forwardedFor: typeof request.headers?.['x-forwarded-for'] === 'string' ? request.headers['x-forwarded-for'] as string : undefined }, { production: process.env.NODE_ENV === 'production', trustedProxyIps: parseCodexTrustedProxyIps(process.env.GPOINT_CODEX_DEVICE_START_TRUSTED_PROXY_IPS) });
    if (!sourceIp) throw new HttpException({ code: 'CODEX_AUTH_NOT_CONFIGURED' }, 503);
    const now = Date.now();
    const sourceHash = hashCodexCredential(sourceIp);
    await prisma.$executeRaw`DELETE FROM "CodexDeviceAuthorization" WHERE "id" IN (SELECT "id" FROM "CodexDeviceAuthorization" WHERE "expiresAt" <= ${new Date(now)} OR ("consumedAt" IS NOT NULL AND "createdAt" <= ${new Date(now - 24 * 60 * 60_000)}) ORDER BY "createdAt" ASC LIMIT 100)`;
    if (typeof body?.creatorSlug !== 'string' || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(body.creatorSlug)) throw new BadRequestException({ code: 'invalid_codex_authorization' });
    const creator = await prisma.creator.findFirst({ where: { slug: body.creatorSlug, profile: { status: 'APPROVED' } }, select: { id: true } });
    const botUsername = process.env.TELEGRAM_BOT_USERNAME?.replace(/^@/, '');
    if (!creator || !botUsername || !/^[A-Za-z0-9_]{5,32}$/.test(botUsername)) throw new UnauthorizedException();
    const deviceCode = secret();
    const telegramChallenge = challenge();
    const deviceCodeHash = hashCodexCredential(deviceCode);
    const telegramChallengeHash = hashCodexCredential(telegramChallenge);
    const created = await prisma.$transaction(async tx => {
      await tx.$executeRaw`SELECT pg_advisory_xact_lock(hashtextextended(${sourceHash}, 0))`;
      const recentStarts = await tx.codexDeviceAuthorization.count({ where: { sourceHash, createdAt: { gte: new Date(now - 60_000) } } });
      if (recentStarts >= 10) return false;
      await tx.codexDeviceAuthorization.create({ data: { deviceCodeHash, telegramChallengeHash, creatorId: creator.id, sourceHash, expiresAt: new Date(now + CODEX_DEVICE_TTL_MS) } });
      return true;
    });
    if (!created) throw new HttpException({ code: 'RATE_LIMITED' }, 429);
    return { deviceCode, telegramLink: `https://t.me/${botUsername}?start=codex_${telegramChallenge}`, expiresIn: Math.floor(CODEX_DEVICE_TTL_MS / 1000) };
  }

  @Post('telegram/codex-approve') async telegramApprove(@Headers() headers: Record<string, string | undefined>, @Body() body: { payload?: unknown; chat?: { id?: number | string; type?: string }; from?: { id?: number | string } }) {
    const expected = process.env.TELEGRAM_WEBHOOK_SECRET;
    if (!validTelegramWebhookSecret(headers['x-telegram-webhook-secret'], expected)) throw new UnauthorizedException();
    const proof = validateTelegramCodexApproval(body);
    if (!proof) throw new UnauthorizedException();
    const { payload, telegramUserId: senderId } = proof;
    if (!approvalLimiter.allow(`codex-telegram-approve:${senderId}`)) throw new HttpException({ code: 'RATE_LIMITED' }, 429);
    const user = await prisma.user.findUnique({ where: { telegramUserId: BigInt(senderId) }, select: { id: true } });
    if (!user || !isCodexOwnerAllowed(user.id, ownerIds())) throw new UnauthorizedException();
    const authorization = await prisma.codexDeviceAuthorization.findFirst({ where: { telegramChallengeHash: hashCodexCredential(payload), expiresAt: { gt: new Date() }, userId: null, consumedAt: null }, select: { id: true, creatorId: true } });
    if (!authorization?.creatorId) throw new UnauthorizedException();
    const agata = createAgataIntegration();
    if (!agata) throw new HttpException({ code: 'MESSAGES_UNAVAILABLE' }, 503);
    const creator = await prisma.creator.findFirst({ where: { id: authorization.creatorId, profile: { status: 'APPROVED' }, owners: { some: { userId: user.id } } }, select: { id: true } });
    if (!creator) throw new UnauthorizedException();
    const history = await agata.listMessages(user.id, creator.id, { limit: 1 });
    if (!history.items.length) throw new UnauthorizedException();
    const now = new Date();
    const updated = await prisma.codexDeviceAuthorization.updateMany({
      where: { id: authorization.id, telegramChallengeHash: hashCodexCredential(payload), expiresAt: { gt: now }, userId: null, consumedAt: null },
      data: { userId: user.id },
    });
    if (!updated.count) throw new UnauthorizedException();
    return { approved: true };
  }

  @Post('device/exchange') async exchange(@Body() body: unknown) {
    const deviceCode = bearer(body, 'deviceCode');
    const now = new Date();
    const authorization = await prisma.codexDeviceAuthorization.findFirst({ where: { deviceCodeHash: hashCodexCredential(deviceCode), expiresAt: { gt: now }, consumedAt: null } });
    if (!authorization) return { status: 'expired' as const };
    if (!authorization.userId || !authorization.creatorId) return { status: 'pending' as const };
    if (!isCodexOwnerAllowed(authorization.userId, ownerIds())) return { status: 'expired' as const };
    const accessToken = secret();
    const refreshToken = secret();
    const accessExpiresAt = new Date(Date.now() + CODEX_ACCESS_TTL_MS);
    const refreshExpiresAt = new Date(Date.now() + CODEX_REFRESH_TTL_MS);
    const result = await prisma.$transaction(async tx => {
      const consumed = await tx.codexDeviceAuthorization.updateMany({ where: { id: authorization.id, consumedAt: null, expiresAt: { gt: new Date() } }, data: { consumedAt: new Date() } });
      if (!consumed.count) return false;
      await tx.codexChatConnection.create({ data: { userId: authorization.userId!, creatorId: authorization.creatorId!, accessTokenHash: hashCodexCredential(accessToken), refreshTokenHash: hashCodexCredential(refreshToken), accessExpiresAt, refreshExpiresAt } });
      return true;
    });
    if (!result) return { status: 'expired' as const };
    return { status: 'connected' as const, accessToken, refreshToken, accessExpiresAt: accessExpiresAt.toISOString(), refreshExpiresAt: refreshExpiresAt.toISOString(), creatorId: authorization.creatorId };
  }

  @Post('device/refresh') async refresh(@Body() body: unknown) {
    const refreshToken = bearer(body, 'refreshToken');
    const current = await prisma.codexChatConnection.findUnique({ where: { refreshTokenHash: hashCodexCredential(refreshToken) } });
    const now = new Date();
    if (!current || current.revokedAt || current.refreshExpiresAt <= now || !isCodexOwnerAllowed(current.userId, ownerIds())) throw new UnauthorizedException();
    const accessToken = secret();
    const nextRefreshToken = secret();
    const accessExpiresAt = new Date(Date.now() + CODEX_ACCESS_TTL_MS);
    const changed = await prisma.codexChatConnection.updateMany({ where: { id: current.id, refreshTokenHash: current.refreshTokenHash, revokedAt: null, refreshExpiresAt: { gt: now } }, data: { accessTokenHash: hashCodexCredential(accessToken), refreshTokenHash: hashCodexCredential(nextRefreshToken), accessExpiresAt } });
    if (!changed.count) throw new UnauthorizedException();
    return { accessToken, refreshToken: nextRefreshToken, accessExpiresAt: accessExpiresAt.toISOString(), refreshExpiresAt: current.refreshExpiresAt.toISOString(), creatorId: current.creatorId };
  }

  @Post('connection/:creatorId/revoke') async revoke(@Param('creatorId') creatorId: string, @Headers() headers: Record<string, string | undefined>) {
    const token = headers.authorization?.replace(/^Bearer\s+/i, '');
    const scoped = await resolveCodexMessageSession(token, creatorId, 'revoke', { now: Date.now, ownerUserIds: ownerIds(), findByAccessTokenHash: async hash => {
      const row = await prisma.codexChatConnection.findUnique({ where: { accessTokenHash: hash } });
      return row ? { id: row.id, userId: row.userId, creatorId: row.creatorId, accessTokenHash: row.accessTokenHash, accessExpiresAt: row.accessExpiresAt, revokedAt: row.revokedAt } : null;
    } });
    if (!scoped) throw new UnauthorizedException();
    await prisma.codexChatConnection.updateMany({ where: { id: scoped.connectionId, userId: scoped.userId, creatorId, revokedAt: null }, data: { revokedAt: new Date() } });
    return { revoked: true };
  }
}
