import { expect, Page } from '@playwright/test';
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
  await page.goto(`/creator/dashboard?e2eUserId=${e2eUserId}`);
  await expect(page.locator('body')).toBeVisible();
  for (let attempt = 0; attempt < 3 && await page.getByText('Loading…', { exact: true }).count(); attempt++) {
    await page.waitForTimeout(1000);
    await page.reload();
  }
  if (await page.getByRole('heading', { name: 'Become a Creator' }).count()) {
    const stamp = Date.now() * 1000 + Math.floor(Math.random() * 1000);
    await page.getByLabel('Display name').fill(`E2E Creator ${stamp}`);
    await page.getByLabel('Slug').fill(`e2e-creator-${stamp}`);
    await page.getByLabel('Bio').fill('Browser release proof creator');
    await page.getByRole('button', { name: /Save Draft|Create draft/ }).click();
  }
  await expect(page.getByRole('heading', { name: 'Creator Dashboard' })).toBeVisible();
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

export async function waitForApi(page: Page, pathPart: string, action: () => Promise<void>) {
  const response = page.waitForResponse(response => response.url().includes(pathPart));
  await action();
  const result = await response;
  expect(result.ok(), `${result.request().method()} ${result.url()} returned ${result.status()}`).toBeTruthy();
}

export async function moderationActions(targetType: string, targetId: string) {
  return readOnlyPrisma.moderationAction.findMany({ where: { targetType, targetId }, orderBy: { createdAt: 'asc' }, select: { action: true, reason: true } });
}
