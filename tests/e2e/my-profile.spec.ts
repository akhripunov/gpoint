import { test, expect } from '@playwright/test';

test('consumer My Profile uses real user data and existing settings/onboarding paths', async ({ page }) => {
  await page.goto('/?e2eUserId=991234567');
  await expect(page.getByRole('button', { name: 'Profile', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Profile', exact: true }).click();
  await expect(page.getByText('MY PROFILE', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Local Dev' })).toBeVisible();
  await expect(page.getByText('Become a Creator', { exact: true })).toBeVisible();
  await expect(page.getByText('Wallet', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: /Settings/ }).click();
  await expect(page.getByRole('heading', { name: 'Recommendation controls' })).toBeVisible();
  await page.getByRole('button', { name: /Become a Creator/ }).click();
  await expect(page.getByRole('heading', { name: 'Become a Creator' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Connect Telegram channel', exact: true })).toBeVisible();
});
