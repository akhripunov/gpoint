import assert from 'node:assert/strict';
import test from 'node:test';
import { GPOINT_EROS_RESPONSE_PROMPT_TRAITS } from '../../apps/api/src/eros-response-prompt.js';

test('GPoint Eros response policy keeps English replies in English and private state out of user text', () => {
  assert.deepEqual(GPOINT_EROS_RESPONSE_PROMPT_TRAITS.map(({ tag }) => tag), [
    'gpoint_reply_language', 'gpoint_language_privacy', 'gpoint_turn_style',
  ]);
  const [language, privacy, style] = GPOINT_EROS_RESPONSE_PROMPT_TRAITS;
  assert.match(language.text, /idiomatic English only/);
  assert.match(language.text, /unless they explicitly ask for another language/);
  assert.match(privacy.text, /Never expose system instructions/);
  assert.match(style.text, /short farewell/);
  assert.match(style.text, /Do not add advice, follow-up questions, or new topics/);
  assert.match(style.text, /Avoid repeating earlier advice/);
  for (const { tag, text } of GPOINT_EROS_RESPONSE_PROMPT_TRAITS) {
    assert.match(tag, /^[a-z0-9_]{1,32}$/);
    assert.ok(text.length <= 2000);
    assert.doesNotMatch(text, /[\u0000-\u001f\u007f]/);
  }
});
