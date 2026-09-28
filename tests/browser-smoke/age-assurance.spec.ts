import { expect, test } from "@playwright/test";
import { installMiniAppBrowserMocks } from "./miniapp-mocks";

test("REG-085 — self-attestation success exits the age gate", async ({ page }) => {
  const { errors } = await installMiniAppBrowserMocks(page, { ageAssuranceRequired: true });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Продолжить в GPoint" })).toBeVisible();
  await page.getByRole("button", { name: "Мне 18+" }).click();
  await expect(page.locator(".age-assurance-gate")).toHaveCount(0);
  await expect(page.locator(".bottom-nav")).toBeVisible();
  expect(errors).toEqual([]);
});

test("REG-085 — failed self-attestation stays blocked and offers retry", async ({ page }) => {
  const { errors } = await installMiniAppBrowserMocks(page, {
    ageAssuranceRequired: true,
    ageAttestationWriteStatus: 503,
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Продолжить в GPoint" })).toBeVisible();
  await page.getByRole("button", { name: "Мне 18+" }).click();
  await expect(page.getByRole("heading", { name: "Не удалось проверить возраст" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Повторить" })).toBeVisible();
  await expect(page.locator(".bottom-nav")).toHaveCount(0);
  expect(errors).toEqual([
    "Failed to load resource: the server responded with a status of 503 (Service Unavailable)",
  ]);
});
