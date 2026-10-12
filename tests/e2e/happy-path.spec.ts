import { test, expect } from '@playwright/test';
import { adminPage, creatorPage } from './helpers';

test('creator dashboard exposes the canonical workspace surface', async ({ page }) => {
  await creatorPage(page, 'happy');
  await expect(page.getByRole('button', { name: 'Overview', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Profile settings', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Imported Posts', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stats', exact: true })).toBeVisible();
  await expect(page.locator('.gpoint-bottom-nav .gpoint-nav-label')).toHaveText(['For You', 'Reels', 'Messages', 'Explore', 'Profile']);
  await expect(page.getByRole('heading', { name: 'Connect Telegram Channel' })).toBeVisible();
  await expect(page.getByRole('button', { name: /Submit for Moderation|Under review/ })).toHaveCount(0);

  await page.getByRole('button', { name: 'Profile settings', exact: true }).click();
  await expect(page.getByText('Public profile:', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Imported Posts', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Imported Posts' })).toBeVisible();
  await page.getByRole('button', { name: 'Stats', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Stats' })).toBeVisible();
});

test('admin can inspect moderation queues from the browser', async ({ page }) => {
  await adminPage(page);
  await page.getByRole('button', { name: 'Content', exact: true }).click();
  await page.getByRole('button', { name: 'VIP', exact: true }).click();
  await expect(page.getByRole('button', { name: 'VIP', exact: true })).toBeVisible();
});
