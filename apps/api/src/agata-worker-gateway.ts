import { timingSafeEqual } from 'node:crypto';
import { Body, Controller, Headers, HttpException, Post, UnauthorizedException } from '@nestjs/common';
import { createAgataClient } from './agata-client.js';

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
  @Post('health')
  async health(@Headers() headers: Record<string, string | string[] | undefined>) {
    assertWorkerAuth(headers);
    return internalClient().ready();
  }

  @Post('claim')
  async claim(@Headers() headers: Record<string, string | string[] | undefined>) {
    assertWorkerAuth(headers);
    return internalClient().claimInferenceJob();
  }

  @Post('complete')
  async complete(
    @Headers() headers: Record<string, string | string[] | undefined>,
    @Body() body: { job_id?: unknown; lease_token?: unknown; text?: unknown; model?: unknown },
  ) {
    assertWorkerAuth(headers);
    if (
      typeof body?.job_id !== 'string' || typeof body.lease_token !== 'string' ||
      typeof body.text !== 'string' || typeof body.model !== 'string' ||
      body.text.length > 4_096 || body.model.length > 128
    ) throw new HttpException({ code: 'INVALID_WORKER_RESULT' }, 400);
    return internalClient().completeInferenceJob({
      job_id: body.job_id,
      lease_token: body.lease_token,
      text: body.text,
      model: body.model,
    });
  }

  @Post('fail')
  async fail(
    @Headers() headers: Record<string, string | string[] | undefined>,
    @Body() body: { job_id?: unknown; lease_token?: unknown; error_code?: unknown },
  ) {
    assertWorkerAuth(headers);
    if (
      typeof body?.job_id !== 'string' || typeof body.lease_token !== 'string' ||
      typeof body.error_code !== 'string' || !/^[a-z0-9_]{1,64}$/.test(body.error_code)
    ) throw new HttpException({ code: 'INVALID_WORKER_FAILURE' }, 400);
    return internalClient().failInferenceJob({
      job_id: body.job_id,
      lease_token: body.lease_token,
      error_code: body.error_code,
    });
  }
}

export { assertWorkerAuth };
