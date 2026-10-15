import assert from 'node:assert/strict';
import test from 'node:test';
import { access } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

test('production build emits the JavaScript helper imported by the bot entrypoint', async () => {
  const compiledHelper = fileURLToPath(new URL('../dist/telegram-api-url.js', import.meta.url));
  await assert.doesNotReject(access(compiledHelper));

  const { telegramInternalUrl } = await import('../dist/telegram-api-url.js');
  assert.equal(
    telegramInternalUrl('update', {
      baseUrl: 'https://gpoint.example.test/api/internal/telegram/update',
      production: true,
    })?.href,
    'https://gpoint.example.test/api/internal/telegram/update',
  );
});
