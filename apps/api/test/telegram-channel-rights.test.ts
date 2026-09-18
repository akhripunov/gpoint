import assert from 'node:assert/strict';
import test from 'node:test';
import { botCanPost, telegramApiTimeoutMs, userIsChannelAdmin } from '../src/telegram-channel.js';

test('ordinary channel administrator is accepted without promote-members permission', () => {
  assert.equal(userIsChannelAdmin({ status: 'administrator', can_manage_chat: true, can_promote_members: false }), true);
  assert.equal(userIsChannelAdmin({ status: 'administrator', can_post_messages: true, can_promote_members: false }), true);
});

test('channel selector only accepts channels and server checks bot state separately', () => {
  assert.equal(userIsChannelAdmin({ status: 'member' }), false);
  assert.equal(botCanPost(null), false);
  assert.equal(botCanPost({ status: 'administrator', can_post_messages: false }), false);
  assert.equal(botCanPost({ status: 'administrator', can_post_messages: true }), true);
  assert.equal(botCanPost({ status: 'creator', can_post_messages: true }), true);
});

test('Telegram preparation transport has a bounded timeout below the client watchdog', () => {
  assert.ok(telegramApiTimeoutMs <= 8_000);
});
