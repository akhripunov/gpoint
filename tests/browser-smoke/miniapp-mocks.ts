import type { Page, Route } from "@playwright/test";
import path from "node:path";

const fixtureImages = path.resolve(__dirname, "../../design/assets/creator-profile");

const creator = {
  id: "browser-smoke-model-id",
  slug: "models_hub",
  displayName: "Models Hub",
  bio: "A softer, bolder you. Beauty. Lifestyle. Real moments. ♡",
  avatarUrl: "/media/assets/fixture-avatar",
  coverUrl: null,
  verified: true,
  vipPriceStars: null,
  tags: ["Fashion", "Lifestyle", "Beauty"],
  posts: [],
  following: false,
  followersCount: 248,
};

const modelPost = (index: number) => ({
  id: `browser-smoke-post-${index}`,
  type: "IMAGE",
  caption: `Browser smoke post ${index}`,
  media: [{ id: `fixture-post-${index}`, kind: "IMAGE", readUrl: `/media/assets/fixture-post-${String(index).padStart(2, "0")}`, width: 960, height: 1280, blurHash: "LEHV6nWB2yk8pyo0adR*.7kCMdnj" }],
  publishedAt: "2026-09-23T09:00:00.000Z",
  likedByMe: false,
  likesCount: 3,
  following: false,
  creator,
});

export async function installMiniAppBrowserMocks(
  page: Page,
  options: { telegramStartParam?: string; ageAssuranceRequired?: boolean; ageAttestationWriteStatus?: number; aiAvailable?: boolean; pendingReply?: boolean; chatHistoryAccepted?: boolean; memoryOptIn?: boolean; existingHistory?: boolean; browserAuthDelayMs?: number; ageAssuranceDelayMs?: number } = {},
) {
  const errors: string[] = [];
  let chatHistoryAccepted = options.chatHistoryAccepted ?? false;
  let memoryOptIn = options.memoryOptIn ?? false;
  let onboardingCompletedAt = chatHistoryAccepted ? "2026-10-03T10:00:00.000Z" : null;
  const chatMessages: Array<{ id: string; clientMessageId?: string; role: "user" | "assistant"; text: string; occurredAt: string }> = options.existingHistory ? [
    { id: "existing-user-message", role: "user", text: "Previously saved message", occurredAt: "2026-10-02T10:00:00.000Z" },
    { id: "existing-assistant-message", role: "assistant", text: "Previously saved reply", occurredAt: "2026-10-02T10:01:00.000Z" },
  ] : [];
  let ageAssuranceRequired = options.ageAssuranceRequired ?? false;
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });

  await page.addInitScript(({ telegramStartParam }) => {
    Object.defineProperty(navigator, "share", {
      configurable: true,
      value: async ({ url }: { url: string }) => {
        (window as Window & { __sharedUrl?: string }).__sharedUrl = url;
      },
    });
    (window as Window & { Telegram?: unknown }).Telegram = {
      WebApp: { initDataUnsafe: { start_param: telegramStartParam } },
    };
  }, options);

  await page.route("http://127.0.0.1:5199/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const apiPath = url.pathname;
    if (route.request().method() === "OPTIONS") {
      await route.fulfill({ status: 204, headers: { "access-control-allow-origin": "http://127.0.0.1:5189", "access-control-allow-credentials": "true", "access-control-allow-methods": "GET,POST,DELETE,OPTIONS", "access-control-allow-headers": "authorization,content-type" }, body: "" });
      return;
    }
    const items = Array.from({ length: 12 }, (_, index) => modelPost(index + 1));
    let data: unknown;
    let status = 200;
    if (apiPath === "/auth/browser") {
      if (options.browserAuthDelayMs) await new Promise((resolve) => setTimeout(resolve, options.browserAuthDelayMs));
      data = { accessToken: "browser-smoke-token" };
    }
    else if (apiPath === "/me") data = { id: "browser-smoke-user", telegramUsername: "smoke", firstName: "Smoke", isPremium: false };
    else if (apiPath === "/feed/for-you") data = { items: [modelPost(1)], nextCursor: null };
    else if (apiPath === "/explore") data = { items: [], nextCursor: null };
    else if (apiPath === "/events") data = { recorded: true };
    else if (apiPath === "/creators/models_hub") data = creator;
    else if (apiPath === "/creators/unknown-model") { status = 404; data = { message: "Creator unavailable" }; }
    else if (apiPath === "/creators/browser-smoke-model-id/posts") data = { items, nextCursor: null };
    else if (apiPath === "/age-assurance/state") {
      if (options.ageAssuranceDelayMs) await new Promise((resolve) => setTimeout(resolve, options.ageAssuranceDelayMs));
      data = { required: ageAssuranceRequired, eligible: !ageAssuranceRequired, status: ageAssuranceRequired ? "pending" : "self_attested", method: ageAssuranceRequired ? "unknown" : "self_attested", jurisdiction: "GLOBAL", assertedAt: ageAssuranceRequired ? null : "2026-09-27T10:00:00.000Z", expiresAt: null, selfAttestationAllowed: true };
    }
    else if (apiPath === "/age-assurance/self-attest" && route.request().method() === "POST") {
      if (options.ageAttestationWriteStatus) {
        status = options.ageAttestationWriteStatus;
        data = { code: "AGE_ASSURANCE_UNAVAILABLE" };
      } else {
        ageAssuranceRequired = false;
        data = { required: false, eligible: true, status: "self_attested", method: "self_attested", jurisdiction: "GLOBAL", assertedAt: "2026-09-27T10:00:00.000Z", expiresAt: null, selfAttestationAllowed: true };
      }
    }
    else if (apiPath === "/messages/state") data = { enabled: true, chatHistory: chatHistoryAccepted, longTermMemory: memoryOptIn, disclosureRequired: !chatHistoryAccepted, onboardingCompleted: chatHistoryAccepted, disclosureVersion: 1, onboardingCompletedAt, memoryOptIn, memoryOptInAt: onboardingCompletedAt, ageAssuranceRequired, aiAvailable: options.aiAvailable ?? true };
    else if (apiPath === "/messages/consent" && route.request().method() === "POST") {
      chatHistoryAccepted = true;
      memoryOptIn = JSON.parse(route.request().postData() || "{}").longTermMemory;
      onboardingCompletedAt = onboardingCompletedAt ?? new Date().toISOString();
      data = { enabled: true, chatHistory: true, longTermMemory: memoryOptIn, disclosureRequired: false, onboardingCompleted: true, disclosureVersion: 1, onboardingCompletedAt, memoryOptIn, memoryOptInAt: onboardingCompletedAt, ageAssuranceRequired, aiAvailable: options.aiAvailable ?? true };
    }
    else if (apiPath === "/messages") data = { items: [{ creator: { id: creator.id, slug: creator.slug, displayName: creator.displayName, avatarUrl: creator.avatarUrl, verified: creator.verified }, lastMessagePreview: "Hello there", lastMessageRole: "assistant", lastMessageAt: "2026-09-27T10:00:00.000Z" }], nextCursor: null };
    else if (apiPath === `/messages/creator/${creator.id}` && route.request().method() === "GET") {
      data = { creator, items: [...chatMessages], nextCursor: null };
    }
    else if (apiPath === `/messages/creator/${creator.id}` && route.request().method() === "POST") {
      const body = JSON.parse(route.request().postData() || "{}");
      const occurredAt = new Date().toISOString();
      const response = { id: "assistant-reply-1", role: "assistant" as const, text: "Thanks for your message.", occurredAt };
      chatMessages.push({ id: `server-${body.clientMessageId}`, clientMessageId: body.clientMessageId, role: "user", text: body.text, occurredAt });
      if (options.pendingReply) {
        data = { userMessageId: body.clientMessageId, status: "pending" };
      } else {
        chatMessages.push(response);
        data = { userMessageId: body.clientMessageId, response };
      }
    } else { status = 404; data = { message: `No browser smoke fixture for ${apiPath}` }; }
    console.log("API_MOCK", route.request().method(), apiPath, status);
    await route.fulfill({ status, headers: { "access-control-allow-origin": "http://127.0.0.1:5189", "access-control-allow-credentials": "true" }, contentType: "application/json", body: JSON.stringify(data) });
  });

  await page.route("**/*", async (route: Route) => {
    const url = new URL(route.request().url());
    if (url.origin === "http://127.0.0.1:5199") {
      await route.fallback();
      return;
    }
    if (url.origin === "http://127.0.0.1:5189") {
      const fixture = /^\/media\/assets\/fixture-(avatar|post-(?:0[1-9]|1[0-2]))$/.exec(url.pathname);
      if (fixture) {
        await route.fulfill({
          status: 200,
          contentType: "image/jpeg",
          path: path.join(fixtureImages, `${fixture[1]}.jpg`),
        });
      } else if (url.pathname === "/api/diagnostics/client") {
        await route.fulfill({ status: 204, body: "" });
      } else {
        await route.continue();
      }
      return;
    }
    if (url.origin !== "http://127.0.0.1:5199") {
      if (url.hostname === "telegram.org") {
        await route.fulfill({
          status: 200,
          contentType: "application/javascript",
          body: "window.Telegram = window.Telegram || { WebApp: { initDataUnsafe: {} } };",
        });
        return;
      }
      if (url.hostname === "fonts.googleapis.com") {
        await route.fulfill({ status: 200, contentType: "text/css", body: "" });
        return;
      }
      if (url.hostname === "fonts.gstatic.com") {
        await route.fulfill({ status: 204, body: "" });
        return;
      }
      await route.abort();
      return;
    }

    await route.abort();
  });

  return { errors };
}
