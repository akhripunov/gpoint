import test from 'node:test';
import assert from 'node:assert/strict';
import { ContentSanitizer } from '../src/content-sanitizer.js';
import { DeterministicSafetyProvider, isEligibleImport, sourceHash } from '../src/telegram-ingestion.js';

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
