import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { creatorPage } from './helpers';

test('captures the reusable GPoint UI foundation surfaces', async ({ page, browser }) => {
  test.setTimeout(120_000);
  const outputDir = path.join(process.cwd(), 'test-results', 'ui-foundation');
  fs.mkdirSync(outputDir, { recursive: true });
  const prefix = `${test.info().project.name}-${page.viewportSize()?.width}`;
  const shot = (name: string) => page.screenshot({ path: path.join(outputDir, `${prefix}-${name}.png`), fullPage: true });

  await page.goto('/');
  await expect(page.locator('.app-shell')).toBeVisible();
  await shot('for-you');

  await page.getByRole('button', { name: 'Reels', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Reels', exact: true })).toBeVisible();
  await shot('reels');

  await page.getByRole('button', { name: 'Explore', exact: true }).click();
  await shot('explore');

  await page.getByRole('button', { name: 'Profile', exact: true }).click();
  await shot('profile');

  await creatorPage(page, 'visual-dashboard');
  await shot('creator-overview');
  await page.getByRole('button', { name: 'Profile settings', exact: true }).click();
  await shot('creator-profile-settings');
  await page.getByRole('button', { name: 'Imported Posts', exact: true }).click();
  await shot('creator-imported-posts');
  await page.getByRole('button', { name: 'Stats', exact: true }).click();
  await shot('creator-stats');

  if (test.info().project.name === 'chromium-mobile') {
    const phone390 = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page390 = await phone390.newPage();
    const shot390 = (name: string) => page390.screenshot({ path: path.join(outputDir, `chromium-mobile-390-${name}.png`), fullPage: true });
    await page390.goto('/');
    await expect(page390.locator('.app-shell')).toBeVisible();
    await shot390('for-you');
    await page390.getByRole('button', { name: 'Reels', exact: true }).click();
    await shot390('reels');
    await page390.getByRole('button', { name: 'Explore', exact: true }).click();
    await shot390('explore');
    await page390.getByRole('button', { name: 'Profile', exact: true }).click();
    await shot390('profile');
    await creatorPage(page390, 'visual-dashboard-390');
    await shot390('creator-overview');
    await page390.getByRole('button', { name: 'Profile settings', exact: true }).click();
    await shot390('creator-profile-settings');
    await page390.getByRole('button', { name: 'Imported Posts', exact: true }).click();
    await shot390('creator-imported-posts');
    await page390.getByRole('button', { name: 'Stats', exact: true }).click();
    await shot390('creator-stats');
    await phone390.close();

    for (const viewport of [{ width: 393, height: 852 }, { width: 430, height: 932 }, { width: 360, height: 800 }]) {
      const phone = await browser.newContext({ viewport });
      const phonePage = await phone.newPage();
      await creatorPage(phonePage, `visual-dashboard-${viewport.width}`);
      await phonePage.screenshot({ path: path.join(outputDir, `chromium-mobile-${viewport.width}-creator-overview.png`), fullPage: true });
      await phone.close();
    }
  }
});
