import { Controller, Get, Headers, Post, Query, UnauthorizedException } from '@nestjs/common';
import { PrismaClient } from '@prisma/client';
import { resolveSession } from './session.js';

const prisma = new PrismaClient();
type SearchHit = { id: string; type: 'creator' | 'post'; displayName?: string; slug?: string; caption?: string; creatorId?: string };

export class SearchService {
  private readonly enabled = process.env.SEARCH_ENABLED === 'true' && !!process.env.MEILISEARCH_URL;
  private async meili(index: string, q: string): Promise<SearchHit[]> {
    const response = await fetch(`${process.env.MEILISEARCH_URL!.replace(/\/$/, '')}/indexes/${index}/search`, { method: 'POST', headers: { 'content-type': 'application/json', ...(process.env.MEILISEARCH_MASTER_KEY ? { authorization: `Bearer ${process.env.MEILISEARCH_MASTER_KEY}` } : {}) }, body: JSON.stringify({ q, limit: 20 }) });
    if (!response.ok) throw new Error(`meilisearch ${response.status}`);
    return ((await response.json()) as { hits?: SearchHit[] }).hits ?? [];
  }
  private async waitTask(response: Response, base: string, headers: Record<string, string>) {
    if (!response.ok) throw new Error(`meilisearch task ${response.status}`);
    const taskUid = (await response.json() as { taskUid?: number }).taskUid;
    if (taskUid === undefined) return;
    for (let attempt = 0; attempt < 30; attempt++) { const task = await fetch(`${base}/tasks/${taskUid}`, { headers }); if (task.ok) { const state = (await task.json() as { status?: string }).status; if (state === 'succeeded') return; if (state === 'failed' || state === 'canceled') throw new Error(`meilisearch task ${state}`); } await new Promise(resolve => setTimeout(resolve, 100)); }
    throw new Error('meilisearch task timeout');
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
  async reindex() {
    if (!this.enabled) throw new Error('Meilisearch is disabled');
    const base = process.env.MEILISEARCH_URL!.replace(/\/$/, ''); const headers = { 'content-type': 'application/json', authorization: `Bearer ${process.env.MEILISEARCH_MASTER_KEY ?? ''}` };
    const [creators, posts] = await Promise.all([
      prisma.creator.findMany({ where: { profile: { status: 'APPROVED' } }, select: { id: true, displayName: true, slug: true, profile: { select: { bio: true, tagsJson: true } } } }),
      prisma.post.findMany({ where: { status: 'PUBLISHED', creator: { profile: { status: 'APPROVED' } } }, select: { id: true, caption: true, sanitizedText: true, creatorId: true } }),
    ]);
    for (const [index, documents] of [['creators', creators.map(x => ({ id: x.id, displayName: x.displayName, slug: x.slug, bio: x.profile?.bio ?? '', tags: x.profile?.tagsJson ?? '[]' }))], ['posts', posts.map(x => ({ id: x.id, caption: x.caption ?? '', sanitizedText: x.sanitizedText ?? '', creatorId: x.creatorId }))] ] as const) {
      const create = await fetch(`${base}/indexes`, { method: 'POST', headers, body: JSON.stringify({ uid: index, primaryKey: 'id' }) }); if (!create.ok && create.status !== 400 && create.status !== 409) throw new Error(`meilisearch index ${create.status}`);
      const clear = await fetch(`${base}/indexes/${index}/documents`, { method: 'DELETE', headers }); await this.waitTask(clear, base, headers);
      const add = await fetch(`${base}/indexes/${index}/documents`, { method: 'POST', headers, body: JSON.stringify(documents) }); await this.waitTask(add, base, headers);
    }
    return { indexed: { creators: creators.length, posts: posts.length }, provider: 'meilisearch' };
  }
}

@Controller('search') export class SearchController { private readonly service = new SearchService(); @Get() search(@Query('q') q?: string) { return this.service.search(q ?? ''); } @Post('reindex') async reindex(@Headers() h: Record<string, string | undefined>) { const uid = resolveSession(h.authorization?.replace(/^Bearer\s+/i, '')); if (!uid || !(await prisma.userRole.findUnique({ where: { userId_role: { userId: uid, role: 'ADMIN' } } }))) throw new UnauthorizedException('admin required'); return this.service.reindex(); } }
