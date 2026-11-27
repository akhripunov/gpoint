/** Restrict the deterministic embedding fixture to disposable local tests. */
export function isDisposableSyntheticLocalTest(env = process.env) {
  return env.NODE_ENV === 'development' && env.EROS_DATA_MODE === 'synthetic' &&
    env.EROS_WORKER_LOCAL_TEST === 'true' && env.EROS_WORKER_DISPOSABLE_TEST_DB === '1';
}

export function isAllowedSyntheticEmbeddingEndpoint(value, { localTest = false } = {}) {
  if (!localTest) return false;
  if (typeof value !== 'string' || value.length > 256) return false;
  let url;
  try { url = new URL(value); } catch { return false; }
  if (url.username || url.password || url.search || url.hash || url.pathname !== '/api/embed') return false;
  if (url.protocol !== 'http:' || url.port !== '11434') return false;
  return url.hostname === '127.0.0.1';
}
