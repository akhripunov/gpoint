import test from "node:test";
import assert from "node:assert/strict";
import {
  canonicalModelProfileUrl,
  modelHandleFromStartParam,
  modelProfilePath,
  modelStartParamFromUrl,
  modelRouteFromLocation,
  telegramModelStartParam,
} from "../src/model-routing.js";

test("model paths and canonical share URLs use the public handle", () => {
  assert.equal(modelProfilePath("alina"), "/model/alina");
  assert.equal(canonicalModelProfileUrl("alina"), "https://gpoint.one/model/alina");
});

test("Telegram model start params resolve to the same profile route", () => {
  assert.deepEqual(modelRouteFromLocation("/", "model_alina"), {
    screen: "profile",
    handle: "alina",
  });
  assert.equal(modelHandleFromStartParam("model_alina"), "alina");
  assert.equal(modelStartParamFromUrl("", "#tgWebAppStartParam=model_alina"), "model_alina");
  assert.equal(modelStartParamFromUrl("?start_param=model_alina", ""), "model_alina");
  assert.equal(modelHandleFromStartParam("affiliate_alina"), null);
  assert.equal(modelHandleFromStartParam("model_not valid"), null);
});

test("Telegram SDK start_param takes precedence and launch URL parameters are supported", () => {
  const previousWindow = globalThis.window;
  try {
    globalThis.window = {
      Telegram: { WebApp: { initDataUnsafe: { start_param: "model_from-sdk" } } },
      location: { search: "?tgWebAppStartParam=model_from-url", hash: "" },
    };
    assert.equal(telegramModelStartParam(), "model_from-sdk");
    globalThis.window.Telegram.WebApp.initDataUnsafe.start_param = undefined;
    assert.equal(telegramModelStartParam(), "model_from-url");
    globalThis.window.location.search = "";
    globalThis.window.location.hash = "#start_param=model_from-hash";
    assert.equal(telegramModelStartParam(), "model_from-hash");
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});

test("direct model URLs resolve the selected handle and unknown handles stay on that route", () => {
  assert.deepEqual(modelRouteFromLocation("/model/alina"), {
    screen: "profile",
    handle: "alina",
  });
  assert.deepEqual(modelRouteFromLocation("/model/unknown"), {
    screen: "profile",
    handle: "unknown",
  });
  assert.deepEqual(modelRouteFromLocation("/m/alina"), { screen: "home" });
});

test("existing admin and creator routes remain intact", () => {
  assert.deepEqual(modelRouteFromLocation("/admin"), { screen: "admin" });
  assert.deepEqual(modelRouteFromLocation("/creator/dashboard"), { screen: "creator" });
});
