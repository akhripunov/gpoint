import React from "react";

type BootStage =
  | "BOOT_SCRIPT_STARTED"
  | "TELEGRAM_GLOBAL_READY"
  | "REACT_ROOT_CREATED"
  | "REACT_RENDER_CALLED"
  | "AUTH_REQUEST_STARTED"
  | "AUTH_SUCCESS"
  | "ME_REQUEST_STARTED"
  | "ME_SUCCESS"
  | "FEED_REQUEST_STARTED"
  | "FEED_SUCCESS"
  | "ROUTE_RESOLUTION_STARTED"
  | "ROUTE_RESOLUTION_FINISHED"
  | "MAIN_VIEW_RENDER_STARTED"
  | "MAIN_VIEW_RENDER_FINISHED"
  | "POST_RENDER_WATCHDOG"
  | "RUNTIME_ERROR";

const buildSha = String(__GPOINT_BUILD_SHA__);
const diagnosticId =
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `boot-${Date.now()}-${Math.random().toString(36).slice(2)}`;
const startedAt = Date.now();

function text(value: unknown, limit = 600) {
  return String(value ?? "").replace(/[\r\n\t]+/g, " ").slice(0, limit);
}

function safeTelegram() {
  const webApp = (window as any).Telegram?.WebApp;
  return {
    platform: text(webApp?.platform, 80) || "unknown",
    version: text(webApp?.version, 40) || "unknown",
  };
}

function safeDom() {
  const root = document.getElementById("root");
  const body = document.body;
  const rect = (node: Element | null) => {
    if (!node) return null;
    const box = node.getBoundingClientRect();
    return { x: box.x, y: box.y, width: box.width, height: box.height };
  };
  const style = root?.firstElementChild
    ? getComputedStyle(root.firstElementChild)
    : null;
  const center = document.elementFromPoint(
    Math.max(0, window.innerWidth / 2),
    Math.max(0, window.innerHeight / 2),
  );
  const centerStyle = center ? getComputedStyle(center) : null;
  return {
    readyState: document.readyState,
    bodyChildElementCount: body?.childElementCount ?? 0,
    rootExists: Boolean(root),
    rootChildElementCount: root?.childElementCount ?? 0,
    rootHtmlLength: root?.innerHTML.length ?? 0,
    rootRect: rect(root),
    bodyRect: rect(body),
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    clientWidth: document.documentElement.clientWidth,
    clientHeight: document.documentElement.clientHeight,
    appStyle: style
      ? {
          display: style.display,
          visibility: style.visibility,
          opacity: style.opacity,
          position: style.position,
          zIndex: style.zIndex,
          width: style.width,
          height: style.height,
          overflow: style.overflow,
        }
      : null,
    centerElement: center
      ? {
          tag: center.tagName,
          id: text(center.id, 80),
          className: text(typeof center.className === "string" ? center.className : "", 120),
          rect: rect(center),
          display: centerStyle?.display,
          visibility: centerStyle?.visibility,
          opacity: centerStyle?.opacity,
          zIndex: centerStyle?.zIndex,
        }
      : null,
  };
}

const reportQueue: Record<string, unknown>[] = [];
let reportTimer: number | undefined;
function sendReports() {
  if (reportTimer !== undefined) return;
  reportTimer = window.setTimeout(() => {
    reportTimer = undefined;
    const reports = reportQueue.splice(0, 8);
    if (!reports.length) return;
    void fetch("/api/diagnostics/client", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ reports }),
      cache: "no-store",
      credentials: "same-origin",
      keepalive: true,
    }).catch(() => undefined);
  }, 50);
}

export function markBootStage(stage: BootStage, extra: Record<string, unknown> = {}) {
  const event = {
    timestamp: new Date().toISOString(),
    buildSha,
    diagnosticId,
    stage,
    pathname: location.pathname,
    searchPresent: Boolean(location.search),
    hashPresent: Boolean(location.hash),
    elapsedMs: Date.now() - startedAt,
    telegram: safeTelegram(),
    ...extra,
  };
  reportQueue.push(event);
  console.info(`[GPoint] boot ${stage}`, event);
  sendReports();
}

function reportError(stage: BootStage, error: unknown) {
  const value = error instanceof Error ? error : new Error(String(error));
  markBootStage(stage, {
    errorName: text(value.name, 120),
    errorMessage: text(value.message),
    errorStack: text(value.stack, 1200),
    dom: safeDom(),
  });
}

function renderFailsafe() {
  const root = document.getElementById("root");
  if (!root || root.childElementCount > 0) return;
  const card = document.createElement("div");
  card.setAttribute("data-gpoint-boot-failsafe", "true");
  card.style.cssText =
    "box-sizing:border-box;min-height:100vh;display:grid;place-items:center;padding:24px;background:#090d16;color:#f7f2ee;font:16px/1.5 system-ui,sans-serif;text-align:center";
  card.innerHTML = `<div><h1 style="margin:0 0 12px;font-size:22px">GPoint failed to render</h1><p style="margin:0;color:#b8b8c2">Build: ${text(buildSha, 12)}<br>Diagnostic ID: ${text(diagnosticId, 80)}</p></div>`;
  root.appendChild(card);
  reportError("RUNTIME_ERROR", new Error("React root remained empty after bootstrap window"));
}

markBootStage("BOOT_SCRIPT_STARTED");
window.addEventListener("error", (event) => reportError("RUNTIME_ERROR", event.error ?? new Error(text(event.message))));
window.addEventListener("unhandledrejection", (event) => reportError("RUNTIME_ERROR", event.reason));
const originalConsoleError = console.error;
console.error = (...args: unknown[]) => {
  originalConsoleError(...args);
  reportError("RUNTIME_ERROR", new Error(args.map((arg) => text(arg, 300)).join(" ")));
};
window.setTimeout(() => {
  const dom = safeDom();
  markBootStage("POST_RENDER_WATCHDOG", { dom });
  renderFailsafe();
}, 3500);

export class BootErrorBoundary extends React.Component<React.PropsWithChildren, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(error: unknown) { reportError("RUNTIME_ERROR", error); }
  render() {
    if (!this.state.failed) return this.props.children;
    return React.createElement("div", { "data-gpoint-error-boundary": "true", className: "app-shell" },
      React.createElement("main", { className: "page" },
        React.createElement("div", { className: "empty" },
          React.createElement("h3", null, "GPoint failed to render"),
          React.createElement("p", null, `Build: ${buildSha.slice(0, 12)} · Diagnostic: ${diagnosticId}`),
        ),
      ),
    );
  }
}

export { buildSha, diagnosticId, safeDom };
