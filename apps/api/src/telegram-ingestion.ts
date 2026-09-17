import { createHash } from 'node:crypto';
import { BadRequestException, Body, Controller, Headers, Post, UnauthorizedException } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';
import { ContentSanitizer, TelegramEntity } from './content-sanitizer.js';
import { mediaStorageFromEnv } from './media-storage.js';
import { safetyProviderFromEnv } from './safety-provider.js';
import { HttpTranslationProvider } from './translation-provider.js';
import { inspectImage } from './media-metadata.js';

const prisma = new PrismaClient();
export type SafetyClassification = 'SAFE' | 'SUGGESTIVE' | 'EROTIC' | 'EXPLICIT' | 'UNCERTAIN';
export interface ContentSafetyProvider { classify(input: { text: string; mimeTypes: string[]; imageBytes?: Buffer }): Promise<{ classification: SafetyClassification; reason?: string; provider?: string; providerVersion?: string; confidence?: number; failureReason?: string }> }
export interface TranslationProvider { translate(text: string, sourceLanguage?: string): Promise<{ text: string; provider: string }> }
export interface TelegramChannelPost { chat: { id: number | string; title?: string; username?: string }; message_id: number; date?: number; edit_date?: number; media_group_id?: string; text?: string; caption?: string; entities?: TelegramEntity[]; caption_entities?: TelegramEntity[]; photo?: unknown[]; video?: { mime_type?: string }; document?: { mime_type?: string }; mediaBytesBase64?: string; mediaMimeType?: string; mediaFileSize?: number; mediaDownloadFailed?: boolean; }
export interface TelegramUpdate { channel_post?: TelegramChannelPost; edited_channel_post?: TelegramChannelPost }

export class DeterministicSafetyProvider implements ContentSafetyProvider {
  async classify(input: { text: string; mimeTypes: string[] }) { const value = input.text.toLowerCase(); if (value.includes('#explicit')) return { classification: 'EXPLICIT' as const, reason: 'explicit marker' }; if (value.includes('#erotic')) return { classification: 'EROTIC' as const, reason: 'erotic marker' }; if (value.includes('#uncertain')) return { classification: 'UNCERTAIN' as const, reason: 'uncertain marker' }; return { classification: 'SAFE' as const, reason: 'deterministic local policy' }; }
}
export class FallbackTranslationProvider implements TranslationProvider { async translate(text: string) { return { text, provider: 'fallback-untranslated' }; } }

export function sourceHash(text: string) { return createHash('sha256').update(text).digest('hex'); }
function messageText(m: TelegramChannelPost) { return m.caption ?? m.text ?? ''; }
function hasNoGpoint(text: string) { return /(^|\s)#nogpoint\b/i.test(text); }
export function isEligibleImport(mode: string, hashtag: string | null, text: string) { if (hasNoGpoint(text)) return false; if (mode === 'HASHTAG') return new RegExp(`(^|\\s)#${(hashtag || 'gpoint').replace(/^#/, '')}\\b`, 'i').test(text); return mode === 'AUTO' || mode === 'MANUAL'; }

export class TelegramIngestionService {
  constructor(private readonly safety: ContentSafetyProvider = safetyProviderFromEnv(), private readonly translation: TranslationProvider = new FallbackTranslationProvider(), private readonly sanitizer = new ContentSanitizer(), private readonly storage = mediaStorageFromEnv()) {}
  async ingest(update: TelegramUpdate) {
    const message = update.channel_post ?? update.edited_channel_post;
    if (!message) return { status: 'IGNORED', reason: 'unsupported-update' };
    const chatId = String(message.chat.id); const channel = await prisma.telegramChannel.findUnique({ where: { telegramChatId: chatId } });
    if (!channel || channel.status !== 'CONNECTED') return { status: 'IGNORED', reason: 'channel-not-connected' };
    const raw = messageText(message); if (!isEligibleImport(channel.importMode, channel.importHashtag, raw)) { const existing = await prisma.post.findUnique({ where: { telegramChannelId_telegramMessageId: { telegramChannelId: channel.id, telegramMessageId: message.message_id } } }); await prisma.telegramChannel.update({ where: { id: channel.id }, data: { lastPostReceivedAt: new Date() } }); if (existing && update.edited_channel_post) { const sanitized = this.sanitizer.sanitize(raw, message.entities ?? message.caption_entities ?? []); await prisma.postTranslation.deleteMany({ where: { postId: existing.id } }); await prisma.post.update({ where: { id: existing.id }, data: { sourceText: raw || null, sanitizedText: sanitized.text || null, caption: sanitized.text || null, status: 'HIDDEN', ingestStatus: 'ARCHIVED', telegramEditedAt: new Date() } }); return { status: 'UPDATED', postId: existing.id, reason: 'ineligible-edit' }; } return { status: 'IGNORED', reason: 'ineligible' }; }
    const existing = await prisma.post.findUnique({ where: { telegramChannelId_telegramMessageId: { telegramChannelId: channel.id, telegramMessageId: message.message_id } } });
    if (existing && !update.edited_channel_post) { await prisma.telegramChannel.update({ where: { id: channel.id }, data: { lastPostReceivedAt: new Date() } }); return { status: 'DUPLICATE', postId: existing.id }; }
    const sanitized = this.sanitizer.sanitize(raw, message.entities ?? message.caption_entities ?? []); const mediaTypes = [message.video?.mime_type, message.document?.mime_type, message.photo?.length ? 'image/jpeg' : undefined].filter(Boolean) as string[]; if (message.mediaDownloadFailed) { await prisma.telegramChannel.update({ where: { id: channel.id }, data: { lastPostReceivedAt: new Date(), lastError: 'Telegram Bot API media download failed or exceeded V1 limit' } }); return { status: 'FAILED', reason: 'telegram-media-download-failed' }; }
    const uploadedBytes = message.mediaBytesBase64 ? Buffer.from(message.mediaBytesBase64, 'base64') : undefined; const safety = await this.safety.classify({ text: sanitized.text, mimeTypes: mediaTypes, imageBytes: uploadedBytes }); const sourceVersion = sourceHash(`${raw}\n${JSON.stringify(message.entities ?? message.caption_entities ?? [])}`); const translation = sanitized.text ? await this.translation.translate(sanitized.text) : null;
    const isEdit = !!update.edited_channel_post; const status = channel.importMode === 'MANUAL' || safety.classification !== 'SAFE' ? 'PENDING' : 'PUBLISHED'; const publishedAt = status === 'PUBLISHED' ? new Date() : null;
    let mediaAssetId: string | undefined;
    if (message.mediaBytesBase64) { const bytes = Buffer.from(message.mediaBytesBase64, 'base64'); const mimeType = message.mediaMimeType ?? mediaTypes[0]; if (!mimeType || bytes.length === 0 || bytes.length > 50 * 1024 * 1024) { await prisma.telegramChannel.update({ where: { id: channel.id }, data: { lastPostReceivedAt: new Date(), lastError: 'media exceeds supported Bot API V1 limit or is invalid' } }); return { status: 'FAILED', reason: 'media-limit-or-invalid' }; } try { const kind = mimeType.startsWith('video/') ? 'VIDEO' : 'IMAGE'; const key = `telegram/${channel.id}/${message.message_id}/${kind.toLowerCase()}`; await this.storage.putBytes(key, mimeType, bytes); const metadata = kind === 'IMAGE' ? await inspectImage(bytes) : {}; const asset = await prisma.mediaAsset.create({ data: { creatorId: channel.creatorId, kind, storageProvider: process.env.MEDIA_STORAGE_PROVIDER ?? 'local', storageKey: key, mimeType, fileSize: bytes.length, status: 'READY', ...metadata } }); mediaAssetId = asset.id; } catch { await prisma.telegramChannel.update({ where: { id: channel.id }, data: { lastPostReceivedAt: new Date(), lastError: 'media copy or metadata extraction failed' } }); return { status: 'FAILED', reason: 'media-copy-failed' }; } }
    const data = { creatorId: channel.creatorId, type: mediaAssetId ? (message.mediaMimeType?.startsWith('video/') ? 'VIDEO' : 'IMAGE') : 'TEXT', caption: sanitized.text || null, sanitizedText: sanitized.text || null, sourceText: raw || null, sourceLanguage: 'und', source: 'TELEGRAM_CHANNEL', telegramChannelId: channel.id, telegramMessageId: message.message_id, telegramMediaGroupId: message.media_group_id ?? null, telegramPublishedAt: message.date ? new Date(message.date * 1000) : null, telegramEditedAt: message.edit_date ? new Date(message.edit_date * 1000) : (isEdit ? new Date() : null), safetyClassification: safety.classification, safetyReason: safety.reason ?? null, ingestStatus: status === 'PUBLISHED' ? 'PUBLISHED' : 'PENDING_MODERATION', status, publishedAt };
    const post = existing ? await prisma.post.update({ where: { id: existing.id }, data }) : await prisma.post.create({ data: mediaAssetId ? { ...data, media: { create: { mediaAssetId, sortOrder: 0 } } } : data });
    if (translation) await prisma.postTranslation.deleteMany({ where: { postId: post.id } }).then(() => prisma.postTranslation.create({ data: { postId: post.id, language: 'en', text: translation.text, provider: translation.provider, sourceHash: sourceVersion } }));
    await prisma.telegramChannel.update({ where: { id: channel.id }, data: { lastPostReceivedAt: new Date(), lastSuccessfulIngestAt: new Date(), lastError: null } });
    return { status: existing ? 'UPDATED' : 'CREATED', postId: post.id, safety: safety.classification };
  }
}

export const telegramIngestion = new TelegramIngestionService(safetyProviderFromEnv(), process.env.GPOINT_TRANSLATION_URL ? new HttpTranslationProvider(process.env.GPOINT_TRANSLATION_URL, process.env.GPOINT_TRANSLATION_API_KEY) : new FallbackTranslationProvider());
@Controller('internal/telegram') export class TelegramIngestionController {
  @Post('update') async update(@Headers() headers: Record<string, string | undefined>, @Body() body: TelegramUpdate) { const expected = process.env.TELEGRAM_WEBHOOK_SECRET; if (!expected || headers['x-telegram-webhook-secret'] !== expected) throw new UnauthorizedException(); return telegramIngestion.ingest(body); }
}
