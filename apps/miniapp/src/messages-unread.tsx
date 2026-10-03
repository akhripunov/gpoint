import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { markCreatorMessagesRead, messagesUnread } from './api/client';
import { createReadQueue, initialPollDelay, unreadPollDelay, type ReadQueue } from './messages-unread-policy.js';
import type { ChatMessageDTO } from '../../../packages/contracts/src/index.js';

type UnreadContextValue = { userId: string; visible: boolean; hasSummary: boolean; total: number; counts: Map<string, number>; acknowledge(creatorId: string, ids: string[]): Promise<{ acknowledgedMessageIds: string[] }> };
const UnreadContext = createContext<UnreadContextValue | null>(null);
export function useMessagesUnread() {
  const state = useContext(UnreadContext);
  if (!state) throw new Error('MessagesUnreadProvider required');
  return state;
}

export function MessagesUnreadProvider({ userId, children }: { userId: string; children: ReactNode }) {
  const queryClient = useQueryClient();
  const [visible, setVisible] = useState(() => document.visibilityState === 'visible');
  const [ready, setReady] = useState(false);
  const failures = useRef(0);
  const priorSignature = useRef<string | null>(null);
  const key = useMemo(() => ['messages-unread', userId], [userId]);
  useEffect(() => {
    const update = () => setVisible(document.visibilityState === 'visible');
    document.addEventListener('visibilitychange', update);
    const timer = setTimeout(() => setReady(true), initialPollDelay());
    return () => { clearTimeout(timer); document.removeEventListener('visibilitychange', update); };
  }, []);
  const summary = useQuery({
    queryKey: key,
    queryFn: async ({ signal }) => {
      try { const result = await messagesUnread(signal); failures.current = 0; return result; }
      catch (error) { if (!signal.aborted) failures.current++; throw error; }
    },
    enabled: Boolean(userId && ready && visible), retry: false,
    refetchInterval: () => visible ? unreadPollDelay(failures.current) : false,
    refetchIntervalInBackground: false, refetchOnWindowFocus: true, refetchOnReconnect: true,
  });
  useEffect(() => {
    if (!summary.data) return;
    const signature = JSON.stringify(summary.data.items);
    if (priorSignature.current !== null && priorSignature.current !== signature) void queryClient.invalidateQueries({ queryKey: ['messages-inbox'] });
    priorSignature.current = signature;
  }, [summary.data, queryClient]);
  useEffect(() => () => {
    for (const queryKey of [key, ['messages-state'], ['messages-inbox'], ['creator-messages']]) {
      void queryClient.cancelQueries({ queryKey }); queryClient.removeQueries({ queryKey });
    }
  }, [queryClient, key]);
  const acknowledge = useCallback(async (creatorId: string, ids: string[]) => {
    await queryClient.cancelQueries({ queryKey: key });
    const result = await markCreatorMessagesRead(creatorId, ids);
    // Stop requests started during the write too; never publish their old snapshot.
    await queryClient.cancelQueries({ queryKey: key });
    await queryClient.cancelQueries({ queryKey: ['messages-inbox'] });
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: key }),
      queryClient.invalidateQueries({ queryKey: ['messages-inbox'] }),
      queryClient.invalidateQueries({ queryKey: ['creator-messages', creatorId] }),
    ]);
    return result;
  }, [queryClient, key]);
  const counts = useMemo(() => new Map(summary.data?.items.map(item => [item.creatorId, item.unreadCount]) ?? []), [summary.data]);
  const value = useMemo(() => ({ userId, visible, hasSummary: Boolean(summary.data), total: summary.data?.totalUnread ?? 0, counts, acknowledge }), [userId, visible, summary.data, counts, acknowledge]);
  return <UnreadContext.Provider value={value}>{children}</UnreadContext.Provider>;
}

export function useVisibleMessageReads(creatorId: string | undefined, root: RefObject<HTMLDivElement>, messages: ChatMessageDTO[], enabled: boolean) {
  const { visible, acknowledge } = useMessagesUnread();
  const queue = useRef<ReadQueue | null>(null);
  const active = useRef(false);
  active.current = Boolean(visible && enabled && creatorId);
  useEffect(() => {
    if (!creatorId) return;
    const next = createReadQueue({ acknowledge: ids => acknowledge(creatorId, ids), isActive: () => active.current });
    queue.current = next;
    return () => { next.dispose(); if (queue.current === next) queue.current = null; };
  }, [creatorId, acknowledge]);
  const ids = messages.filter(message => message.role === 'assistant' && message.incomingReadAt === null).map(message => message.id);
  const signature = JSON.stringify(ids);
  useEffect(() => {
    const current = queue.current;
    const element = root.current;
    if (!current || !element || !active.current) return;
    const eligible = new Set<string>(JSON.parse(signature));
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        const id = (entry.target as HTMLElement).dataset.incomingMessageId;
        if (id) current.view(id, entry.isIntersecting && entry.intersectionRatio > 0 && active.current);
      }
    }, { root: element, threshold: 0.01 });
    element.querySelectorAll<HTMLElement>('[data-incoming-message-id]').forEach(row => { if (eligible.has(row.dataset.incomingMessageId!)) observer.observe(row); });
    current.wake();
    return () => { observer.disconnect(); for (const id of eligible) current.view(id, false); };
  }, [creatorId, signature, visible, enabled, acknowledge, root]);
}
