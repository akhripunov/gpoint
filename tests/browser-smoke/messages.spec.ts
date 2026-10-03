import { expect, test } from "@playwright/test";
import { installMiniAppBrowserMocks } from "./miniapp-mocks";

test.beforeEach(async ({ page }, testInfo) => {
  await installMiniAppBrowserMocks(page, {
    ageAssuranceRequired: testInfo.title.includes("age assurance blocks") || testInfo.title.includes("age assurance self-attestation"),
    aiAvailable: !testInfo.title.includes("provider-disabled"),
    pendingReply: testInfo.title.includes("pending send"),
    existingHistory: testInfo.title.includes("existing conversation"),
  });
});

test("Messages nav replaces Following while feed tabs retain it", async ({ page }) => {
  await page.goto("/messages", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Messages", exact: true })).toBeVisible();
  const nav = page.locator(".bottom-nav");
  await expect(nav.getByRole("button")).toHaveText(["For You", "Reels", "Messages", "Explore", "Profile"]);
  await expect(page.getByRole("button", { name: /Models Hub/ })).toBeVisible();
  await page.getByRole("button", { name: "For You" }).click();
  await expect(page.getByRole("tab", { name: "Following", exact: true })).toBeVisible();
});

test("creator profile opens its chat; consent and text exchange work", async ({ page }) => {
  await page.goto("/model/models_hub", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Models Hub" })).toBeVisible();
  await page.getByRole("button", { name: "Message Models Hub" }).click();
  await expect(page).toHaveURL(/\/messages\/models_hub$/);
  await expect(page.getByRole("heading", { name: "AI character conversation" })).toBeVisible();
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.locator(".chat-welcome")).toBeVisible();
  const composer = page.getByRole("textbox", { name: "Message" });
  await expect(composer).toBeVisible();
  await composer.fill("Hello!");
  await composer.press("Enter");
  await expect(page.getByText("Hello!", { exact: true })).toBeVisible();
  await expect(page.getByText("Thanks for your message.", { exact: true })).toBeVisible();
  await expect(page.locator(".bottom-nav")).toHaveCount(0);
});

test("one-time onboarding persists when memory is enabled across reload and existing-chat navigation", async ({ page }) => {
  await page.goto("/messages/models_hub", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "AI character conversation" })).toBeVisible();
  const memory = page.getByRole("checkbox", { name: /Remember things/ });
  await memory.check();
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText("Previously saved message", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "AI character conversation" })).toHaveCount(0);

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "AI character conversation" })).toHaveCount(0);
  await expect(page.locator(".chat-welcome")).toBeVisible();
  await page.getByRole("button", { name: "Back to messages" }).click();
  await expect(page.getByRole("heading", { name: "Messages", exact: true })).toBeVisible();
  await page.getByRole("button", { name: /Models Hub/ }).click();
  await expect(page.getByRole("heading", { name: "AI character conversation" })).toHaveCount(0);
  await expect(page.getByRole("checkbox", { name: /Remember things/ })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible();
});

test("memory remains opted out after Continue, reload, and Telegram Mini App reopen", async ({ page }) => {
  await page.goto("/messages/models_hub", { waitUntil: "domcontentloaded" });
  const memory = page.getByRole("checkbox", { name: /Remember things/ });
  await expect(memory).not.toBeChecked();
  await page.getByRole("button", { name: "Continue" }).click();
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "AI character conversation" })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible();
  await page.goto("/messages", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: /Models Hub/ }).click();
  await expect(page.getByRole("heading", { name: "AI character conversation" })).toHaveCount(0);
});

test("an existing conversation without a saved current disclosure remains gated once, then opens history", async ({ page }) => {
  await page.goto("/messages/models_hub", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "AI character conversation" })).toBeVisible();
  await expect(page.getByText("Previously saved message", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText("Previously saved message", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Back to messages" }).click();
  await page.getByRole("button", { name: /Models Hub/ }).click();
  await expect(page.getByRole("heading", { name: "AI character conversation" })).toHaveCount(0);
  await expect(page.getByText("Previously saved message", { exact: true })).toBeVisible();
});

test("pending send polls history without reposting and merges the persisted bubble", async ({ page }) => {
  const sendRequests: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/messages/creator/")) {
      sendRequests.push(request.url());
    }
  });
  await page.goto("/messages/models_hub", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Continue" }).click();
  const composer = page.getByRole("textbox", { name: "Message" });
  await composer.fill("Accepted once");
  await composer.press("Enter");

  const bubble = page.locator(".chat-bubble.mine").filter({ hasText: "Accepted once" });
  await expect(bubble).toHaveCount(1);
  await expect(bubble.getByRole("button", { name: "Retry" })).toHaveCount(0);
  await page.waitForTimeout(6_000);
  expect(sendRequests).toHaveLength(1);
  await expect(page.getByText(/couldn't confirm delivery/i)).toHaveCount(0);
});

test("age assurance blocks Messages behind the global onboarding gate", async ({ page }) => {
  await page.goto("/messages/models_hub", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Продолжить в GPoint" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Message" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Мне 18+" })).toBeVisible();
});

test("age assurance self-attestation persists and returns to the requested Messages route", async ({ page }) => {
  await page.goto("/messages", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "Продолжить в GPoint" })).toBeVisible();
  await page.getByRole("button", { name: "Мне 18+" }).click();
  await expect(page.getByRole("heading", { name: "Messages", exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/messages$/);
});

test("provider-disabled chat remains readable and blocks sending without a fake reply", async ({ page }) => {
  await page.goto("/messages/models_hub", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "AI character conversation" })).toBeVisible();
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("status")).toContainText("AGATA is temporarily unavailable");
  const composer = page.getByRole("textbox", { name: "Message" });
  await expect(composer).toBeDisabled();
  await expect(page.getByRole("button", { name: "Send message" })).toBeDisabled();
  await expect(page.getByText("Thanks for your message.", { exact: true })).toHaveCount(0);
});
