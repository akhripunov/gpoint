export function badgeText(count: number): string | null;
export function initialPollDelay(random?: () => number): number;
export function unreadPollDelay(failures: number): number;
export function chatMessagePresentation(role: 'user' | 'assistant' | 'system'): { side: 'mine' | 'theirs' | 'system'; incoming: boolean };
export type ReadQueue = { view(id: string, intersects: boolean): void; wake(): void; flush(): Promise<void>; dispose(): void };
export function createReadQueue(options: { acknowledge(ids: string[]): Promise<{ acknowledgedMessageIds: string[] }>; isActive(): boolean; setTimer?: typeof setTimeout; clearTimer?: typeof clearTimeout }): ReadQueue;
