const allowedTelegramHosts = new Set(['t.me', 'telegram.me', 'telegram.dog']);
export function validateApprovedVipUrl(value: string): string {
  const url = new URL(value); if (url.protocol !== 'https:' || !allowedTelegramHosts.has(url.hostname) || url.username || url.password) throw new Error('unapproved VIP redirect');
  return url.toString();
}
export function isAdmin(roles: readonly string[]): boolean { return roles.includes('ADMIN'); }
export function assertOwner(ownerUserId: string, authenticatedUserId: string): void { if (ownerUserId !== authenticatedUserId) throw new Error('forbidden'); }
export function mediaKeyIsSafe(key: string): boolean { return key.length > 0 && key.length < 512 && !key.includes('..') && !key.startsWith('/') && !key.includes('\\'); }
