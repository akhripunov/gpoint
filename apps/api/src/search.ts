import { Controller, Get, Query } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';

const prisma = new PrismaClient();
type SearchHit = { id: string; type: 'creator' | 'post'; displayName?: string; slug?: string; caption?: string; creatorId?: string };

export class SearchService {
  private readonly enabled = process.env.SEARCH_ENABLED === 'true' && !!process.env.MEILISEARCH_URL;
  private async meili(index: string, q: string): Promise<SearchHit[]> {
    const response = await fetch(`${process.env.MEILISEARCH_URL!.replace(/\/$/, '')}/indexes/${index}/search`, { method: 'POST', headers: { 'content-type': 'application/json', ...(process.env.MEILISEARCH_MASTER_KEY ? { authorization: `Bearer ${process.env.MEILISEARCH_MASTER_KEY}` } : {}) }, body: JSON.stringify({ q, limit: 20 }) });
    if (!response.ok) throw new Error(`meilisearch ${response.status}`);
    return ((await response.json()) as { hits?: SearchHit[] }).hits ?? [];
  }
  async search(q: string) {
    const query = q.trim().slice(0, 100); if (!query) return { query: '', items: [], provider: this.enabled ? 'meilisearch' : 'postgres-fallback' };
    if (this.enabled) { try { const [creators, posts] = await Promise.all([this.meili('creators', query), this.meili('posts', query)]); return { query, items: [...creators, ...posts], provider: 'meilisearch' }; } catch { /* derived search outage must not break GPoint */ } }
    const [creators, posts] = await Promise.all([
      prisma.creator.findMany({ where: { profile: { status: 'APPROVED' }, OR: [{ displayName: { contains: query, mode: 'insensitive' } }, { slug: { contains: query, mode: 'insensitive' } }] }, take: 20, select: { id: true, displayName: true, slug: true } }),
      prisma.post.findMany({ where: { status: 'PUBLISHED', creator: { profile: { status: 'APPROVED' } }, OR: [{ caption: { contains: query, mode: 'insensitive' } }, { sanitizedText: { contains: query, mode: 'insensitive' } }] }, take: 20, select: { id: true, caption: true, creatorId: true } }),
    ]);
    return { query, provider: 'postgres-fallback', items: [...creators.map(x => ({ ...x, type: 'creator' as const })), ...posts.map(x => ({ ...x, type: 'post' as const }))] };
  }
}

@Controller('search') export class SearchController { private readonly service = new SearchService(); @Get() search(@Query('q') q?: string) { return this.service.search(q ?? ''); } }
