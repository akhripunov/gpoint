import assert from 'node:assert/strict';
import test from 'node:test';
import { loadGpointChatState, revokeGpointChatConsent } from '../src/gpoint-chat-store.js';

function unavailablePrisma() {
  const fail = () => { throw new Error('GPoint consent tables must not be queried before the pilot migration'); };
  return new Proxy({}, { get: () => fail }) as never;
}

test('disabled Eros returns a closed chat state without querying unmigrated GPoint state tables', async () => {
  const previous = process.env.EROS_ENABLED;
  delete process.env.EROS_ENABLED;
  try {
    const state = await loadGpointChatState(unavailablePrisma(), '10000000-0000-4000-8000-000000000001');
    assert.equal(state.enabled, false);
    assert.equal(state.aiAvailable, false);
    assert.equal(state.chatHistory, false);
    assert.equal(state.longTermMemory, false);
  } finally {
    if (previous === undefined) delete process.env.EROS_ENABLED;
    else process.env.EROS_ENABLED = previous;
  }
});

test('disabled Eros consent revocation does not query GPoint tables before migration', async () => {
  const previous = process.env.EROS_ENABLED;
  delete process.env.EROS_ENABLED;
  try {
    const state = await revokeGpointChatConsent(unavailablePrisma(), '10000000-0000-4000-8000-000000000001', 'chat_history');
    assert.equal(state.enabled, false);
    assert.equal(state.aiAvailable, false);
  } finally {
    if (previous === undefined) delete process.env.EROS_ENABLED;
    else process.env.EROS_ENABLED = previous;
  }
});
