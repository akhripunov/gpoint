export function shouldShowAvatarImage(src, failedSrc) {
  return typeof src === 'string' && src.trim().length > 0 && src !== failedSrc;
}

export function avatarFallbackText(name, length = 1) {
  return String(name ?? '').trim().slice(0, Math.max(1, length)).toUpperCase();
}

export function messageChannelAvatarSource(creator) {
  return typeof creator?.channelAvatarUrl === 'string' && creator.channelAvatarUrl.trim().length > 0
    ? creator.channelAvatarUrl
    : null;
}
