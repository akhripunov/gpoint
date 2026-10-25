import { expect, type Page, useDefaultAgeAssuranceState, useDeterministicTelegramSdk } from './test';
import { PrismaClient } from '@prisma/client';

const readOnlyPrisma = new PrismaClient();

export const tinyPng = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');
export const tinyMp4 = Buffer.from('AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAN1bW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAAMgAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAp90cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAAMgAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAABAAAAAQAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAADIAAAEAAABAAAAAAIXbWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAAyAAAACgBVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAABwm1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAYJzdGJsAAAAvnN0c2QAAAAAAAAAAQAAAK5hdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAABAAEABIAAAASAAAAAAAAAABFUxhdmM2Mi4yOC4xMDIgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAANGF2Y0MBZAAK/+EAF2dkAAqs2V7ARAAAAwAEAAADAMg8SJZYAQAGaOvjyyLA/fj4AAAAABBwYXNwAAAAAQAAAAEAAAAUYnRydAAAAAAAAHcQAAAAAAAAABhzdHRzAAAAAAAAAAEAAAAFAAACAAAAABRzdHNzAAAAAAAAAAEAAAABAAAAOGN0dHMAAAAAAAAABQAAAAEAAAQAAAAAAQAACgAAAAABAAAEAAAAAAEAAAAAAAAAAQAAAgAAAAAcc3RzYwAAAAAAAAABAAAAAQAAAAUAAAABAAAAKHN0c3oAAAAAAAAAAAAAAAUAAALKAAAADAAAAAwAAAAMAAAADAAAABRzdGNvAAAAAAAAAAEAAAOlAAAAYnVkdGEAAABabWV0YQAAAAAAAAAhaGRscgAAAAAAAAAAbWRpcmFwcGwAAAAAAAAAAAAAAAAtaWxzdAAAACWpdG9vAAAAHWRhdGEAAAABAAAAAExhdmY2Mi4xMi4xMDIAAAAIZnJlZQAAAwJtZGF0AAACrgYF//+q3EXpvebZSLeWLNgg2SPu73gyNjQgLSBjb3JlIDE2NSByMzIyMyAwNDgwY2IwIC0gSC4yNjQvTVBFRy00IEFWQyBjb2RlYyAtIENvcHlsZWZ0IDIwMDMtMjAyNSAtIGh0dHA6Ly93d3cudmlkZW9sYW4ub3JnL3gyNjQuaHRtbCAtIG9wdGlvbnM6IGNhYmFjPTEgcmVmPTMgZGVibG9jaz0xOjA6MCBhbmFseXNlPTB4MzoweDExMyBtZT1oZXggc3VibWU9NyBwc3k9MSBwc3lfcmQ9MS4wMDowLjAwIG1peGVkX3JlZj0xIG1lX3JhbmdlPTE2IGNocm9tYV9tZT0xIHRyZWxsaXM9MSA4eDhkY3Q9MSBjcW09MCBkZWFkem9uZT0yMSwxMSBmYXN0X3Bza2lwPTEgY2hyb21hX3FwX29mZnNldD0tMiB0aHJlYWRzPTEgbG9va2FoZWFkX3RocmVhZHM9MSBzbGljZWRfdGhyZWFkcz0wIG5yPTAgZGVjaW1hdGU9MSBpbnRlcmxhY2VkPTAgYmx1cmF5X2NvbXBhdD0wIGNvbnN0cmFpbmVkX2ludHJhPTAgYmZyYW1lcz0zIGJfcHlyYW1pZD0yIGJfYWRhcHQ9MSBiX2JpYXM9MCBkaXJlY3Q9MSB3ZWlnaHRiPTEgb3Blbl9nb3A9MCB3ZWlnaHRwPTIga2V5aW50PTI1MCBrZXlpbnRfbWluPTI1IHNjZW5lY3V0PTQwIGludHJhX3JlZnJlc2g9MCByY19sb29rYWhlYWQ9NDAgcmM9Y3JmIG1idHJlZT0xIGNyZj0yMy4wIHFjb21wPTAuNjAgcXBtaW49MCBxcG1heD02OSBxcHN0ZXA9NCBpcF9yYXRpbz0xLjQwIGFxPTE6MS4wMACAAAAAFGWIhAAz//7fMvgU1oUOdEIzZHS/AAAACEGaJGxCv/7AAAAACEGeQniF/8GBAAAACAGeYXRCv8SAAAAACAGeY2pCv8SB', 'base64');

export async function creatorPage(page: Page, scenario = `run-${Date.now()}`) {
  const configuredRun = Number(process.env.GPOINT_E2E_RUN_ID);
  const runSuffix = Number.isSafeInteger(configuredRun) ? configuredRun : Date.now() * 1000 + Math.floor(Math.random() * 1000);
  const project = page.viewportSize()?.width === 393 ? 'chromium-mobile' : 'chromium';
  const identityHash = Array.from(`${scenario}:${project}`).reduce((sum, char) => sum + char.charCodeAt(0), 0);
  const e2eUserId = 200000000 + runSuffix * 10 + identityHash;
  const creatorRoute = `/creator/dashboard?e2eUserId=${e2eUserId}`;
  let profileReady = false;
  for (let attempt = 0; attempt < 3 && !profileReady; attempt++) {
    await page.goto(`/?e2eUserId=${e2eUserId}`, { timeout: 60_000 });
    try {
      await expect(page.getByRole('button', { name: 'Profile', exact: true })).toBeVisible({ timeout: 5000 });
      profileReady = true;
    } catch (error) {
      if (attempt === 2) throw error;
      await page.waitForTimeout(500);
    }
  }
  const stamp = Date.now() * 1000 + Math.floor(Math.random() * 1000);
  const onboardingPayload = { displayName: `E2E Creator ${stamp}`, slug: `e2e-creator-${stamp}`, bio: 'Browser release proof creator', tags: [] as string[] };
  const api = process.env.GPOINT_E2E_API_URL ?? 'http://localhost:3000';
  const result = await page.evaluate(async ({ api, payload }) => {
    const token = localStorage.getItem('gpoint.session');
    const response = await fetch(`${api}/creator/onboarding`, {
      method: 'POST',
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    });
    return { ok: response.ok, status: response.status, body: await response.text() };
  }, { api, payload: onboardingPayload });
  if (!result.ok) throw new Error(`creator onboarding setup failed: ${result.status} ${result.body}`);
  await page.goto(creatorRoute, { timeout: 60_000 });
  await expect(page.locator('body')).toBeVisible();
  for (let attempt = 0; attempt < 3 && await page.getByText('Loading…', { exact: true }).count(); attempt++) {
    await page.waitForTimeout(1000);
    await page.reload();
  }
  for (let attempt = 0; attempt < 3; attempt++) {
    let state: { creator?: unknown };
    try {
      state = await apiRead<{ creator?: unknown }>(page, '/creator/me');
    } catch (error) {
      if (!(error instanceof Error) || !error.message.includes('401')) throw error;
      await page.goto(`/?e2eUserId=${e2eUserId}`);
      await expect(page.getByRole('button', { name: 'Profile', exact: true })).toBeVisible();
      await apiRequest(page, '/creator/onboarding', 'POST', onboardingPayload);
      await page.goto(creatorRoute);
      continue;
    }
    if (state.creator) break;
    try {
      await apiRequest(page, '/creator/onboarding', 'POST', onboardingPayload);
    } catch (error) {
      if (!(error instanceof Error) || !error.message.includes('401')) throw error;
      await page.goto(`/?e2eUserId=${e2eUserId}`, { timeout: 60_000 });
      await expect(page.getByRole('button', { name: 'Profile', exact: true })).toBeVisible({ timeout: 5000 });
      continue;
    }
    await page.waitForTimeout(500);
    await page.goto(`/?e2eUserId=${e2eUserId}`);
    await expect(page.getByRole('button', { name: 'Profile', exact: true })).toBeVisible();
    await page.goto(creatorRoute);
  }
  await expect(page.getByRole('heading', { name: 'Creator Dashboard' })).toBeVisible();
}

export async function adminPage(page: Page) {
  await useDeterministicTelegramSdk(page);
  await useDefaultAgeAssuranceState(page);
  const profile = page.getByRole('button', { name: 'Profile', exact: true });
  for (let attempt = 0; attempt < 3; attempt++) {
    await page.goto('/?e2eUserId=991234567');
    try {
      await profile.waitFor({ state: 'visible', timeout: 6_000 });
      break;
    } catch {
      if (attempt === 2) throw new Error('browser auth did not become ready for admin E2E');
    }
  }
  await expect(profile).toBeVisible();
  await page.goto('/admin');
  await expect(page.getByRole('heading', { name: 'Moderation' })).toBeVisible();
}

export async function choosePng(page: Page, index = 0) {
  await page.locator('input[type=file]').nth(index).setInputFiles({ name: `e2e-${index}.png`, mimeType: 'image/png', buffer: tinyPng });
}

export async function apiRead<T>(page: Page, path: string): Promise<T> {
  const base = process.env.GPOINT_E2E_API_URL ?? 'http://localhost:3000';
  return page.evaluate(async ({ path, base }) => {
    const token = localStorage.getItem('gpoint.session');
    const response = await fetch(`${base}${path}`, { headers: { authorization: `Bearer ${token}` } });
    if (!response.ok) throw new Error(`read ${path} failed: ${response.status}`);
    return response.json();
  }, { path, base });
}

export async function apiRequest<T>(page: Page, path: string, method: string, body?: unknown): Promise<T> {
  const base = process.env.GPOINT_E2E_API_URL ?? 'http://localhost:3000';
  return page.evaluate(async ({ path, method, body, base }) => {
    const token = localStorage.getItem('gpoint.session');
    const response = await fetch(`${base}${path}`, {
      method,
      headers: { authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'content-type': 'application/json' }) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    if (!response.ok) throw new Error(`${method} ${path} failed: ${response.status} ${text}`);
    return text ? JSON.parse(text) as T : undefined as T;
  }, { path, method, body, base });
}

export async function createPendingImagePost(page: Page, caption: string) {
  const intent = await apiRequest<{ mediaId: string }>(page, '/creator/me/media/intent', 'POST', { kind: 'IMAGE', mimeType: 'image/png', fileSize: tinyPng.length });
  await apiRequest(page, `/creator/me/media/${intent.mediaId}/data`, 'POST', { dataBase64: tinyPng.toString('base64') });
  await apiRequest(page, `/creator/me/media/${intent.mediaId}/finalize`, 'POST');
  const post = await apiRequest<{ id: string }>(page, '/creator/me/posts', 'POST', { type: 'IMAGE', caption, mediaIds: [intent.mediaId] });
  return apiRequest<{ id: string; status: string }>(page, `/creator/me/posts/${post.id}`, 'PATCH', { status: 'PENDING', caption, mediaIds: [intent.mediaId] });
}

export async function waitForApi(page: Page, pathPart: string, action: () => Promise<void>) {
  const response = page.waitForResponse(response => response.url().includes(pathPart));
  await action();
  const result = await response;
  expect(result.ok(), `${result.request().method()} ${result.url()} returned ${result.status()}`).toBeTruthy();
}

export async function moderationActions(targetType: string, targetId: string) {
  return readOnlyPrisma.moderationAction.findMany({ where: { targetType, targetId }, orderBy: { createdAt: 'asc' }, select: { action: true, reason: true } });
}
