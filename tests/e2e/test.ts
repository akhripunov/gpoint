import { test as base, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

const ageAssurancePages = new WeakSet<Page>();
const telegramSdkPages = new WeakSet<Page>();

export async function useDeterministicTelegramSdk(page: Page): Promise<void> {
  if (telegramSdkPages.has(page)) return;
  telegramSdkPages.add(page);
  await page.route('https://telegram.org/js/telegram-web-app.js', (route) => route.fulfill({
    status: 200,
    contentType: 'application/javascript',
    body: 'window.Telegram = { WebApp: { initData: "", initDataUnsafe: {}, ready() {} } };',
  }));
}

export async function useDefaultAgeAssuranceState(page: Page): Promise<void> {
  if (ageAssurancePages.has(page)) return;
  ageAssurancePages.add(page);
  await page.route('**/age-assurance/state', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      required: false,
      eligible: true,
      status: 'verified',
      method: 'e2e',
      jurisdiction: 'test',
      assertedAt: null,
      expiresAt: null,
      selfAttestationAllowed: false,
    }),
  }));
}

/**
 * The API integration is intentionally not started by the browser E2E server
 * (it needs the separately deployed AGATA service). Keep ordinary UI journeys
 * deterministic with an adult test account; age-gate journeys can register a
 * later route for this URL and override the default per Playwright routing.
 */
export const test = base.extend({
  page: async ({ page }, use) => {
    await useDeterministicTelegramSdk(page);
    await useDefaultAgeAssuranceState(page);
    await use(page);
  },
});

export { expect };
export type { Page } from '@playwright/test';
