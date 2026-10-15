import { telegramInternalUrl } from './telegram-api-url.js';

type TelegramChat = { id?: number | string; type?: string } | undefined;
type TelegramUser = { id?: number | string } | undefined;

export async function forwardCodexStart(payload: string, chat: TelegramChat, from: TelegramUser, fetchImpl: typeof fetch = fetch): Promise<{ approved: boolean }> {
  const challenge = /^codex_([A-Za-z0-9_-]{43})$/.exec(payload);
  const senderId = typeof from?.id === 'number' || typeof from?.id === 'string' ? String(from.id) : '';
  const chatId = typeof chat?.id === 'number' || typeof chat?.id === 'string' ? String(chat.id) : '';
  const endpoint = telegramInternalUrl('codex-approve', { baseUrl: process.env.GPOINT_INGEST_URL, production: process.env.NODE_ENV === 'production' });
  const webhookSecret = process.env.TELEGRAM_WEBHOOK_SECRET;
  if (!challenge || chat?.type !== 'private' || !/^\d{1,20}$/.test(senderId) || senderId === '0' || BigInt(senderId) > 9_223_372_036_854_775_807n || chatId !== senderId || !endpoint || !webhookSecret) return { approved: false };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8_000);
  try {
    const response = await fetchImpl(endpoint, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-telegram-webhook-secret': webhookSecret },
      body: JSON.stringify({ payload: challenge[1], chat: { id: chat!.id, type: chat!.type }, from: { id: from!.id } }),
      signal: controller.signal,
    });
    if (!response.ok) return { approved: false };
    const result = await response.json() as { approved?: unknown };
    return { approved: result?.approved === true };
  } catch {
    return { approved: false };
  } finally {
    clearTimeout(timer);
  }
}
