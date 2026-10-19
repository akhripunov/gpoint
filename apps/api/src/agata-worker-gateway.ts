import { timingSafeEqual } from 'node:crypto';
import { Body, Controller, Get, Headers, Header, HttpException, Post, UnauthorizedException } from '@nestjs/common';
import { createAgataClient } from './agata-client.js';
import { claimErosForGpointWorker, checkErosForGpointWorker, completeErosForGpointWorker, erosQueueStatusForGpointWorker, failErosForGpointWorker } from './eros-worker-gateway.js';

function assertWorkerAuth(
  headers: Record<string, string | string[] | undefined>,
  env: NodeJS.ProcessEnv = process.env,
) {
  const configured = env.GPOINT_AGATA_WORKER_TOKEN;
  const supplied = headers.authorization;
  if (!configured || typeof supplied !== 'string' || !supplied.startsWith('Bearer ')) {
    throw new UnauthorizedException();
  }
  const expected = Buffer.from(configured);
  const actual = Buffer.from(supplied.slice(7));
  if (expected.length !== actual.length || !timingSafeEqual(expected, actual)) {
    throw new UnauthorizedException();
  }
}

function internalClient() {
  const client = createAgataClient();
  if (!client) throw new HttpException({ code: 'AGATA_UNAVAILABLE' }, 503);
  return client;
}

@Controller('agata-worker')
export class AgataWorkerGatewayController {
  protected readAgataQueueStatus() {
    return internalClient().getWorkerQueueStatus('GPOINT');
  }

  protected readErosQueueStatus() {
    return process.env.EROS_ENABLED === 'true'
      ? erosQueueStatusForGpointWorker()
      : Promise.resolve({ pending_jobs: 0, running_jobs: 0 });
  }

  protected claimAgataJob() {
    return internalClient().claimInferenceJob();
  }

  protected claimErosJob() {
    return claimErosForGpointWorker();
  }

  @Get('queue-status')
  @Header('Cache-Control', 'no-store')
  async queueStatus(@Headers() headers: Record<string, string | string[] | undefined>) {
    assertWorkerAuth(headers);
    try {
      const agata = await this.readAgataQueueStatus();
      let eros = { pending_jobs: 0, running_jobs: 0 };
      let erosQueueAvailable = process.env.EROS_ENABLED !== 'true';
      if (process.env.EROS_ENABLED === 'true') {
        try { eros = await this.readErosQueueStatus(); erosQueueAvailable = true; }
        catch { /* Eros must not hide health of the ordinary GPOINT queue. */ }
      }
      return {
        ...agata,
        pending_jobs: agata.pending_jobs + eros.pending_jobs,
        running_jobs: agata.running_jobs + eros.running_jobs,
        eros_pending_jobs: eros.pending_jobs,
        eros_running_jobs: eros.running_jobs,
        eros_queue_available: erosQueueAvailable,
      };
    } catch {
      throw new HttpException({ code: 'AGATA_UNAVAILABLE' }, 503);
    }
  }

  @Post('health')
  async health(@Headers() headers: Record<string, string | string[] | undefined>) {
    assertWorkerAuth(headers);
    return internalClient().ready();
  }

  @Post('claim')
  async claim(@Headers() headers: Record<string, string | string[] | undefined>) {
    assertWorkerAuth(headers);
    if (process.env.EROS_ENABLED !== 'true') return this.claimAgataJob();
    try {
      const agata = await this.claimAgataJob();
      if (agata?.job) return agata;
    } catch {
      throw new HttpException({ code: 'AGATA_UNAVAILABLE' }, 503);
    }
    // Eros is opportunistic on the shared lane: do not let its database or
    // configuration failures block normal GPOINT responses.
    try { return (await this.claimErosJob()) ?? { job: null }; }
    catch { return { job: null }; }
  }

  @Post('cancel-check')
  async cancelCheck(
    @Headers() headers: Record<string, string | string[] | undefined>,
    @Body() body: { job_id?: unknown; lease_token?: unknown },
  ) {
    assertWorkerAuth(headers);
    if (typeof body?.job_id !== 'string' || typeof body.lease_token !== 'string') {
      throw new HttpException({ code: 'INVALID_WORKER_REQUEST' }, 400);
    }
    if (process.env.EROS_ENABLED !== 'true') return { active: false, terminal_status: 'EXPIRED' };
    try { return await checkErosForGpointWorker({ job_id: body.job_id, lease_token: body.lease_token }); }
    catch { throw new HttpException({ code: 'AGATA_UNAVAILABLE' }, 503); }
  }

  @Post('complete')
  async complete(
    @Headers() headers: Record<string, string | string[] | undefined>,
    @Body() body: { job_id?: unknown; lease_token?: unknown; text?: unknown; model?: unknown; purpose?: unknown },
  ) {
    assertWorkerAuth(headers);
    if (
      typeof body?.job_id !== 'string' || typeof body.lease_token !== 'string' ||
      typeof body.text !== 'string' || typeof body.model !== 'string' ||
      body.text.length > 4_096 || body.model.length > 128 ||
      (body.purpose !== undefined && typeof body.purpose !== 'string')
    ) throw new HttpException({ code: 'INVALID_WORKER_RESULT' }, 400);
    try {
      if (body.purpose === 'eros_chat_completion') {
        if (process.env.EROS_ENABLED !== 'true') throw new HttpException({ code: 'EROS_BRIDGE_UNAVAILABLE' }, 503);
        const eros = await completeErosForGpointWorker({
          job_id: body.job_id, lease_token: body.lease_token, text: body.text, model: body.model,
        });
        if (!eros.handled) throw new HttpException({ code: 'WORKER_LEASE_REJECTED' }, 409);
        return { completed: true };
      }
      return await internalClient().completeInferenceJob({
        job_id: body.job_id, lease_token: body.lease_token, text: body.text, model: body.model,
      });
    } catch (error) {
      if (error instanceof HttpException) throw error;
      throw new HttpException({ code: 'AGATA_UNAVAILABLE' }, 503);
    }
  }

  @Post('fail')
  async fail(
    @Headers() headers: Record<string, string | string[] | undefined>,
    @Body() body: { job_id?: unknown; lease_token?: unknown; error_code?: unknown; purpose?: unknown },
  ) {
    assertWorkerAuth(headers);
    if (
      typeof body?.job_id !== 'string' || typeof body.lease_token !== 'string' ||
      typeof body.error_code !== 'string' || !/^[a-z0-9_]{1,64}$/.test(body.error_code) ||
      (body.purpose !== undefined && typeof body.purpose !== 'string')
    ) throw new HttpException({ code: 'INVALID_WORKER_FAILURE' }, 400);
    try {
      if (body.purpose === 'eros_chat_completion') {
        if (process.env.EROS_ENABLED !== 'true') throw new HttpException({ code: 'EROS_BRIDGE_UNAVAILABLE' }, 503);
        const eros = await failErosForGpointWorker({
          job_id: body.job_id, lease_token: body.lease_token, error_code: body.error_code,
        });
        if (!eros.handled) throw new HttpException({ code: 'WORKER_LEASE_REJECTED' }, 409);
        return { accepted: true, state: 'failed' };
      }
      return await internalClient().failInferenceJob({
        job_id: body.job_id, lease_token: body.lease_token, error_code: body.error_code,
      });
    } catch (error) {
      if (error instanceof HttpException) throw error;
      throw new HttpException({ code: 'AGATA_UNAVAILABLE' }, 503);
    }
  }
}

export { assertWorkerAuth };
