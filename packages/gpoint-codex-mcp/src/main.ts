import { serveStdio } from '@modelcontextprotocol/server/stdio';
import { buildServer } from './index.js';

void serveStdio(buildServer);
console.error('GPoint Eros Dialogue Lab MCP server running on stdio');
