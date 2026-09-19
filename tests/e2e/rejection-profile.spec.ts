import { test, expect } from '@playwright/test';
import { adminPage, apiRead, creatorPage, moderationActions, waitForApi } from './helpers';

test('admin approval publishes the creator profile with an audit trail', async ({ page, browser }) => {
  await creatorPage(page, 'profile-approval');
  const creator = (await apiRead<any>(page, '/creator/me')).creator;
  const adminContext = await browser.newContext({ baseURL: process.env.GPOINT_MINIAPP_URL ?? 'http://127.0.0.1:5183' });
  const admin = await adminContext.newPage();
  await adminPage(admin);
  const row = admin.getByRole('article').filter({ hasText: creator.displayName });
  await expect(row).toContainText('DRAFT');
  await waitForApi(admin, `/admin/creators/${creator.id}/APPROVE`, () => row.getByRole('button', { name: 'Approve', exact: true }).click());

  expect((await apiRead<any>(page, '/creator/me')).creator.status).toBe('APPROVED');
  const publicProfile = await apiRead<any>(page, `/creators/${creator.slug}`);
  expect(publicProfile.slug).toBe(creator.slug);
  const history = await moderationActions('CREATOR', creator.id);
  expect(history.map((item) => item.action)).toContain('APPROVE');
  await adminContext.close();
});
