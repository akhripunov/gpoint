import { Body, Controller, HttpCode, Post, BadRequestException } from '@nestjs/common';

function clean(value: unknown, limit = 1200) {
  return String(value ?? '').replace(/[\r\n\t]+/g, ' ').slice(0, limit);
}

@Controller('diagnostics')
export class DiagnosticsController {
  @Post('client')
  @HttpCode(204)
  client(@Body() body: { reports?: unknown }) {
    if (!body || !Array.isArray(body.reports) || body.reports.length > 8) throw new BadRequestException('invalid diagnostic payload');
    const reports = body.reports.map((item) => {
      if (!item || typeof item !== 'object') return { stage: 'INVALID' };
      const value = item as Record<string, unknown>;
      return {
        timestamp: clean(value.timestamp, 40), buildSha: clean(value.buildSha, 40), diagnosticId: clean(value.diagnosticId, 80),
        stage: clean(value.stage, 80), pathname: clean(value.pathname, 120), elapsedMs: typeof value.elapsedMs === 'number' && Number.isFinite(value.elapsedMs) ? value.elapsedMs : undefined,
        errorName: clean(value.errorName, 120), errorMessage: clean(value.errorMessage, 600), errorStack: clean(value.errorStack, 1200),
        dom: value.dom && typeof value.dom === 'object' ? value.dom : undefined,
      };
    });
    for (const report of reports) console.warn('[GPoint] client boot diagnostic', report);
  }
}
