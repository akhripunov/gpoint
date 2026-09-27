export function messagesPath(slug) {
  return `/messages/${encodeURIComponent(slug)}`;
}

export function messagesRouteFromLocation(pathname) {
  if (pathname === "/messages" || pathname === "/messages/") {
    return { screen: "messages" };
  }
  const match = pathname.match(/^\/messages\/([^/]+)\/?$/);
  if (!match) return null;
  try {
    return { screen: "chat", slug: decodeURIComponent(match[1]) };
  } catch {
    return { screen: "chat", slug: match[1] };
  }
}
