export function badgeText(count) { return Number.isSafeInteger(count) && count > 0 ? count > 99 ? '99+' : String(count) : null; }
export function initialPollDelay(random = Math.random) { return Math.floor(random() * 3000); }
export function unreadPollDelay(failures) { return Math.min(60000, 3000 * 2 ** Math.max(0, Math.min(failures - 1, 5))); }

/** One chat's visible ID queue; no unread arithmetic or message content. */
export function createReadQueue({ acknowledge, isActive, setTimer = setTimeout, clearTimer = clearTimeout }) {
  const visible = new Set();
  const confirmed = new Set();
  let timer = null, busy = false, disposed = false, failures = 0, nextAttemptAt = 0;
  const cancel = () => { if (timer !== null) clearTimer(timer); timer = null; };
  const schedule = () => {
    if (!disposed && !busy && timer === null && visible.size && isActive()) timer = setTimer(() => { timer = null; void flush(); }, Math.max(250, nextAttemptAt - Date.now()));
  };
  async function flush() {
    if (disposed || busy || !isActive()) return;
    cancel();
    const ids = [...visible].filter(id => !confirmed.has(id)).slice(0, 100);
    if (!ids.length) return;
    busy = true;
    try {
      const result = await acknowledge(ids);
      const acknowledged = new Set(result.acknowledgedMessageIds);
      if (acknowledged.size !== ids.length || ids.some(id => !acknowledged.has(id))) throw new Error('read acknowledgement mismatch');
      if (!disposed) for (const id of ids) { confirmed.add(id); visible.delete(id); }
      failures = 0; nextAttemptAt = 0;
    } catch {
      failures++; nextAttemptAt = Date.now() + unreadPollDelay(failures);
    } finally { busy = false; schedule(); }
  }
  return {
    view(id, intersects) {
      if (disposed || confirmed.has(id)) return;
      if (intersects) visible.add(id); else visible.delete(id);
      if (!visible.size) cancel(); else schedule();
    },
    wake: schedule, flush,
    dispose() { disposed = true; cancel(); visible.clear(); confirmed.clear(); },
  };
}
