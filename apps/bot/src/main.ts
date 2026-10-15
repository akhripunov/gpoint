import { Bot, InlineKeyboard } from 'grammy';
import { forwardCodexStart } from './codex-start.js';
import { telegramInternalUrl } from './telegram-api-url.js';

const token = process.env.TELEGRAM_BOT_TOKEN;
const production = process.env.NODE_ENV === 'production';
const ingestUrl = telegramInternalUrl('update', { baseUrl: process.env.GPOINT_INGEST_URL, production });
const webhookSecret = process.env.TELEGRAM_WEBHOOK_SECRET;
const publicAppUrl = process.env.PUBLIC_APP_URL;
const releaseSha = process.env.RELEASE_SHA;

function miniAppLaunchUrl(url: string) {
  const launchUrl = new URL(url);
  if (/^[a-f0-9]{40}$/i.test(releaseSha ?? '')) launchUrl.searchParams.set('v', releaseSha!);
  return launchUrl.toString();
}

if (!token) {
  console.error('TELEGRAM_BOT_TOKEN is required');
  process.exitCode = 1;
} else if (!ingestUrl) {
  console.error('GPOINT_INGEST_URL must be HTTPS in production and point to /api/internal/telegram/update');
  process.exitCode = 1;
} else if (!publicAppUrl) {
  console.error('PUBLIC_APP_URL is required for the Open GPoint button');
  process.exitCode = 1;
} else {
  const bot = new Bot(token);
  const launchUrl = miniAppLaunchUrl(publicAppUrl);
  const openKeyboard = new InlineKeyboard().webApp('Open GPoint', launchUrl);

  void bot.api.setChatMenuButton({ menu_button: { type: 'web_app', text: 'Open GPoint', web_app: { url: launchUrl } } })
    .then(() => console.info('[GPoint] Telegram Mini App menu button synchronized', { releaseShaPresent: Boolean(releaseSha) }))
    .catch(error => console.error('[GPoint] Telegram Mini App menu button sync failed', { reason: error instanceof Error ? error.message : 'UNKNOWN' }));

  bot.command('start', async ctx => {
    const payload = typeof ctx.match === 'string' ? ctx.match.trim() : '';
    if (payload.startsWith('codex_')) {
      const result = await forwardCodexStart(payload, ctx.chat, ctx.from);
      await ctx.reply(result.approved
        ? 'Codex is authorized. Return to Codex to finish connecting.'
        : 'Could not authorize Codex. Return to Codex and request a new link.');
      return;
    }
    await ctx.reply('Welcome to GPoint — a creator-first feed.', { reply_markup: openKeyboard });
  });

  const forward = async (update: any) => {
    if (!webhookSecret) return;
    const message = update.channel_post ?? update.edited_channel_post;
    try {
      const media = message?.video?.file_id ?? message?.document?.file_id ?? message?.photo?.at(-1)?.file_id;
      if (media && message) {
        const file = await bot.api.getFile(media);
        if (file.file_path) {
          let response: Response | undefined;
          for (let attempt = 0; attempt < 2; attempt += 1) {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 15000);
            try {
              response = await fetch(`https://api.telegram.org/file/bot${token}/${file.file_path}`, { signal: controller.signal });
              if (response.ok) break;
            } catch { response = undefined; } finally { clearTimeout(timer); }
          }
          if (!response?.ok) message.mediaDownloadFailed = true;
          else {
            const bytes = Buffer.from(await response.arrayBuffer());
            if (bytes.length <= 20 * 1024 * 1024) {
              message.mediaBytesBase64 = bytes.toString('base64');
              message.mediaMimeType = message.video?.mime_type ?? message.document?.mime_type ?? 'image/jpeg';
              message.mediaFileSize = bytes.length;
            } else message.mediaDownloadFailed = true;
          }
        }
      }
    } catch { if (message) message.mediaDownloadFailed = true; }
    await fetch(ingestUrl, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-telegram-webhook-secret': webhookSecret },
      body: JSON.stringify(update),
    }).catch(() => undefined);
  };

  bot.on('channel_post', ctx => forward(ctx.update));
  bot.on('edited_channel_post', ctx => forward(ctx.update));
  bot.on('message:chat_shared', async ctx => {
    if (!webhookSecret) return;
    await fetch(telegramInternalUrl('chat-shared', { baseUrl: process.env.GPOINT_INGEST_URL, production })!, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-telegram-webhook-secret': webhookSecret },
      body: JSON.stringify({ from: ctx.from, chat_shared: ctx.msg.chat_shared }),
    }).catch(() => undefined);
  });
  bot.on('my_chat_member', async ctx => {
    if (!webhookSecret) return;
    const update = ctx.update.my_chat_member;
    console.info('[GPoint] my_chat_member received', {
      updateId: ctx.update.update_id,
      chatType: update?.chat?.type ?? null,
      newStatus: update?.new_chat_member?.status ?? null,
      actorPresent: Boolean(update?.from?.id),
    });
    const response = await fetch(telegramInternalUrl('my-chat-member', { baseUrl: process.env.GPOINT_INGEST_URL, production })!, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-telegram-webhook-secret': webhookSecret },
      body: JSON.stringify({ ...update, updateId: ctx.update.update_id }),
    }).catch(() => undefined);
    console.info('[GPoint] my_chat_member forwarded', { updateId: ctx.update.update_id, apiStatus: response?.status ?? 'NETWORK_ERROR' });
  });
  bot.start({ allowed_updates: ['message', 'channel_post', 'edited_channel_post', 'my_chat_member'] });
}
