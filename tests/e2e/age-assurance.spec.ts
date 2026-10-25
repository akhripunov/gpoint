import { test, expect } from './test';

test('an age-gate scenario can override the default E2E adult state', async ({ page }) => {
  await page.route('**/age-assurance/state', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      required: true,
      eligible: false,
      status: 'pending',
      method: 'unknown',
      jurisdiction: 'test',
      assertedAt: null,
      expiresAt: null,
      selfAttestationAllowed: true,
    }),
  }));

  await page.goto('/?e2eUserId=991234568');

  await expect(page.getByRole('heading', { name: 'Продолжить в GPoint' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Мне 18+' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Profile', exact: true })).toHaveCount(0);
});
