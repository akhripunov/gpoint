import type { Page, Route } from "@playwright/test";
import path from "node:path";

const fixtureImages = path.resolve(process.cwd(), "design/assets/creator-profile");

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
  media: [{ id: `fixture-post-${index}`, kind: "IMAGE", readUrl: `/media/assets/fixture-post-${String(index).padStart(2, "0")}` }],
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

    const apiPath = url.pathname;
    const items = Array.from({ length: 12 }, (_, index) => modelPost(index + 1));
    let data: unknown;
    let status = 200;
    if (apiPath === "/auth/browser") data = { accessToken: "browser-smoke-token" };
    else if (apiPath === "/me") {
      data = {
        id: "browser-smoke-user",
        telegramUsername: "smoke",
        firstName: "Smoke",
        isPremium: false,
      };
    } else if (apiPath === "/feed/for-you") {
      data = { items: [modelPost(1)], nextCursor: null };
    } else if (apiPath === "/explore") {
      data = { items: [], nextCursor: null };
    } else if (apiPath === "/events") {
      data = { recorded: true };
    } else if (apiPath === "/creators/models_hub") data = creator;
    else if (apiPath === "/creators/unknown-model") {
      status = 404;
      data = { message: "Creator unavailable" };
    } else if (apiPath === "/creators/browser-smoke-model-id/posts") {
      data = { items, nextCursor: null };
    } else {
      status = 404;
      data = { message: `No browser smoke fixture for ${apiPath}` };
    }

    console.log("API_MOCK", route.request().method(), apiPath, status);
    await route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(data),
    });
  });

  return { errors };
}
