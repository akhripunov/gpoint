import { randomUUID } from 'node:crypto';
export type SessionRecord = { userId: string; expiresAt: number };
const sessions = new Map<string, SessionRecord>();
export function createSession(userId: string, now = Date.now()): string { const token=randomUUID(); sessions.set(token,{userId,expiresAt:now+3600_000}); return token; }
export function resolveSession(token: string|undefined, now = Date.now()): string|null { if(!token)return null; const s=sessions.get(token); if(!s||s.expiresAt<=now){sessions.delete(token);return null;} return s.userId; }
