import { test, expect } from '@playwright/test';

test('consumer My Profile matches the approved UserProfile structure and uses real user data', async ({ page }) => {
  await page.goto('/?e2eUserId=991234567');
  await expect(page.getByRole('button', { name: 'Profile', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Profile', exact: true }).click();
  await expect(page.getByText('MY PROFILE', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Local Dev' })).toBeVisible();
  await expect(page.getByText('Saved', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Saved: unavailable')).toBeVisible();
  await expect(page.getByText('Following', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Following: unavailable')).toBeVisible();
  await expect(page.getByText('My Subscription', { exact: true })).toBeVisible();
  await expect(page.getByText('Wallet', { exact: true })).toBeVisible();
  await expect(page.getByText('Settings', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /Creator tools/ })).toBeVisible();
  await expect(page.getByText('Preferences', { exact: true })).toHaveCount(0);
  await expect(page.getByText('GPoint creator', { exact: true })).toHaveCount(0);
});
