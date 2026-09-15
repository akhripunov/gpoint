export function validateSlug(slug: string): string {
  if (!/^[a-z0-9](?:[a-z0-9-]{1,48}[a-z0-9])?$/.test(slug)) throw new Error('invalid creator slug');
  return slug;
}
export function encodeCursor(createdAt: Date, id: string): string { return encodeURIComponent(`${createdAt.toISOString()}|${id}`); }
export function decodeCursor(cursor: string): { createdAt: Date; id: string } {
  try { const [date, id] = decodeURIComponent(cursor).split('|'); const createdAt = new Date(date); if (!id || Number.isNaN(createdAt.valueOf())) throw new Error(); return { createdAt, id }; } catch { throw new Error('invalid cursor'); }
}
export function trendingScore(profileViews24h: number, follows24h: number, vipClicks24h: number): number { return profileViews24h + follows24h * 4 + vipClicks24h * 8; }
export function popularScore(profileViews: number, follows: number, vipClicks: number): number { return profileViews + follows * 4 + vipClicks * 8; }
