export function shouldShowAvatarImage(src: string | null, failedSrc: string | null): boolean;
export function avatarFallbackText(name: string | null | undefined, length?: number): string;
export function creatorChannelAvatarSource(creator: { channelAvatarUrl?: string | null } | null | undefined): string | null;
export function messageChannelAvatarSource(creator: { channelAvatarUrl?: string | null } | null | undefined): string | null;
