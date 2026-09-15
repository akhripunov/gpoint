import { Queue } from 'bullmq';
const redisUrl = process.env.REDIS_URL;
if (!redisUrl) { console.error('REDIS_URL is required'); process.exitCode = 1; } else { new Queue('gpoint', { connection: { url: redisUrl } as never }); }
