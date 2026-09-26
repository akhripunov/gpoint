import { AgataClient } from '@gpoint/agata-client';

/** One API-side construction point; callers never access agata.* via SQL. */
export function createAgataClient(env: NodeJS.ProcessEnv = process.env): AgataClient | null {
  const baseUrl = env.AGATA_SERVICE_URL;
  const token = env.AGATA_INTERNAL_API_TOKEN;
  if (!baseUrl && !token) return null;
  if (!baseUrl || !token) throw new Error('AGATA internal client configuration is incomplete');
  return new AgataClient({ baseUrl, token, timeoutMs: 15_000 });
}
