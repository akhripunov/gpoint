const DEFAULT_INGEST_URL = 'http://127.0.0.1:3000/internal/telegram/update';
const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]']);

export function telegramInternalUrl(path, options = {}) {
  const raw = options.baseUrl ?? (options.production ? undefined : DEFAULT_INGEST_URL);
  if (!raw) return null;
  try {
    const url = new URL(raw);
    const isLoopbackHttp = url.protocol === 'http:' && LOOPBACK.has(url.hostname);
    if (url.protocol !== 'https:' && !(isLoopbackHttp && !options.production)) return null;
    const apiPrefix = options.production ? '/api' : '';
    if (url.username || url.password || url.search || url.hash || url.pathname !== `${apiPrefix}/internal/telegram/update`) return null;
    url.pathname = path === 'codex-approve'
      ? `${apiPrefix}/auth/codex/telegram/codex-approve`
      : `${apiPrefix}/internal/telegram/${path}`;
    return url;
  } catch {
    return null;
  }
}
