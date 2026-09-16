import test from 'node:test';
import assert from 'node:assert/strict';
import { ContentSanitizer } from '../src/content-sanitizer.js';
import { DeterministicSafetyProvider, isEligibleImport, sourceHash } from '../src/telegram-ingestion.js';
import { HttpSafetyProvider, UnconfiguredSafetyProvider } from '../src/safety-provider.js';
import { HttpTranslationProvider } from '../src/translation-provider.js';

test('sanitizer preserves ordinary caption and removes outbound promotion', () => {
  const result = new ContentSanitizer().sanitize('Сегодня гуляла по Москве ❤️\nБольше фото: @example\nhttps://t.me/example');
  assert.equal(result.text, 'Сегодня гуляла по Москве ❤️');
  assert.equal(result.text.includes('t.me'), false);
  assert.equal(result.text.includes('@example'), false);
});
test('import policy supports AUTO, HASHTAG, MANUAL and opt-out', () => {
  assert.equal(isEligibleImport('AUTO', null, 'hello'), true);
  assert.equal(isEligibleImport('MANUAL', null, 'hello'), true);
  assert.equal(isEligibleImport('HASHTAG', '#gpoint', 'hello #gpoint'), true);
  assert.equal(isEligibleImport('HASHTAG', '#gpoint', 'hello'), false);
  assert.equal(isEligibleImport('AUTO', null, 'hello #nogpoint'), false);
});
test('safety provider never treats explicit or uncertain markers as safe', async () => {
  const provider = new DeterministicSafetyProvider();
  assert.equal((await provider.classify({ text: '#explicit', mimeTypes: [] })).classification, 'EXPLICIT');
  assert.equal((await provider.classify({ text: '#uncertain', mimeTypes: [] })).classification, 'UNCERTAIN');
  assert.equal((await provider.classify({ text: 'ordinary', mimeTypes: [] })).classification, 'SAFE');
});
test('source versions are deterministic for translation invalidation', () => assert.equal(sourceHash('same'), sourceHash('same')));
test('unconfigured safety provider fails closed', async () => assert.equal((await new UnconfiguredSafetyProvider().classify({ text: 'ordinary', mimeTypes: [] })).classification, 'UNCERTAIN'));
test('http safety adapter maps taxonomy and low confidence to uncertain', async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response(JSON.stringify({ classification: 'SAFE', confidence: 0.95 }), { status: 200 });
    assert.equal((await new HttpSafetyProvider('http://safety.test').classify({ text: 'x', mimeTypes: [] })).classification, 'SAFE');
    globalThis.fetch = async () => new Response(JSON.stringify({ classification: 'SAFE', confidence: 0.2 }), { status: 200 });
    assert.equal((await new HttpSafetyProvider('http://safety.test').classify({ text: 'x', mimeTypes: [] })).classification, 'UNCERTAIN');
    globalThis.fetch = async () => new Response('{bad', { status: 200 });
    assert.equal((await new HttpSafetyProvider('http://safety.test').classify({ text: 'x', mimeTypes: [] })).classification, 'UNCERTAIN');
  } finally { globalThis.fetch = original; }
});
test('http translation adapter returns provider output and safe fallback', async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response(JSON.stringify({ text: 'English text' }), { status: 200 });
    const provider = new HttpTranslationProvider('http://translation.test');
    assert.deepEqual(await provider.translate('Русский', 'ru'), { text: 'English text', provider: 'external-http' });
    globalThis.fetch = async () => { throw new Error('offline'); };
    assert.deepEqual(await provider.translate('Русский', 'ru'), { text: 'Русский', provider: 'fallback-untranslated' });
  } finally { globalThis.fetch = original; }
});
