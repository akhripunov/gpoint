import { defineConfig, devices } from "@playwright/test";

const appUrl = "http://127.0.0.1:5189";

export default defineConfig({
  testDir: "./tests/browser-smoke",
  fullyParallel: false,
  workers: 1,
  timeout: 15_000,
  expect: { timeout: 7_000 },
  reporter: "list",
  outputDir: "test-results/browser-smoke",
  preserveOutput: "always",
  use: {
    ...devices["Desktop Chrome"],
    viewport: { width: 390, height: 844 },
    baseURL: appUrl,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    actionTimeout: 7_000,
    navigationTimeout: 8_000,
  },
  webServer: {
    command:
      "VITE_DEV_BROWSER_AUTH=true VITE_API_URL=http://127.0.0.1:5199 ./node_modules/.bin/vite --host 127.0.0.1 --port 5189 --strictPort",
    cwd: "apps/miniapp",
    url: appUrl,
    reuseExistingServer: !process.env.CI,
    timeout: 15_000,
  },
});
