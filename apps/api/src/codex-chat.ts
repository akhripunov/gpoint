import { randomBytes, randomInt } from 'node:crypto';
import { BadRequestException, Body, Controller, Get, Headers, HttpException, Param, Post, Req, UnauthorizedException } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';
import { createAgataIntegration } from './agata-integration.js';
import { hashCodexCredential, isCodexOwnerAllowed, parseCodexOwnerUserIds, parseCodexTrustedProxyIps, resolveCodexDeviceStartRateKey, CODEX_ACCESS_TTL_MS, CODEX_DEVICE_TTL_MS, CODEX_REFRESH_TTL_MS } from './codex-chat-auth.js';
import { resolveSession } from './session.js';
import { FixedWindowRateLimiter } from './rate-limit.js';

const prisma = new PrismaClient();
const approvalLimiter = new FixedWindowRateLimiter(20, 60_000);
const ownerIds = () => parseCodexOwnerUserIds(process.env.GPOINT_CODEX_OWNER_USER_IDS);
const secret = () => randomBytes(32).toString('base64url');
const code = () => `${randomInt(0, 0x10000).toString(16).padStart(4, '0')}-${randomInt(0, 0x10000).toString(16).padStart(4, '0')}`.toUpperCase();
function normalUser(headers: Record<string, string | undefined>) {
  const userId = resolveSession(headers.authorization?.replace(/^Bearer\s+/i, ''));
  if (!userId) throw new UnauthorizedException();
  return userId;
}
function bearer(body: unknown, field: 'deviceCode' | 'refreshToken') {
  if (!body || typeof body !== 'object' || typeof (body as Record<string, unknown>)[field] !== 'string' || ((body as Record<string, string>)[field]).length < 32 || ((body as Record<string, string>)[field]).length > 256) throw new BadRequestException({ code: 'invalid_codex_authorization' });
  return (body as Record<string, string>)[field];
}

@Controller('auth/codex')
export class CodexChatController {
  @Post('device/start') async start(@Req() request: { ip?: string; headers?: Record<string, string | string[] | undefined> }) {
    const sourceIp = resolveCodexDeviceStartRateKey({ peerIp: request.ip, forwardedFor: typeof request.headers?.['x-forwarded-for'] === 'string' ? request.headers['x-forwarded-for'] as string : undefined }, { production: process.env.NODE_ENV === 'production', trustedProxyIps: parseCodexTrustedProxyIps(process.env.GPOINT_CODEX_DEVICE_START_TRUSTED_PROXY_IPS) });
    if (!sourceIp) throw new HttpException({ code: 'CODEX_AUTH_NOT_CONFIGURED' }, 503);
    const now = Date.now();
    const sourceHash = hashCodexCredential(sourceIp);
    await prisma.$executeRaw`DELETE FROM "CodexDeviceAuthorization" WHERE "id" IN (SELECT "id" FROM "CodexDeviceAuthorization" WHERE "expiresAt" <= ${new Date(now)} OR ("consumedAt" IS NOT NULL AND "createdAt" <= ${new Date(now - 24 * 60 * 60_000)}) ORDER BY "createdAt" ASC LIMIT 100)`;
    const deviceCode = secret();
    const userCode = code();
    const deviceCodeHash = hashCodexCredential(deviceCode);
    const userCodeHash = hashCodexCredential(userCode);
    const created = await prisma.$transaction(async tx => {
      await tx.$executeRaw`SELECT pg_advisory_xact_lock(hashtextextended(${sourceHash}, 0))`;
      const recentStarts = await tx.codexDeviceAuthorization.count({ where: { sourceHash, createdAt: { gte: new Date(now - 60_000) } } });
      if (recentStarts >= 10) return false;
      await tx.codexDeviceAuthorization.create({ data: { deviceCodeHash, userCodeHash, sourceHash, expiresAt: new Date(now + CODEX_DEVICE_TTL_MS) } });
      return true;
    });
    if (!created) throw new HttpException({ code: 'RATE_LIMITED' }, 429);
    return { deviceCode, userCode, expiresIn: Math.floor(CODEX_DEVICE_TTL_MS / 1000), verificationPath: '/messages' };
  }

  @Post('device/approve') async approve(@Headers() headers: Record<string, string | undefined>, @Body() body: { userCode?: unknown; creatorId?: unknown }) {
    const userId = normalUser(headers);
    if (!isCodexOwnerAllowed(userId, ownerIds())) throw new UnauthorizedException();
    if (!approvalLimiter.allow(`codex-device-approve:${userId}`)) throw new HttpException({ code: 'RATE_LIMITED' }, 429);
    if (typeof body?.userCode !== 'string' || typeof body?.creatorId !== 'string' || body.userCode.length > 16 || body.creatorId.length > 128) throw new BadRequestException({ code: 'invalid_codex_authorization' });
    const normalized = body.userCode.trim().toUpperCase();
    if (!/^[0-9A-F]{4}-[0-9A-F]{4}$/.test(normalized)) throw new BadRequestException({ code: 'invalid_codex_authorization' });
    const creator = await prisma.creator.findFirst({ where: { id: body.creatorId, profile: { status: 'APPROVED' } }, select: { id: true } });
    if (!creator) throw new UnauthorizedException();
    const agata = createAgataIntegration();
    if (!agata) throw new HttpException({ code: 'MESSAGES_UNAVAILABLE' }, 503);
    const history = await agata.listMessages(userId, creator.id, { limit: 1 });
    if (!history.items.length) throw new UnauthorizedException();
    const now = new Date();
    const updated = await prisma.codexDeviceAuthorization.updateMany({
      where: { userCodeHash: hashCodexCredential(normalized), expiresAt: { gt: now }, userId: null, consumedAt: null },
      data: { userId, creatorId: creator.id },
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

  @Get('connection/:creatorId') async status(@Param('creatorId') creatorId: string, @Headers() headers: Record<string, string | undefined>) {
    const userId = normalUser(headers);
    if (!isCodexOwnerAllowed(userId, ownerIds())) return { enabled: false, connected: false };
    const connection = await prisma.codexChatConnection.findFirst({ where: { userId, creatorId, revokedAt: null, refreshExpiresAt: { gt: new Date() } }, select: { id: true, accessExpiresAt: true, refreshExpiresAt: true }, orderBy: { createdAt: 'desc' } });
    return { enabled: true, connected: Boolean(connection), ...(connection ? { accessExpiresAt: connection.accessExpiresAt, refreshExpiresAt: connection.refreshExpiresAt } : {}) };
  }

  @Post('connection/:creatorId/revoke') async revoke(@Param('creatorId') creatorId: string, @Headers() headers: Record<string, string | undefined>) {
    const userId = normalUser(headers);
    if (!isCodexOwnerAllowed(userId, ownerIds())) throw new UnauthorizedException();
    await prisma.codexChatConnection.updateMany({ where: { userId, creatorId, revokedAt: null }, data: { revokedAt: new Date() } });
    return { revoked: true };
  }
}
