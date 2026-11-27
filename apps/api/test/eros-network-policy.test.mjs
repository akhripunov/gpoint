import test from 'node:test';
import assert from 'node:assert/strict';
import { isAllowedSyntheticEmbeddingEndpoint } from '../src/eros-network-policy.js';

test('production cannot enable the synthetic embedding endpoint', () => {
  assert.equal(isAllowedSyntheticEmbeddingEndpoint('http://127.0.0.1:11434/api/embed'), false);
});

test('disposable local test mode permits only its loopback synthetic endpoint', () => {
  assert.equal(isAllowedSyntheticEmbeddingEndpoint('http://127.0.0.1:11434/api/embed', { localTest: true }), true);
  for (const value of [
    'https://example.com/api/embed',
    'http://localhost:11434/api/embed',
    'http://172.31.254.2:11434/api/embed',
    'http://172.31.254.1:11434/api/embed',
    'http://127.0.0.1:11435/api/embed',
    'http://user:pass@127.0.0.1:11434/api/embed',
    'http://127.0.0.1:11434/api/embed?redirect=https://example.com',
    'http://127.0.0.1:11434/api/tags',
    'not a URL',
  ]) {
    assert.equal(isAllowedSyntheticEmbeddingEndpoint(value, { localTest: true }), false, value);
  }
});
