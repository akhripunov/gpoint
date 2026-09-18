import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const root = dirname(dirname(fileURLToPath(import.meta.url)));

test('production entrypoint loads the Telegram WebApp SDK before the app', async () => {
  const html = await readFile(join(root, 'index.html'), 'utf8');
  const sdk = '<script src="https://telegram.org/js/telegram-web-app.js"></script>';
  assert.ok(html.includes(sdk));
  assert.ok(html.indexOf(sdk) < html.indexOf('<script type="module"'));
});

test('API client disables conditional caching and bounds request time', async () => {
  const source = await readFile(join(root, 'src/api/client.ts'), 'utf8');
  assert.match(source, /cache:'no-store'/);
  assert.match(source, /setTimeout\(\(\)=>controller\.abort\(\),timeoutMs\)/);
});

test('creator onboarding uses the official startchannel deep link', async () => {
  const client = await readFile(join(root, 'src/api/client.ts'), 'utf8');
  const main = await readFile(join(root, 'src/main.tsx'), 'utf8');
  assert.match(client, /openTelegramLink/);
  assert.match(client, /startchannel&admin=post_messages/);
  assert.match(client, /creator\/channel\/start/);
  assert.doesNotMatch(main, /Prepare channel selector/);
  assert.match(main, /Connect Telegram channel/);
  assert.match(main, /openTelegramStartChannel/);
  assert.doesNotMatch(main, /NativeCreatorOnboardingBridge/);
  assert.match(main, /state==='IDLE'/);
  assert.match(main, /WAITING_FOR_CHANNEL/);
  assert.match(main, /telegramChannelConnectionStatus/);
  assert.match(main, /deadline>Date\.now\(\)\?1500:false/);
  assert.match(main, /Channel connected/);
  assert.match(main, /Continue to Creator tools/);
  assert.match(main, /Try again/);
  assert.doesNotMatch(main, /requestCreatorTelegramChannel/);
});
