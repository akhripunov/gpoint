import { Bot, InlineKeyboard } from 'grammy';

const token = process.env.TELEGRAM_BOT_TOKEN;
const ingestUrl = process.env.GPOINT_INGEST_URL ?? 'http://127.0.0.1:3000/internal/telegram/update';
const webhookSecret = process.env.TELEGRAM_WEBHOOK_SECRET;
const publicAppUrl = process.env.PUBLIC_APP_URL;

if (!token) {
  console.error('TELEGRAM_BOT_TOKEN is required');
  process.exitCode = 1;
} else if (!publicAppUrl) {
  console.error('PUBLIC_APP_URL is required for the Open GPoint button');
  process.exitCode = 1;
} else {
  const bot = new Bot(token);
  const openKeyboard = new InlineKeyboard().webApp('Open GPoint', publicAppUrl);

  bot.command('start', ctx => ctx.reply('Welcome to GPoint — a creator-first feed.', { reply_markup: openKeyboard }));

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
    await fetch(ingestUrl.replace(/\/internal\/telegram\/update$/, '/internal/telegram/chat-shared'), {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-telegram-webhook-secret': webhookSecret },
      body: JSON.stringify({ from: ctx.from, chat_shared: ctx.msg.chat_shared }),
    }).catch(() => undefined);
  });
  bot.start();
}
