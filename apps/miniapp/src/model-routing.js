const MODEL_HANDLE_PATTERN = /^[a-z0-9](?:[a-z0-9-]{1,48}[a-z0-9])?$/;

export function modelProfilePath(handle) {
  return `/model/${encodeURIComponent(handle)}`;
}

export function canonicalModelProfileUrl(handle) {
  return `https://gpoint.one${modelProfilePath(handle)}`;
}

export function modelHandleFromStartParam(value) {
  if (!value?.startsWith("model_")) return null;
  const handle = value.slice("model_".length);
  return MODEL_HANDLE_PATTERN.test(handle) ? handle : null;
}

export function telegramModelStartParam() {
  const webApp = window.Telegram?.WebApp;
  const sdkParam = webApp?.initDataUnsafe?.start_param;
  return sdkParam || modelStartParamFromUrl(window.location.search, window.location.hash);
}

export function modelStartParamFromUrl(search, hash) {
  for (const raw of [search, hash]) {
    const value = raw.replace(/^[?#]/, "");
    if (!value) continue;
    const params = new URLSearchParams(value);
    const launchParam =
      params.get("tgWebAppStartParam") ??
      params.get("start_param") ??
      params.get("startapp");
    if (launchParam) return launchParam;
  }
  return null;
}

export function modelRouteFromLocation(pathname, startParam) {
  const startHandle = modelHandleFromStartParam(startParam);
  if (startHandle) return { screen: "profile", handle: startHandle };

  const match = pathname.match(/^\/model\/([^/]+)\/?$/);
  if (match) {
    let handle = match[1];
    try {
      handle = decodeURIComponent(handle);
    } catch {
      // Keep malformed path text as a lookup key so it yields the unavailable state.
    }
    return { screen: "profile", handle };
  }
  if (pathname.startsWith("/admin")) return { screen: "admin" };
  if (pathname.startsWith("/creator")) return { screen: "creator" };
  return { screen: "home" };
}
