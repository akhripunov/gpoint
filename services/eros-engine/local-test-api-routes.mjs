export function isAgataMessageHistoryPath(requestTarget) {
  if (typeof requestTarget !== 'string') return false;
  let pathname;
  try {
    pathname = new URL(requestTarget, 'http://local-test.invalid').pathname;
  } catch {
    return false;
  }
  return /^\/internal\/v1\/users\/[^/]+\/characters\/[^/]+\/messages$/.test(pathname);
}
