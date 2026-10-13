import { serveStdio } from '@modelcontextprotocol/server/stdio';
import { buildServer } from './index.js';

void serveStdio(buildServer);
console.error('GPoint AGATA MCP server running on stdio');
