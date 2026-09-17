import { defineConfig, devices } from '@playwright/test';

const testDatabaseUrl = process.env.DATABASE_URL ?? 'postgresql://gpoint:gpoint_dev_only@127.0.0.1:5432/gpoint';
const testApiUrl = 'http://127.0.0.1:3013';
const testAppUrl = 'http://127.0.0.1:5183';
process.env.DATABASE_URL ??= testDatabaseUrl;
process.env.GPOINT_MINIAPP_URL ??= testAppUrl;
process.env.GPOINT_E2E_API_URL ??= testApiUrl;

export default defineConfig({
  testDir: './tests/e2e',
  globalSetup: './tests/e2e/global-setup.ts',
  timeout: 30_000,
  fullyParallel: false,
  reporter: [['line']],
  webServer: [
    { command: `NODE_ENV=development DATABASE_URL=${testDatabaseUrl} TELEGRAM_BOT_TOKEN= DEV_BROWSER_AUTH=true MINIAPP_ORIGIN=${testAppUrl} API_PORT=3013 node node_modules/tsx/dist/cli.mjs src/main.ts`, cwd: 'apps/api', url: `${testApiUrl}/health/live`, reuseExistingServer: false, timeout: 30_000 },
    { command: `VITE_DEV_BROWSER_AUTH=true VITE_API_URL=${testApiUrl} node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5183`, cwd: 'apps/miniapp', url: testAppUrl, reuseExistingServer: false, timeout: 30_000 },
  ],
  use: { baseURL: process.env.GPOINT_MINIAPP_URL ?? testAppUrl, trace: 'retain-on-failure' },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'chromium-mobile', use: { ...devices['Pixel 5'] } },
  ],
});
