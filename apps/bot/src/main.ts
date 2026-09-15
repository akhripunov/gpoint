import { Bot } from 'grammy';
const token = process.env.TELEGRAM_BOT_TOKEN;
if (!token) { console.error('TELEGRAM_BOT_TOKEN is required'); process.exitCode = 1; } else { const bot = new Bot(token); bot.command('start', ctx => ctx.reply('GPoint seller bot is configured.')); bot.start(); }
