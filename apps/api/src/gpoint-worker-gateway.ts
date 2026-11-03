import { Body, Controller, Get, Headers, Header, HttpException, Post, UnauthorizedException } from '@nestjs/common';
import { timingSafeEqual } from 'node:crypto';
import {
  checkErosForGpointWorker, claimErosForGpointWorker, completeErosForGpointWorker,
  erosQueueStatusForGpointWorker, failErosForGpointWorker,
} from './eros-worker-gateway.js';
import { exactErosPilotPair } from './eros-adapter.js';

function authenticate(headers: Record<string, string | string[] | undefined>, env: NodeJS.ProcessEnv = process.env) {
  const configured = env.GPOINT_EROS_WORKER_TOKEN;
  const supplied = headers.authorization;
  if (!configured || typeof supplied !== 'string' || !supplied.startsWith('Bearer ')) throw new UnauthorizedException();
  const expected = Buffer.from(configured);
  const actual = Buffer.from(supplied.slice(7));
  if (expected.length !== actual.length || !timingSafeEqual(expected, actual)) throw new UnauthorizedException();
}

function pilotReady() {
  const pair = exactErosPilotPair();
  const policyTenant = process.env.GPOINT_CHAT_POLICY_TENANT_ID ?? '';
  const workerTenant = process.env.EROS_WORKER_TENANT_ID ?? '';
  if (!pair || !process.env.GPOINT_EROS_WORKER_TOKEN ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(policyTenant) ||
      workerTenant !== policyTenant) {
    throw new HttpException({ code: 'EROS_PILOT_RUNTIME_UNAVAILABLE' }, 503);
  }
}

@Controller('gpoint-worker')
export class GpointWorkerGatewayController {
  @Post('health')
  health(@Headers() headers: Record<string, string | string[] | undefined>) {
    authenticate(headers); pilotReady();
    return { status: 'ok', lane: 'GPOINT', provider: 'mac_product_worker' };
  }

  @Get('queue-status')
  @Header('Cache-Control', 'no-store')
  async queueStatus(@Headers() headers: Record<string, string | string[] | undefined>) {
    authenticate(headers); pilotReady();
    const counts = await erosQueueStatusForGpointWorker();
    return { ...counts, lane: 'GPOINT', source_id: 'gpoint-eros-worker-queue-status-v1:gpoint', snapshot_at: new Date().toISOString() };
  }

  @Post('claim')
  async claim(@Headers() headers: Record<string, string | string[] | undefined>) {
    authenticate(headers); pilotReady();
    return await claimErosForGpointWorker() ?? { job: null };
  }

  @Post('cancel-check')
  async cancelCheck(@Headers() headers: Record<string, string | string[] | undefined>, @Body() body: { job_id?: unknown; lease_token?: unknown }) {
    authenticate(headers); pilotReady();
    if (typeof body?.job_id !== 'string' || typeof body?.lease_token !== 'string') throw new HttpException({ code: 'INVALID_WORKER_REQUEST' }, 400);
    return checkErosForGpointWorker({ job_id: body.job_id, lease_token: body.lease_token });
  }

  @Post('complete')
  async complete(@Headers() headers: Record<string, string | string[] | undefined>, @Body() body: { job_id?: unknown; lease_token?: unknown; text?: unknown; model?: unknown; purpose?: unknown }) {
    authenticate(headers); pilotReady();
    if (typeof body?.job_id !== 'string' || typeof body.lease_token !== 'string' || typeof body.text !== 'string' ||
      typeof body.model !== 'string' || body.text.length > 4096 || body.model.length > 128 || body.purpose !== 'eros_chat_completion') {
      throw new HttpException({ code: 'INVALID_WORKER_RESULT' }, 400);
    }
    const result = await completeErosForGpointWorker({ job_id: body.job_id, lease_token: body.lease_token, text: body.text, model: body.model });
    if (!result.handled) throw new HttpException({ code: 'WORKER_LEASE_REJECTED' }, 409);
    return { completed: true };
  }

  @Post('fail')
  async fail(@Headers() headers: Record<string, string | string[] | undefined>, @Body() body: { job_id?: unknown; lease_token?: unknown; error_code?: unknown; purpose?: unknown }) {
    authenticate(headers); pilotReady();
    if (typeof body?.job_id !== 'string' || typeof body.lease_token !== 'string' || typeof body.error_code !== 'string' ||
      !/^[a-z0-9_]{1,64}$/.test(body.error_code) || body.purpose !== 'eros_chat_completion') {
      throw new HttpException({ code: 'INVALID_WORKER_FAILURE' }, 400);
    }
    const result = await failErosForGpointWorker({ job_id: body.job_id, lease_token: body.lease_token, error_code: body.error_code });
    if (!result.handled) throw new HttpException({ code: 'WORKER_LEASE_REJECTED' }, 409);
    return { accepted: true, state: 'failed' };
  }
}

export { authenticate as assertGpointWorkerAuth };
