import { test, expect } from '@playwright/test';
import { adminPage } from './helpers';

test('Mini App loads in Chromium without horizontal overflow', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('body')).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
  expect(overflow).toBeTruthy();
});

test('Telegram mobile shell fills common phone viewports without scaling', async ({ browser }) => {
  for (const viewport of [{ width: 390, height: 844 }, { width: 393, height: 852 }, { width: 430, height: 932 }, { width: 360, height: 800 }]) {
    const context = await browser.newContext({ viewport });
    const page = await context.newPage();
    await page.goto('/');
    await expect(page.locator('.app-shell')).toBeVisible();
    const metrics = await page.evaluate(() => {
      const shell = document.querySelector('.app-shell');
      const nav = document.querySelector('.bottom-nav');
      const rect = shell?.getBoundingClientRect();
      return {
        viewport: window.innerWidth,
        documentWidth: document.documentElement.scrollWidth,
        shellWidth: rect?.width ?? 0,
        navBottom: nav?.getBoundingClientRect().bottom ?? 0,
        viewportHeight: window.innerHeight,
        transform: shell ? getComputedStyle(shell).transform : 'none',
      };
    });
    expect(metrics.documentWidth).toBeLessThanOrEqual(metrics.viewport);
    expect(metrics.shellWidth).toBeGreaterThanOrEqual(metrics.viewport - 1);
    expect(metrics.navBottom).toBeLessThanOrEqual(metrics.viewportHeight + 1);
    expect(metrics.transform).toBe('none');
    await context.close();
  }
});

test('admin moderation route is reachable in the browser', async ({ page }) => {
  await adminPage(page);
  await expect(page.getByRole('heading', { name: 'Moderation' })).toBeVisible();
});

test('consumer Explore and Reels routes remain browser reachable', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('body')).toBeVisible();
  await page.getByRole('button', { name: /Reels/ }).click();
  await expect(page.getByRole('heading', { name: 'Reels', exact: true })).toBeVisible();
  expect(await page.locator('body').innerText()).not.toContain('storageKey');
});

test('mobile creator route has no horizontal overflow', async ({ page }) => {
  await page.goto('/creator/dashboard');
  await expect(page.locator('body')).toBeVisible();
  const dimensions = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth);
});

test('creator dashboard exposes the frozen creator-platform surface', async ({ page }) => {
  await page.goto('/creator/dashboard');
  await expect(page.locator('body')).toBeVisible();
  expect(await page.locator('body').innerText()).not.toContain('storageKey');
});

test('admin moderation surface has creator, content and VIP queues', async ({ page }) => {
  await adminPage(page);
  await expect(page.getByRole('button', { name: 'Creators', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Content', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'VIP', exact: true })).toBeVisible();
});
