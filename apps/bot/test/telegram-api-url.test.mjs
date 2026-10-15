import assert from 'node:assert/strict';
import test from 'node:test';
import { telegramInternalUrl } from '../src/telegram-api-url.js';

test('production bot internal endpoints require HTTPS and fixed path', () => {
  assert.equal(telegramInternalUrl('update', { baseUrl: 'http://api:3000/internal/telegram/update', production: true }), null);
  assert.equal(telegramInternalUrl('update', { baseUrl: 'https://api.example.test/api/internal/telegram/update', production: true })?.href, 'https://api.example.test/api/internal/telegram/update');
  assert.equal(telegramInternalUrl('codex-approve', { baseUrl: 'https://api.example.test/api/internal/telegram/update', production: true })?.href, 'https://api.example.test/api/auth/codex/telegram/codex-approve');
  assert.equal(telegramInternalUrl('chat-shared', { baseUrl: 'https://api.example.test/api/internal/telegram/update', production: true })?.href, 'https://api.example.test/api/internal/telegram/chat-shared');
  assert.equal(telegramInternalUrl('my-chat-member', { baseUrl: 'https://api.example.test/api/internal/telegram/update', production: true })?.href, 'https://api.example.test/api/internal/telegram/my-chat-member');
  assert.equal(telegramInternalUrl('update', { baseUrl: 'https://api.example.test/other', production: true }), null);
  assert.equal(telegramInternalUrl('update', { baseUrl: 'https://api.example.test/internal/telegram/update', production: true }), null);
});

test('local development permits loopback HTTP only; rejects credentials and URL modifiers', () => {
  assert.equal(telegramInternalUrl('update', { baseUrl: 'http://127.0.0.1:3000/internal/telegram/update' })?.href, 'http://127.0.0.1:3000/internal/telegram/update');
  assert.equal(telegramInternalUrl('update', { baseUrl: 'http://api:3000/internal/telegram/update' }), null);
  assert.equal(telegramInternalUrl('update', { baseUrl: 'https://user:pass@api.example.test/internal/telegram/update' }), null);
  assert.equal(telegramInternalUrl('update', { baseUrl: 'https://api.example.test/internal/telegram/update?token=x' }), null);
  assert.equal(telegramInternalUrl('update', { production: true }), null);
});
