import { test, expect } from '@playwright/test';
import { adminPage, apiRead, apiRequest, creatorPage, moderationActions, waitForApi } from './helpers';

test('VIP moderation rejects, preserves history, then supports API resubmission and approval', async ({ page, browser }) => {
  await creatorPage(page, 'vip-rejection');
  const first = await apiRequest<any>(page, '/creator/me/vip', 'PATCH', { title: 'E2E VIP', affiliateUrl: 'https://t.me/e2e_bot?startapp=vip-one' });
  const offerId = first.offer.id;
  const adminContext = await browser.newContext({ baseURL: process.env.GPOINT_MINIAPP_URL ?? 'http://127.0.0.1:5183' });
  const admin = await adminContext.newPage();
  await adminPage(admin);
  await admin.getByRole('button', { name: 'VIP', exact: true }).click();
  const row = admin.getByRole('article').filter({ hasText: 'E2E VIP' }).filter({ hasText: 'PENDING_REVIEW' });
  await expect(row).toContainText('PENDING_REVIEW');
  await admin.getByLabel('Moderation reason').fill('Canonical VIP rejection');
  await waitForApi(admin, `/admin/vip/${offerId}/REJECT`, () => row.getByRole('button', { name: 'Reject', exact: true }).click());
  expect((await apiRead<any>(page, '/creator/me/vip')).moderationReason).toBe('Canonical VIP rejection');
  expect((await apiRead<any>(page, '/creator/me/vip')).status).toBe('REJECTED');
  expect((await moderationActions('VIP_OFFER', offerId)).map((item) => item.action)).toContain('REJECT');

  await apiRequest(page, '/creator/me/vip', 'PATCH', { title: 'E2E VIP', affiliateUrl: 'https://t.me/e2e_bot?startapp=vip-two' });
  await admin.reload();
  await admin.getByRole('button', { name: 'VIP', exact: true }).click();
  const resubmitted = admin.getByRole('article').filter({ hasText: 'E2E VIP' }).filter({ hasText: 'PENDING_REVIEW' });
  await expect(resubmitted).toContainText('PENDING_REVIEW');
  await waitForApi(admin, `/admin/vip/${offerId}/APPROVE`, () => resubmitted.getByRole('button', { name: 'Approve', exact: true }).click());
  expect((await apiRead<any>(page, '/creator/me/vip')).status).toBe('ACTIVE');
  expect((await moderationActions('VIP_OFFER', offerId)).map((item) => item.action)).toEqual(expect.arrayContaining(['REJECT', 'APPROVE']));
  await adminContext.close();
});
