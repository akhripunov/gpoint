import type { Page, Route } from "@playwright/test";

const creator = {
  id: "browser-smoke-model-id",
  slug: "models_hub",
  displayName: "Models Hub",
  bio: "A creator profile for browser smoke.",
  avatarUrl: null,
  coverUrl: null,
  verified: true,
  vipPriceStars: null,
  tags: ["smoke"],
  posts: [],
  following: false,
  followersCount: 248,
};

const modelPost = (index: number) => ({
  id: `browser-smoke-post-${index}`,
  type: "IMAGE",
  caption: `Browser smoke post ${index}`,
  media: [],
  publishedAt: "2026-09-23T09:00:00.000Z",
  likedByMe: false,
  likesCount: 3,
  following: false,
  creator,
});

export async function installMiniAppBrowserMocks(
  page: Page,
  options: { telegramStartParam?: string } = {},
) {
  const errors: string[] = [];
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

  await page.route("**/*", async (route: Route) => {
    const url = new URL(route.request().url());
    if (url.origin === "http://127.0.0.1:5189") {
      if (url.pathname === "/api/diagnostics/client") {
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

    const path = url.pathname;
    const items = [modelPost(1), modelPost(2), modelPost(3)];
    let data: unknown;
    let status = 200;
    if (path === "/auth/browser") data = { accessToken: "browser-smoke-token" };
    else if (path === "/me") {
      data = {
        id: "browser-smoke-user",
        telegramUsername: "smoke",
        firstName: "Smoke",
        isPremium: false,
      };
    } else if (path === "/feed/for-you") {
      data = { items: [modelPost(1)], nextCursor: null };
    } else if (path === "/creators/models_hub") data = creator;
    else if (path === "/creators/unknown-model") {
      status = 404;
      data = { message: "Creator unavailable" };
    } else if (path === "/creators/browser-smoke-model-id/posts") {
      data = { items, nextCursor: null };
    } else {
      status = 404;
      data = { message: `No browser smoke fixture for ${path}` };
    }

    console.log("API_MOCK", route.request().method(), path, status);
    await route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(data),
    });
  });

  return { errors };
}
