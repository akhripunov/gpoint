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

test('native creator onboarding uses requestChat and does not require manual first-step fields', async () => {
  const client = await readFile(join(root, 'src/api/client.ts'), 'utf8');
  const main = await readFile(join(root, 'src/main.tsx'), 'utf8');
  assert.match(client, /requestChat/);
  assert.match(client, /version/);
  assert.match(client, /9\.6/);
  assert.match(client, /selection\/prepare/);
  assert.match(client, /requestCreatorTelegramChannel\(requestId:string\)/);
  assert.doesNotMatch(main, /queryKey:\['creator-channel-selection'\]/);
  assert.match(main, /queryKey:\['creator-channel-onboarding'\]/);
  assert.match(main, /onClick=\{onBecomeCreator\}/);
  assert.match(main, /onClick=\{select\}/);
  assert.doesNotMatch(main, /NativeCreatorOnboardingBridge/);
  assert.match(main, /state==='PREPARING'/);
  assert.match(main, /state==='READY'/);
  assert.match(main, /WAITING_FOR_BOT/);
  assert.match(main, /setState\('READY'\)/);
  assert.match(main, /Try again/);
  assert.match(client, /prepareInFlight/);
  assert.match(client, /prepareAttemptCount/);
  assert.match(client, /Could not prepare channel selector/);
});
