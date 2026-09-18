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
