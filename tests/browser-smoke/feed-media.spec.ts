import { expect, test } from "@playwright/test";
import { installMiniAppBrowserMocks } from "./miniapp-mocks";

test("prioritizes the first feed photos and reserves their image space while they load", async ({ page }) => {
  const { errors } = await installMiniAppBrowserMocks(page);
  let releaseImage!: () => void;
  const imageCanLoad = new Promise<void>((resolve) => { releaseImage = resolve; });
  await page.route("http://127.0.0.1:5189/media/assets/fixture-post-01", async (route) => {
    await imageCanLoad;
    await route.fulfill({ status: 200, contentType: "image/jpeg", path: "design/assets/creator-profile/post-01.jpg" });
  });

  await page.goto("/", { waitUntil: "domcontentloaded" });
  const firstImage = page.locator(".post-card img").first();
  await expect(firstImage).toHaveAttribute("loading", "eager");
  await expect(firstImage).toHaveAttribute("fetchpriority", "high");
  await expect(firstImage).toHaveAttribute("width", "960");
  await expect(firstImage).toHaveAttribute("height", "1280");
  await expect(page.locator(".post-card").first()).toContainText("Browser smoke post 1");
  await expect.poll(async () => firstImage.evaluate((image: HTMLImageElement) => image.naturalWidth)).toBe(0);
  const box = await firstImage.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.height).toBeGreaterThan(100);
  await expect(firstImage).toHaveCSS("background-size", "cover");

  releaseImage();
  await expect.poll(async () => firstImage.evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
  const loadedBox = await firstImage.boundingBox();
  expect(loadedBox).not.toBeNull();
  expect(loadedBox!.height).toBeCloseTo(box!.height, 0);
  expect(errors).toEqual([]);
});
