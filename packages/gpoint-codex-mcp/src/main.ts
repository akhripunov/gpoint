import { serveStdio } from '@modelcontextprotocol/server/stdio';
import { buildServer } from './index.js';

void serveStdio(buildServer);
console.error('GPoint Eros MCP server running on stdio');
