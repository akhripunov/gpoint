import test from 'node:test';
import assert from 'node:assert/strict';
import { isAllowedOllamaEmbeddingEndpoint, isDisposableSyntheticLocalTest } from '../src/eros-network-policy.js';

test('production permits only the private Ollama Compose endpoint', () => {
  assert.equal(isAllowedOllamaEmbeddingEndpoint('http://ollama:11434/api/embed'), true);
});

test('production rejects public hosts, alternate services, and URL injection', () => {
  for (const value of [
    'https://example.com/api/embed',
    'http://127.0.0.1:11434/api/embed',
    'http://localhost:11434/api/embed',
    'http://ollama:11435/api/embed',
    'http://user:pass@ollama:11434/api/embed',
    'http://ollama:11434/api/embed?redirect=https://example.com',
    'http://ollama:11434/api/tags',
    'not a URL',
  ]) {
    assert.equal(isAllowedOllamaEmbeddingEndpoint(value), false, value);
  }
});

test('local test mode permits loopback and keeps production hostname restricted', () => {
  assert.equal(isAllowedOllamaEmbeddingEndpoint('http://127.0.0.1:11434/api/embed', { localTest: true }), true);
  assert.equal(isAllowedOllamaEmbeddingEndpoint('http://ollama:11414/api/embed', { localTest: true }), false);
  assert.equal(isAllowedOllamaEmbeddingEndpoint('http://example.com:11434/api/embed', { localTest: true }), false);
});

test('loopback embedding exception requires the complete disposable synthetic test mode', () => {
  const valid = {
    NODE_ENV: 'development', EROS_DATA_MODE: 'synthetic',
    EROS_WORKER_LOCAL_TEST: 'true', EROS_WORKER_DISPOSABLE_TEST_DB: '1',
  };
  assert.equal(isDisposableSyntheticLocalTest(valid), true);
  for (const patch of [
    { NODE_ENV: 'production' },
    { EROS_DATA_MODE: 'production' },
    { EROS_WORKER_LOCAL_TEST: 'false' },
    { EROS_WORKER_DISPOSABLE_TEST_DB: '0' },
  ]) {
    assert.equal(isDisposableSyntheticLocalTest({ ...valid, ...patch }), false);
  }
});
