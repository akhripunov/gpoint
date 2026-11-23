import test from 'node:test'; import assert from 'node:assert/strict'; import { createHmac } from 'node:crypto'; import { validateTelegramInitData } from '../src/telegram-init-data.js';
test('validates Telegram initData server-side', () => { const token='123:TEST'; const authDate=1700000000; const p=new URLSearchParams({auth_date:String(authDate),query_id:'AA',user:'{"id":7}'}); const data=[...p.entries()].sort(([a],[b])=>a.localeCompare(b)).map(([k,v])=>`${k}=${v}`).join('\n'); const secret=createHmac('sha256','WebAppData').update(token).digest(); p.set('hash',createHmac('sha256',secret).update(data).digest('hex')); assert.equal(validateTelegramInitData(p.toString(),token,authDate+1).query_id,'AA'); assert.throws(()=>validateTelegramInitData(p.toString(),'wrong',authDate+1)); });
import { UnauthorizedException } from '@nestjs/common';
import { AuthController } from '../src/core-v2.js';

test('Telegram endpoint returns HTTP 401 for invalid signatures and expired initData', async () => {
  const previousToken = process.env.TELEGRAM_BOT_TOKEN;
  process.env.TELEGRAM_BOT_TOKEN = '123:TEST';
  try {
    const controller = new AuthController();
    const now = Math.floor(Date.now() / 1000);
    for (const initData of [
      new URLSearchParams({auth_date: String(now), user: '{"id":7}', hash: '0'.repeat(64)}).toString(),
      new URLSearchParams({auth_date: '1700000000', user: '{"id":7}', hash: '0'.repeat(64)}).toString(),
      new URLSearchParams({auth_date: String(now), user: '{"id":7}'}).toString(),
    ]) {
      await assert.rejects(
        () => controller.auth({}, {initData}),
        (error: unknown) => error instanceof UnauthorizedException && error.getStatus() === 401,
      );
    }
  } finally {
    if (previousToken === undefined) delete process.env.TELEGRAM_BOT_TOKEN;
    else process.env.TELEGRAM_BOT_TOKEN = previousToken;
  }
});

