import assert from 'node:assert/strict';
import test from 'node:test';
import { creatorChannelAvatarSource, messageChannelAvatarSource, shouldShowAvatarImage } from '../src/avatar-display.js';

test('avatar image is shown while its URL is available', () => {
  assert.equal(shouldShowAvatarImage('/media/assets/channel-a', null), true);
});

test('missing and failed avatar URLs use the initials fallback', () => {
  assert.equal(shouldShowAvatarImage(null, null), false);
  assert.equal(shouldShowAvatarImage('', null), false);
  assert.equal(shouldShowAvatarImage('/media/assets/unavailable', '/media/assets/unavailable'), false);
});

test('a refreshed avatar URL recovers after the previous image failed', () => {
  assert.equal(shouldShowAvatarImage('/media/assets/new-channel-photo', '/media/assets/old-channel-photo'), true);
});

test('Messages uses only the linked Telegram channel avatar, never a persona profile avatar', () => {
  assert.equal(messageChannelAvatarSource({ channelAvatarUrl: '/media/assets/channel', avatarUrl: '/media/assets/profile' }), '/media/assets/channel');
  assert.equal(messageChannelAvatarSource({ channelAvatarUrl: null, avatarUrl: '/media/assets/profile' }), null);
});

test('creator profile uses only the linked Telegram channel avatar, never the persona profile avatar', () => {
  assert.equal(creatorChannelAvatarSource({ channelAvatarUrl: '/media/assets/channel', avatarUrl: '/media/assets/profile' }), '/media/assets/channel');
  assert.equal(creatorChannelAvatarSource({ channelAvatarUrl: null, avatarUrl: '/media/assets/profile' }), null);
});
