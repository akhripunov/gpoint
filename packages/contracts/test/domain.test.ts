import test from 'node:test';
import assert from 'node:assert/strict';
import { affiliateCommission, parseAffiliateUrl } from '../src/index.js';

test('calculates permille commission using floor semantics for expected display', () => {
  assert.equal(affiliateCommission(999, 200), 199);
  assert.equal(affiliateCommission(1000, 200), 200);
});

test('rejects non-Telegram or referral-less URLs', () => {
  assert.throws(() => parseAffiliateUrl('https://example.com/?ref=x'));
  assert.throws(() => parseAffiliateUrl('https://t.me/model_bot'));
  assert.doesNotThrow(() => parseAffiliateUrl('https://t.me/model_bot?startapp=affiliate_x'));
});
