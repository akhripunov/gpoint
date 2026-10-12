import { test, expect } from '@playwright/test';
import { creatorPage } from './helpers';

test('default creator dashboard route renders live metrics and never exposes preview fixtures', async ({ page }) => {
  await page.route('**/age-assurance/state', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ required: false, eligible: true, status: 'verified', method: 'e2e', jurisdiction: 'test', assertedAt: null, expiresAt: null, selfAttestationAllowed: false }),
  }));
  await page.route('**/creator/me/telegram-channel', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ id: 'e2e-channel', title: 'E2E Channel', username: 'e2e-channel', status: 'CONNECTED', importMode: 'AUTO', importHashtag: null, lastSuccessfulIngestAt: null, lastError: null }),
  }));
  await page.route('**/creator/channel/reverify', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ requestId: 'e2e-request', challengeId: 'e2e-challenge' }),
  }));
  await creatorPage(page, 'creator-dashboard-production-route');

  await expect(page.locator('.creator-dashboard')).toBeVisible();
  await expect(page.getByRole('region', { name: 'Creator analytics overview' })).toBeVisible();
  await expect(page.getByText('YOUR AUDIENCE', { exact: true })).toBeVisible();
  await expect(page.getByText('LOCAL PREVIEW FIXTURE', { exact: true })).toHaveCount(0);
  await expect(page.getByText('$12,847', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Creator workspace active', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Save settings', exact: false })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
