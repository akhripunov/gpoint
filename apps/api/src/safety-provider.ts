import type { ContentSafetyProvider, SafetyClassification } from './telegram-ingestion.js';
import sharp from 'sharp';

const classifications = new Set<SafetyClassification>(['SAFE', 'SUGGESTIVE', 'EROTIC', 'EXPLICIT', 'UNCERTAIN']);
export class UnconfiguredSafetyProvider implements ContentSafetyProvider { async classify(_input: { text: string; mimeTypes: string[] }) { return { classification: 'UNCERTAIN' as const, reason: 'real safety provider is not configured' }; } }
export class HttpSafetyProvider implements ContentSafetyProvider {
  constructor(private readonly endpoint: string, private readonly secret?: string, private readonly timeoutMs = 10000) {}
  async classify(input: { text: string; mimeTypes: string[] }) { const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), this.timeoutMs); try { const response = await fetch(this.endpoint, { method: 'POST', headers: { 'content-type': 'application/json', ...(this.secret ? { authorization: `Bearer ${this.secret}` } : {}) }, body: JSON.stringify(input), signal: controller.signal }); if (!response.ok) throw new Error(`safety provider ${response.status}`); const body = await response.json() as { classification?: string; reason?: string; confidence?: number }; if (!body.classification || !classifications.has(body.classification as SafetyClassification)) throw new Error('malformed safety response'); if (typeof body.confidence === 'number' && body.confidence < 0.8) return { classification: 'UNCERTAIN' as const, reason: 'safety provider confidence below threshold' }; return { classification: body.classification as SafetyClassification, reason: body.reason ?? 'external safety provider' }; } catch { return { classification: 'UNCERTAIN' as const, reason: 'safety provider unavailable or malformed' }; } finally { clearTimeout(timer); } }
}
export class MisskeySensitiveDetectorProvider implements ContentSafetyProvider {
  constructor(private readonly endpoint: string, private readonly apiKey?: string, private readonly timeoutMs = 60_000) {}
  async classify(input: { text: string; mimeTypes: string[]; imageBytes?: Buffer }) {
    if (!input.imageBytes || !input.mimeTypes.some(x => x.startsWith('image/'))) return { classification: 'UNCERTAIN' as const, reason: 'image bytes required by sensitive-detector', provider: 'misskey-sensitive-detector' };
    const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const normalized = await sharp(input.imageBytes, { limitInputPixels: 40_000_000 }).rotate().resize(299, 299, { fit: 'contain', background: { r: 255, g: 255, b: 255, alpha: 1 } }).png().toBuffer();
      const form = new FormData(); form.append('image0', new File([new Uint8Array(normalized).slice()], 'image.png', { type: 'image/png' }));
      const response = await fetch(`${this.endpoint.replace(/\/$/, '')}/v1/detect-images`, { method: 'POST', headers: this.apiKey ? { authorization: `Bearer ${this.apiKey}` } : {}, body: form, signal: controller.signal });
      if (!response.ok) throw new Error(`detector ${response.status}`);
      const body = await response.json() as { result?: { results?: Array<{ predictions?: Array<{ className?: string; probability?: number }> | Record<string, number>; error?: { code?: string } }> }; results?: Array<{ predictions?: Array<{ className?: string; probability?: number }> | Record<string, number>; error?: { code?: string } }> };
      const result = (body.result?.results ?? body.results)?.[0]; if (!result?.predictions || result.error) throw new Error('malformed detector response');
      const predictions = Array.isArray(result.predictions) ? result.predictions.map(item => [item.className?.toLowerCase() ?? '', Number(item.probability)] as const) : Object.entries(result.predictions).map(([label, value]) => [label.toLowerCase(), Number(value)] as const);
      const max = predictions.filter(([label, value]) => Boolean(label) && Number.isFinite(value)).sort((a, b) => b[1] - a[1])[0];
      if (!max || max[1] < 0.8) return { classification: 'UNCERTAIN' as const, reason: 'detector confidence below threshold', confidence: max?.[1], provider: 'misskey-sensitive-detector' };
      const classification = /porn|explicit|hentai/.test(max[0]) ? 'EXPLICIT' : /sexy|erotic|sensitive/.test(max[0]) ? 'SUGGESTIVE' : 'SAFE';
      return { classification: classification as SafetyClassification, reason: `detector:${max[0]}`, confidence: max[1], provider: 'misskey-sensitive-detector' };
    } catch { return { classification: 'UNCERTAIN' as const, reason: 'sensitive-detector unavailable or malformed', provider: 'misskey-sensitive-detector', failureReason: 'provider_unavailable' }; } finally { clearTimeout(timer); }
  }
}
export function safetyProviderFromEnv(env: NodeJS.ProcessEnv = process.env): ContentSafetyProvider { if (env.GPOINT_SAFETY_PROVIDER === 'misskey-sensitive-detector' && env.GPOINT_SAFETY_URL) return new MisskeySensitiveDetectorProvider(env.GPOINT_SAFETY_URL, env.GPOINT_SAFETY_API_KEY); return env.GPOINT_SAFETY_URL ? new HttpSafetyProvider(env.GPOINT_SAFETY_URL, env.GPOINT_SAFETY_API_KEY) : new UnconfiguredSafetyProvider(); }
