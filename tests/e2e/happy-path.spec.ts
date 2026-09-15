import { test, expect } from '@playwright/test';
import { apiRead, choosePng, creatorPage, tinyMp4, waitForApi } from './helpers';

test('creator can configure profile, media, post, reel and VIP controls', async ({ page }) => {
  await creatorPage(page, 'happy');
  const creatorBefore = await apiRead<any>(page, '/creator/me');
  expect(creatorBefore.creator?.id).toBeTruthy();
  expect(creatorBefore.creator.status).toBe('DRAFT');
  await page.getByRole('button', { name: 'Profile', exact: true }).click();
  await expect(page.getByText('Avatar', { exact: true })).toBeVisible();
  await expect(page.getByText('Cover', { exact: true })).toBeVisible();
  await waitForApi(page, '/finalize', () => choosePng(page, 0));
  await waitForApi(page, '/finalize', () => choosePng(page, 1));
  await page.getByRole('button', { name: 'Save Draft', exact: true }).click();
  await page.locator('.content-tabs').getByRole('button', { name: 'Overview', exact: true }).click();
  await waitForApi(page, '/creator/me/submit', async () => page.getByRole('button', { name: 'Submit for Moderation', exact: true }).click());
  expect((await apiRead<any>(page, '/creator/me')).creator.status).toBe('PENDING');
  await page.getByRole('button', { name: 'Content', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Save Draft', exact: true })).toBeVisible();
  const postCaption = `Browser E2E image post ${Date.now()}`;
  await page.getByLabel('Post caption').fill(postCaption);
  await waitForApi(page, '/creator/me/posts', async () => {
    await page.locator('input[type=file]').nth(0).setInputFiles({ name: 'e2e-post.png', mimeType: 'image/png', buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64') });
    await page.getByRole('button', { name: 'Save Draft', exact: true }).click();
  });
  const postList = await apiRead<any>(page, '/creator/me/posts');
  const postIndex = postList.items.findIndex((item: any) => item.caption === postCaption);
  const post = postList.items[postIndex];
  const submitIndex = postList.items.slice(0, postIndex).filter((item: any) => item.status === 'DRAFT').length;
  expect(post?.id).toBeTruthy();
  expect(post.status).toBe('DRAFT');
  await page.getByRole('button', { name: 'Submit for Moderation', exact: true }).nth(submitIndex).click();
  await expect.poll(async () => (await apiRead<any>(page, '/creator/me/posts')).items.find((item: any) => item.id === post.id)?.status).toBe('PENDING');
  await page.locator('.content-tabs').getByRole('button', { name: 'Reels', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Reel editor' })).toBeVisible();
  const reelCaption = `Browser E2E reel ${Date.now()}`;
  await waitForApi(page, '/finalize', async () => {
    await page.locator('input[type=file]').nth(1).setInputFiles({ name: 'e2e-reel.mp4', mimeType: 'video/mp4', buffer: tinyMp4 });
  });
  await page.getByLabel('Reel caption').fill(reelCaption);
  await waitForApi(page, '/creator/me/reels', async () => page.getByRole('button', { name: 'Save Draft', exact: true }).click());
  const reelList = await apiRead<any>(page, '/creator/me/reels');
  const reel = reelList.items.find((item: any) => item.caption === reelCaption);
  expect(reel?.id).toBeTruthy();
  expect(reel.media?.[0]?.mediaAssetId).toBeTruthy();
  expect(reel.status).toBe('DRAFT');
  const allPostList = await apiRead<any>(page, '/creator/me/posts');
  const reelIndex = allPostList.items.findIndex((item: any) => item.id === reel.id);
  const reelSubmitIndex = allPostList.items.slice(0, reelIndex).filter((item: any) => item.status === 'DRAFT').length;
  await page.getByRole('button', { name: 'Submit for Moderation', exact: true }).nth(reelSubmitIndex).click();
  await expect.poll(async () => (await apiRead<any>(page, '/creator/me/reels')).items.find((item: any) => item.id === reel.id)?.status).toBe('PENDING');
  await page.getByRole('button', { name: 'VIP', exact: true }).click();
  await page.getByLabel('VIP title').fill('Browser E2E VIP');
  await page.getByLabel('Affiliate URL').fill('https://t.me/e2e_bot?startapp=vip');
  await waitForApi(page, '/creator/me/vip', async () => page.getByRole('button', { name: 'Save / Submit', exact: true }).click());
  expect((await apiRead<any>(page, '/creator/me/vip')).status).toBe('PENDING_REVIEW');
});

test('admin can inspect moderation queues from the browser', async ({ page }) => {
  await page.goto('/admin');
  await expect(page.getByRole('heading', { name: 'Moderation' })).toBeVisible();
  await page.getByRole('button', { name: 'Content', exact: true }).click();
  await page.getByRole('button', { name: 'VIP', exact: true }).click();
  await expect(page.getByRole('button', { name: 'VIP', exact: true })).toBeVisible();
});
