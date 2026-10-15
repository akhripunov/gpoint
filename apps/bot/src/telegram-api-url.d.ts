export type TelegramInternalPath = 'update' | 'chat-shared' | 'my-chat-member' | 'codex-approve';
export function telegramInternalUrl(path: TelegramInternalPath, options?: { baseUrl?: string; production?: boolean }): URL | null;
