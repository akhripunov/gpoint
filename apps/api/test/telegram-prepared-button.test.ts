import assert from 'node:assert/strict';
import test from 'node:test';
import { preparedButtonId, preparedChannelButton } from '../src/telegram-channel.js';

test('prepared channel button has required text, channel filter and complete rights', () => {
  const button = preparedChannelButton(2_147_483_647);
  assert.equal(button.text, 'Select Telegram channel');
  assert.equal(button.request_chat.chat_is_channel, true);
  assert.equal(button.request_chat.request_id, 2_147_483_647);
  assert.equal(button.request_chat.bot_administrator_rights.can_post_messages, true);
  assert.equal(button.request_chat.user_administrator_rights.can_post_messages, true);
  assert.equal(button.request_chat.user_administrator_rights.can_promote_members, true);
  for (const key of ['is_anonymous', 'can_manage_chat', 'can_delete_messages', 'can_manage_video_chats', 'can_restrict_members', 'can_promote_members', 'can_change_info', 'can_invite_users', 'can_post_stories', 'can_edit_stories', 'can_delete_stories', 'can_send_welcome_messages', 'can_post_messages']) {
    const botValue = button.request_chat.bot_administrator_rights[key as keyof typeof button.request_chat.bot_administrator_rights];
    const userValue = button.request_chat.user_administrator_rights[key as keyof typeof button.request_chat.user_administrator_rights];
    assert.equal(typeof botValue, 'boolean');
    assert.equal(typeof userValue, 'boolean');
    assert.ok(Number(userValue) >= Number(botValue));
  }
});

test('prepared request id is signed 32-bit and PreparedKeyboardButton id is extracted', () => {
  assert.throws(() => preparedChannelButton(2_147_483_648), /signed 32-bit/);
  assert.throws(() => preparedChannelButton(-2_147_483_649), /signed 32-bit/);
  assert.equal(preparedButtonId({ id: 'prepared-test' }), 'prepared-test');
  assert.throws(() => preparedButtonId({ id: '' }), /invalid PreparedKeyboardButton/);
  assert.throws(() => preparedButtonId({}), /invalid PreparedKeyboardButton/);
});
