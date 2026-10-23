import { BadRequestException, Body, Controller, HttpCode, Post, Req } from '@nestjs/common';

const WINDOW_MS = 60_000;
const MAX_PEERS = 1024;
const MAX_BATCH_BYTES = 32 * 1024;

function ownData(value: unknown, key: string): unknown {
  if (!value || typeof value !== 'object') return undefined;
  const descriptor = Object.getOwnPropertyDescriptor(value, key);
  return descriptor && 'value' in descriptor ? descriptor.value : undefined;
}

function text(value: unknown, limit = 1200): string {
  if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean') return '';
  return String(value).replace(/[\r\n\t]+/g, ' ').slice(0, limit);
}

function number(value: unknown, max = 100_000): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(-max, Math.min(max, value)) : undefined;
}

function rectangle(value: unknown) {
  if (!value || typeof value !== 'object') return null;
  return { x: number(ownData(value, 'x')), y: number(ownData(value, 'y')), width: number(ownData(value, 'width')), height: number(ownData(value, 'height')) };
}

function dom(value: unknown) {
  if (!value || typeof value !== 'object') return undefined;
  const style = (input: unknown, center = false) => {
    if (!input || typeof input !== 'object') return null;
    const fields = ['display', 'visibility', 'opacity', ...(center ? ['zIndex'] : ['position', 'zIndex', 'width', 'height', 'overflow'])];
    return Object.fromEntries(fields.map((field) => [field, text(ownData(input, field), 32)]));
  };
  const center = ownData(value, 'centerElement');
  return {
    readyState: text(ownData(value, 'readyState'), 16),
    bodyChildElementCount: number(ownData(value, 'bodyChildElementCount'), 5000),
    rootExists: typeof ownData(value, 'rootExists') === 'boolean' ? ownData(value, 'rootExists') : undefined,
    rootChildElementCount: number(ownData(value, 'rootChildElementCount'), 5000),
    rootHtmlLength: number(ownData(value, 'rootHtmlLength'), 10_000_000),
    rootRect: rectangle(ownData(value, 'rootRect')),
    bodyRect: rectangle(ownData(value, 'bodyRect')),
    innerWidth: number(ownData(value, 'innerWidth'), 100_000), innerHeight: number(ownData(value, 'innerHeight'), 100_000),
    clientWidth: number(ownData(value, 'clientWidth'), 100_000), clientHeight: number(ownData(value, 'clientHeight'), 100_000),
    appStyle: style(ownData(value, 'appStyle')),
    centerElement: center && typeof center === 'object' ? {
      tag: text(ownData(center, 'tag'), 16), id: text(ownData(center, 'id'), 80), className: text(ownData(center, 'className'), 120),
      rect: rectangle(ownData(center, 'rect')), ...style(center, true),
    } : null,
  };
}

function available(bucket: { startedAt: number; count: number }, now: number, limit: number) {
  if (now - bucket.startedAt >= WINDOW_MS || now < bucket.startedAt) { bucket.startedAt = now; bucket.count = 0; }
  return bucket.count < limit;
}

@Controller('diagnostics')
export class DiagnosticsController {
  private readonly globalBucket = { startedAt: 0, count: 0 };
  private readonly peerBuckets = new Map<string, { startedAt: number; count: number }>();
  @Post('client')
  @HttpCode(204)
  client(@Body() body: unknown, @Req() request: { raw?: { socket?: { remoteAddress?: string } }; socket?: { remoteAddress?: string } }) {
    const input = ownData(body, 'reports');
    if (!Array.isArray(input) || input.length > 8) throw new BadRequestException('invalid diagnostic payload');

    const now = Date.now();
    // Use the connection peer only; forwarded headers are client-controlled. A shared
    // reverse proxy therefore shares one deliberate per-peer diagnostic budget.
    const address = request?.raw?.socket?.remoteAddress ?? request?.socket?.remoteAddress;
    const key = typeof address === 'string' && address.length > 0 && address.length <= 128 ? address : '<unknown-peer>';
    let bucket = this.peerBuckets.get(key);
    if (!bucket) {
      if (this.peerBuckets.size >= MAX_PEERS) this.peerBuckets.delete(this.peerBuckets.keys().next().value!);
      bucket = { startedAt: now, count: 0 };
      this.peerBuckets.set(key, bucket);
    } else {
      this.peerBuckets.delete(key);
      this.peerBuckets.set(key, bucket);
    }
    if (!available(bucket, now, 5) || !available(this.globalBucket, now, 30)) return;
    bucket.count++;
    this.globalBucket.count++;

    const reports = input.map((item) => {
      if (!item || typeof item !== 'object') return { stage: 'INVALID' };
      const elapsed = number(ownData(item, 'elapsedMs'), 86_400_000);
      return {
        timestamp: text(ownData(item, 'timestamp'), 40), buildSha: text(ownData(item, 'buildSha'), 40), diagnosticId: text(ownData(item, 'diagnosticId'), 80),
        stage: text(ownData(item, 'stage'), 80), pathname: text(ownData(item, 'pathname'), 120), elapsedMs: elapsed,
        errorName: text(ownData(item, 'errorName'), 120),
        dom: dom(ownData(item, 'dom')),
      };
    });
    while (reports.length && Buffer.byteLength(JSON.stringify(reports)) > MAX_BATCH_BYTES) {
      const largest = reports.reduce((best, report, index) => JSON.stringify(report).length > JSON.stringify(reports[best]).length ? index : best, 0);
      const report = reports[largest] as Record<string, unknown>;
      if (report.dom) report.dom = undefined;
      else reports.pop();
    }
    for (const report of reports) console.warn('[GPoint] client boot diagnostic', report);
  }
}
