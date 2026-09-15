import { createHmac, timingSafeEqual } from 'node:crypto';

export function validateTelegramInitData(initData: string, botToken: string, nowSeconds = Math.floor(Date.now() / 1000), maxAgeSeconds = 86400): Record<string, string> {
  const params = new URLSearchParams(initData); const received = params.get('hash');
  if (!received) throw new Error('Telegram initData hash is required');
  const authDate = Number(params.get('auth_date')); if (!Number.isFinite(authDate) || nowSeconds - authDate > maxAgeSeconds || authDate > nowSeconds + 60) throw new Error('Telegram initData is expired or from the future');
  params.delete('hash'); const dataCheckString = [...params.entries()].sort(([a],[b]) => a.localeCompare(b)).map(([k,v]) => `${k}=${v}`).join('\n');
  const secret = createHmac('sha256', 'WebAppData').update(botToken).digest(); const expected = createHmac('sha256', secret).update(dataCheckString).digest('hex');
  const a = Buffer.from(expected, 'hex'); const b = Buffer.from(received, 'hex'); if (a.length !== b.length || !timingSafeEqual(a,b)) throw new Error('Telegram initData signature is invalid');
  return Object.fromEntries(params.entries());
}
