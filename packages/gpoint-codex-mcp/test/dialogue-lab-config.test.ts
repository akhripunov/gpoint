import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const root = new URL('../../../', import.meta.url);
const tools = [
  'connect_gpoint',
  'finish_gpoint_connection',
  'get_gpoint_connection_status',
  'read_gpoint_history',
  'send_gpoint_message',
  'resume_pending_gpoint_message',
  'begin_models_hub_scenario',
  'send_models_hub_scenario_turn',
  'stop_models_hub_scenario',
  'disconnect_gpoint',
];

test('the existing GPoint MCP entry exposes the Dialogue Lab modes without a second server', () => {
  const config = readFileSync(new URL('.codex/config.toml', root), 'utf8');
  assert.equal((config.match(/^\[mcp_servers\./gm) ?? []).length, 1);
  assert.match(config, /^\[mcp_servers\.gpoint_agata\]$/m);
  assert.match(config, /^env_vars = \["GPOINT_API_URL"\]$/m);
  const raw = config.match(/^enabled_tools = (\[.*\])$/m)?.[1];
  assert.ok(raw);
  assert.deepEqual(JSON.parse(raw), tools);

  const implementation = readFileSync(new URL('packages/gpoint-codex-mcp/src/index.ts', root), 'utf8');
  const registered = [...implementation.matchAll(/registerTool\('([^']+)'/g)].map((match) => match[1]);
  assert.deepEqual(registered, tools);
});
