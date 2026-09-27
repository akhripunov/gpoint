import { expect, test, type Page } from "@playwright/test";
import { installMiniAppBrowserMocks } from "./miniapp-mocks";

const runtimeErrors = new WeakMap<Page, string[]>();

test.beforeEach(async ({ page }, testInfo) => {
  const { errors } = await installMiniAppBrowserMocks(page, {
    telegramStartParam: testInfo.title.startsWith("C —") ? "model_models_hub" : undefined,
    ageAssuranceRequired: testInfo.title.includes("age assurance blocked"),
  });
  runtimeErrors.set(page, errors);
});

test("A — direct /model/:handle resolves the model", async ({ page }) => {
  console.log("DIRECT_ROUTE_START");
  await page.goto("/model/models_hub", { waitUntil: "domcontentloaded" });
  await expect(page.locator("h1")).toContainText("Models Hub");
  await expect(page).toHaveURL(/\/model\/models_hub$/);
  console.log("DIRECT_ROUTE_PASS");
});

test("B — unknown handle shows Model unavailable", async ({ page }) => {
  console.log("INVALID_ROUTE_START");
  await page.goto("/model/unknown-model", { waitUntil: "domcontentloaded" });
  console.log("INVALID_ROUTE_LOADED", page.url());
  await expect(page.getByRole("heading", { name: "Model unavailable" })).toBeVisible();
  console.log("INVALID_ROUTE_PASS");
});

test("C — Telegram start parameter resolves the same model", async ({ page }) => {
  console.log("START_PARAM_START");
  await page.goto("/?tgWebAppStartParam=model_models_hub", {
    waitUntil: "domcontentloaded",
  });
  await expect(page.locator("h1")).toContainText("Models Hub");
  await expect(page).toHaveURL(/\/model\/models_hub\?/);
  console.log("START_PARAM_PASS");
});

test("D — feed creator action opens the model profile", async ({ page }) => {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const card = page.getByRole("article", { name: /Post by Models Hub/ });
  await expect(card).toBeVisible();
  await card.locator(".post-creator").click();
  await expect(page.locator("h1")).toContainText("Models Hub");
  await expect(page).toHaveURL(/\/model\/models_hub$/);
  console.log("FEED_CREATOR_CLICK_PASS");
});

test("E — share action produces the canonical current-model URL", async ({ page }) => {
  await page.goto("/model/models_hub", { waitUntil: "domcontentloaded" });
  await expect(page.locator("h1")).toContainText("Models Hub");
  await page.getByRole("button", { name: /Share Models Hub/ }).click();
  await expect.poll(() => page.evaluate(() => (window as Window & { __sharedUrl?: string }).__sharedUrl))
    .toBe("https://gpoint.amiora.one/model/models_hub");
  console.log("SHARE_PASS");
});

test("F — post click, 3-column grid, bottom nav and 390×844 layout", async ({ page }, testInfo) => {
  console.log("FEED_POST_CLICK_START");
  await page.goto("/", { waitUntil: "domcontentloaded" });
  const card = page.getByRole("article", { name: /Post by Models Hub/ });
  await expect(card).toBeVisible();
  await card.locator(".post-caption").click();
  await expect(page.locator("h1")).toContainText("Models Hub");
  console.log("FEED_POST_CLICK_PASS");

  const grid = page.locator(".free-grid");
  await expect(grid).toBeVisible();
  const columns = await grid.evaluate((element) =>
    getComputedStyle(element).gridTemplateColumns.split(" ").length,
  );
  expect(columns).toBe(3);
  await expect(page.locator(".bottom-nav")).toBeVisible();
  const layout = await page.evaluate(() => {
    const nav = document.querySelector(".bottom-nav")!.getBoundingClientRect();
    const profile = document.querySelector(".profile-view")!.getBoundingClientRect();
    return {
      viewport: [innerWidth, innerHeight],
      docWidth: document.documentElement.scrollWidth,
      navTop: nav.top,
      navBottom: nav.bottom,
      profileBottom: profile.bottom,
      pageHeight: document.documentElement.scrollHeight,
    };
  });
  expect(layout.viewport).toEqual([390, 844]);
  expect(layout.docWidth).toBeLessThanOrEqual(390);
  expect(layout.navBottom).toBe(844);
  expect(layout.pageHeight).toBeGreaterThan(844);
  await page.screenshot({ path: testInfo.outputPath("model-profile-390x844.png") });
  expect(runtimeErrors.get(page)).toEqual([]);
  console.log("LAYOUT_PASS", JSON.stringify(layout));
});
