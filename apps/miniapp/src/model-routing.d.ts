export type ModelRoute =
  | { screen: "profile"; handle: string }
  | { screen: "admin" | "creator" | "home" };

export function modelProfilePath(handle: string): string;
export function canonicalModelProfileUrl(handle: string): string;
export function modelHandleFromStartParam(value?: string | null): string | null;
export function telegramModelStartParam(): string | null;
export function modelStartParamFromUrl(search: string, hash: string): string | null;
export function modelRouteFromLocation(pathname: string, startParam?: string | null): ModelRoute;
