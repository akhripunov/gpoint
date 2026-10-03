import { expect, test } from "@playwright/test";
import { installMiniAppBrowserMocks } from "./miniapp-mocks";

test("launch keeps one dark splash through first paint, Telegram auth and age-state loading", async ({ page }) => {
  const { errors } = await installMiniAppBrowserMocks(page, {
    browserAuthDelayMs: 350,
    ageAssuranceDelayMs: 500,
  });

  // Hold the app bundle briefly so the test observes the literal HTML first paint,
  // before React can replace the server-side startup placeholder.
  await page.route("**/src/main.tsx**", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 700));
    await route.continue();
  });

  await page.goto("/", { waitUntil: "domcontentloaded" });
  const splash = page.locator(".gpoint-boot-splash");
  await expect(splash).toBeVisible();
  await expect(splash).toContainText("Открываем GPoint…");
  await expect(splash).toHaveCSS("background-color", "rgb(9, 13, 22)");
  await expect(page.locator(".bottom-nav")).toHaveCount(0);

  await expect(page.locator(".bottom-nav")).toBeVisible();
  await expect(splash).toHaveCount(0);
  expect(errors).toEqual([]);
});
