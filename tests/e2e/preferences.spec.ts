import { test, expect } from '@playwright/test';
import { apiRead, creatorPage, waitForApi } from './helpers';

test('recommendation controls work from For You, Reels and Profile', async ({ page }) => {
  await creatorPage(page, 'preference-controls');
  const e2eUserId = new URL(page.url()).searchParams.get('e2eUserId');
  await page.goto(`/?e2eUserId=${e2eUserId}`);
  await page.getByRole('button', { name: 'For You', exact: true }).click();
  const card = page.locator('.creator-card').first();
  await expect(card).toBeVisible();
  await card.locator('summary[aria-label="Recommendation actions"]').click();
  await waitForApi(page, '/preferences', () => card.getByRole('button', { name: 'Don\'t recommend this creator', exact: true }).click());
  await expect(card.getByRole('status')).toContainText("won't recommend this creator");
  let prefs = await apiRead<any>(page, '/preferences');
  expect(prefs.items.some((item: any) => item.preferenceType === 'DO_NOT_RECOMMEND_CREATOR')).toBeTruthy();
  await card.getByRole('button', { name: 'Undo', exact: true }).click();
  await expect.poll(async () => (await apiRead<any>(page, '/preferences')).items.length).toBe(0);

  await page.goto(`/?e2eUserId=${e2eUserId}`);
  await page.locator('nav.bottom-nav button').nth(1).click();
  const reel = page.locator('.reel-card').first();
  await expect(reel).toBeVisible();
  await reel.locator('summary[aria-label="Recommendation actions"]').click();
  await waitForApi(page, '/preferences', () => reel.getByRole('button', { name: 'Not interested', exact: true }).click());
  prefs = await apiRead<any>(page, '/preferences');
  expect(prefs.items.some((item: any) => item.preferenceType === 'NOT_INTERESTED' && item.entityType === 'REEL')).toBeTruthy();
  await reel.getByRole('button', { name: 'Undo', exact: true }).click();
  await expect.poll(async () => (await apiRead<any>(page, '/preferences')).items.length).toBe(0);

  await page.locator('nav.bottom-nav button').nth(3).click();
  await expect(page.getByRole('heading', { name: 'Recommendation controls' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Reset recommendations', exact: true })).toBeVisible();
});
