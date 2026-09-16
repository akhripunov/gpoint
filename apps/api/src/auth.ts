import { randomUUID } from 'node:crypto';
import { validateTelegramInitData } from './telegram-init-data.js';
export interface Session { token: string; userId: string; expiresAt: number; }
export function authenticateTelegram(initData: string, botToken: string, upsertUser: (telegramUser: Record<string,string>) => string, nowSeconds = Math.floor(Date.now()/1000)): Session {
  const values = validateTelegramInitData(initData, botToken, nowSeconds); const raw = values.user;
  if (!raw) throw new Error('Telegram user is required'); const user = JSON.parse(raw) as Record<string,string>; if (!user.id) throw new Error('Telegram user id is required');
  return { token: randomUUID(), userId: upsertUser(user), expiresAt: nowSeconds + 3600 };
}
export function devAuthAllowed(nodeEnv: string, devAuthMode: string | undefined): boolean { return nodeEnv !== 'production' && devAuthMode === 'true'; }
export function browserDevAuthAllowed(nodeEnv: string | undefined, flag: string | undefined, host: string | undefined, origin: string | undefined): boolean {
  if (nodeEnv === 'production' || flag !== 'true') return false;
  const hostname = (host ?? '').replace(/^\[/, '').split(']')[0].split(':')[0].toLowerCase();
  if (!['localhost', '127.0.0.1', '::1'].includes(hostname)) return false;
  if (!origin) return true;
  try { return ['localhost', '127.0.0.1', '::1'].includes(new URL(origin).hostname.toLowerCase()); } catch { return false; }
}
