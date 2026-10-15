#!/usr/bin/env python3
"""Start the exact production Bot image offline with a stub Telegram API."""
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid


FETCH_STUB = r'''const Module = require('node:module');
const originalLoad = Module._load;
const fetchStub = async input => {
  const value = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  const url = new URL(value);
  const method = url.pathname.split('/').at(-1);
  let result = true;
  if (method === 'getMe') {
    result = { id: 123456, is_bot: true, first_name: 'Smoke', username: 'gpoint_smoke_bot' };
  } else if (method === 'getUpdates') {
    console.log('BOT_STARTUP_SMOKE_POLLING=PASS');
    await new Promise(resolve => setTimeout(resolve, 100));
    result = [];
  }
  return { ok: true, json: async () => ({ ok: true, result }) };
};
fetchStub.default = fetchStub;
Module._load = function(request, parent, isMain) {
  if (request === 'node-fetch') {
    return fetchStub;
  }
  return originalLoad.call(this, request, parent, isMain);
};
'''


def run(*args, capture=False):
    result = subprocess.run(args, check=True, text=True,
                            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=30)
    return result.stdout.strip() if capture else None


def smoke(image):
    container = f'gpoint-bot-smoke-{uuid.uuid4().hex[:12]}'
    with tempfile.TemporaryDirectory(prefix='gpoint-bot-smoke-') as temp:
        stub = Path(temp) / 'telegram-fetch.cjs'
        stub.write_text(FETCH_STUB)
        stub.chmod(0o600)
        try:
            run('docker', 'run', '-d', '--name', container, '--network', 'none',
                '--read-only', '--tmpfs', '/tmp', '-v', f'{stub}:/tmp/telegram-fetch.cjs:ro',
                '-e', 'NODE_ENV=production', '-e', 'TELEGRAM_BOT_TOKEN=123456:offline-smoke-token',
                '-e', 'GPOINT_INGEST_URL=https://gpoint-smoke.invalid/api/internal/telegram/update',
                '-e', 'TELEGRAM_WEBHOOK_SECRET=offline-smoke-secret',
                '-e', 'PUBLIC_APP_URL=https://gpoint-smoke.invalid',
                '-e', 'NODE_OPTIONS=--require=/tmp/telegram-fetch.cjs',
                image, 'node', 'apps/bot/dist/main.js')
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = run('docker', 'inspect', '--format', '{{.State.Running}}', container, capture=True)
                logs = run('docker', 'logs', container, capture=True)
                if state == 'true' and 'BOT_STARTUP_SMOKE_POLLING=PASS' in logs:
                    print('BOT_PRODUCTION_IMAGE_STARTUP=PASS')
                    return
                if state != 'true':
                    break
                time.sleep(0.25)
            raise RuntimeError('production Bot image failed offline startup/long-poll smoke')
        finally:
            subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False, timeout=20)


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise RuntimeError('usage: smoke_bot_image.py <production-image>')
        smoke(sys.argv[1])
    except Exception as error:
        print(f'BOT_PRODUCTION_IMAGE_STARTUP=FAIL reason={type(error).__name__}', file=sys.stderr)
        raise SystemExit(1)
