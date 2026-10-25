import { test, expect } from './test';
import { adminPage, apiRead, createPendingImagePost, creatorPage, moderationActions, waitForApi } from './helpers';

test('admin rejection keeps imported content out of publication and discovery', async ({ page, browser }) => {
  await creatorPage(page, 'content-rejection');
  const caption = `E2E moderation rejection ${Date.now()}`;
  const post = await createPendingImagePost(page, caption);
  const adminContext = await browser.newContext({ baseURL: process.env.GPOINT_MINIAPP_URL ?? 'http://127.0.0.1:5183' });
  const admin = await adminContext.newPage();
  await adminPage(admin);
  await admin.getByRole('button', { name: 'Content', exact: true }).click();
  const row = admin.getByRole('article').filter({ hasText: caption });
  await expect(row).toBeVisible();
  await admin.getByLabel('Moderation reason').fill('Canonical E2E rejection');
  await waitForApi(admin, `/admin/posts/${post.id}/REJECT`, () => row.getByRole('button', { name: 'Reject', exact: true }).click());

  const creatorPost = (await apiRead<any>(page, '/creator/me/posts')).items.find((item: any) => item.id === post.id);
  expect(creatorPost.status).toBe('REJECTED');
  expect(creatorPost.moderationReason).toBe('Canonical E2E rejection');
  const history = await moderationActions('POST', post.id);
  expect(history.map((item) => item.action)).toContain('REJECT');
  const feed = await apiRead<any>(page, '/feed/for-you');
  expect(feed.items.some((item: any) => item.id === post.id)).toBe(false);
  await adminContext.close();
});
