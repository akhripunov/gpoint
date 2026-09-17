import { createHmac } from 'node:crypto';

export type ImageVariant = 'THUMBNAIL' | 'FEED' | 'PROFILE' | 'ORIGINAL';
const widths: Record<Exclude<ImageVariant, 'ORIGINAL'>, number> = { THUMBNAIL: 320, FEED: 640, PROFILE: 480 };

function encodedSource(id: string, env: NodeJS.ProcessEnv) {
  const base = env.IMGPROXY_SOURCE_BASE_URL?.replace(/\/$/, '');
  if (!base) return undefined;
  return `${base}/media/assets/${encodeURIComponent(id)}`;
}

/** Single boundary for public image delivery. Private assets never call this helper. */
export function imageUrl(id: string, variant: ImageVariant, env: NodeJS.ProcessEnv = process.env) {
  if (variant === 'ORIGINAL' || env.IMGPROXY_ENABLED !== 'true') return `/media/assets/${encodeURIComponent(id)}`;
  const endpoint = env.IMGPROXY_URL?.replace(/\/$/, '');
  const source = encodedSource(id, env);
  const key = env.IMGPROXY_KEY;
  const salt = env.IMGPROXY_SALT;
  if (!endpoint || !source || !key || !salt) return `/media/assets/${encodeURIComponent(id)}`;
  const width = widths[variant];
  const processing = `/rs:fit:${width}:0:0/g:sm/plain/${encodeURIComponent(source)}@webp`;
  const raw = Buffer.from(processing);
  const signature = createHmac('sha256', Buffer.from(key, 'hex')).update(Buffer.from(salt, 'hex')).update(raw).digest('base64url');
  return `${endpoint}/${signature}${processing}`;
}

export function mediaOptimizationStatus(env: NodeJS.ProcessEnv = process.env) {
  const enabled = env.IMGPROXY_ENABLED === 'true';
  return { enabled, healthy: enabled ? Boolean(env.IMGPROXY_URL && env.IMGPROXY_KEY && env.IMGPROXY_SALT && env.IMGPROXY_SOURCE_BASE_URL) : true, mode: enabled ? 'imgproxy-or-original-fallback' : 'original-fallback' };
}
