import type { Server } from 'node:http';

export function startBotReadinessServer(isReady: () => boolean, port?: number): Server;
