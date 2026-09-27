export type MessagesRoute = { screen: "messages" } | { screen: "chat"; slug: string };
export function messagesPath(slug: string): string;
export function messagesRouteFromLocation(pathname: string): MessagesRoute | null;
