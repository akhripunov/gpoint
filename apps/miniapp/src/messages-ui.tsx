import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowUp, MessageCircle, Plus, ShieldAlert } from "lucide-react";
import { ApiError, acceptMessagesConsent, creator, creatorMessages, mediaUrl, messages, messagesState, sendCreatorMessage } from "./api/client";
import type { ChatMessageDTO, MessageThreadDTO } from "../../../packages/contracts/src/index.js";
import { mergePendingMessages } from "./messages-pending.js";
import { Button, UnreadBadge } from "./ui";
import { useMessagesUnread, useVisibleMessageReads } from "./messages-unread";

function Avatar({ src, name, className = "" }: { src: string | null; name: string; className?: string }) {
  return <span className={`messages-avatar ${className}`}>{src ? <img src={mediaUrl(src)} alt="" /> : name.slice(0, 1).toUpperCase()}</span>;
}

function activityTime(value: string) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const now = Date.now();
  const elapsed = Math.max(0, now - date.getTime());
  if (elapsed < 60_000) return "now";
  if (elapsed < 3_600_000) return `${Math.floor(elapsed / 60_000)}m`;
  if (elapsed < 86_400_000) return `${Math.floor(elapsed / 3_600_000)}h`;
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(date);
}

function preview(item: MessageThreadDTO) {
  if (!item.lastMessagePreview) return "Start a conversation";
  return item.lastMessageRole === "user" ? `You: ${item.lastMessagePreview}` : item.lastMessagePreview;
}

export function MessagesInbox({ openChat, explore }: { openChat: (slug: string) => void; explore: () => void }) {
  const unread = useMessagesUnread();
  const state = useQuery({ queryKey: ["messages-state"], queryFn: messagesState });
  const inbox = useInfiniteQuery({
    queryKey: ["messages-inbox"],
    queryFn: ({ pageParam }) => messages(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.nextCursor ?? undefined,
    enabled: state.data?.enabled === true,
  });
  const unreadCount = (item: MessageThreadDTO) => unread.hasSummary ? unread.counts.get(item.creator.id) ?? 0 : item.unreadCount ?? 0;
  const items = useMemo(() => (inbox.data?.pages.flatMap((page) => page.items) ?? []).sort((a, b) => Date.parse(b.lastMessageAt) - Date.parse(a.lastMessageAt)), [inbox.data]);

  if (state.isLoading) return <div className="messages-state"><span className="messages-spinner" /><p>Loading messages…</p></div>;
  if (state.isError || !state.data?.enabled) return <div className="messages-state"><MessageCircle size={32} /><h2>Messages unavailable</h2><p>Conversations are temporarily unavailable. Please try again later.</p></div>;
  if (inbox.isError) return <div className="messages-state"><MessageCircle size={32} /><h2>Couldn't load messages</h2><p>Your conversations are safe. Try again.</p><Button variant="secondary" onClick={() => void inbox.refetch()}>Retry</Button></div>;

  return <section className="messages-inbox" aria-label="Messages">
    <header className="messages-page-heading"><div><p className="eyebrow">YOUR CONVERSATIONS</p><h1>Messages</h1></div><button type="button" className="messages-compose" aria-label="Explore models" onClick={explore}><Plus size={20} /></button></header>
    {inbox.isLoading ? <div className="messages-state"><span className="messages-spinner" /><p>Loading conversations…</p></div> : items.length ? <div className="messages-thread-list">
      {items.map((item) => <button type="button" className={`messages-thread${unreadCount(item) > 0 ? " unread" : ""}`} aria-label={`${item.creator.displayName}${unreadCount(item) > 0 ? `, ${unreadCount(item)} unread messages` : ""}`} key={item.creator.id} onClick={() => openChat(item.creator.slug)}>
        <Avatar src={item.creator.avatarUrl} name={item.creator.displayName} />
        <span className="messages-thread-copy"><strong>{item.creator.displayName}{item.creator.verified && <span className="messages-verified" aria-label="Verified">✓</span>}</strong><span>{preview(item)}</span></span>
        <span className="messages-thread-meta"><time dateTime={item.lastMessageAt}>{activityTime(item.lastMessageAt)}</time><UnreadBadge count={unreadCount(item)} /></span>
      </button>)}
      {inbox.hasNextPage && <button type="button" className="messages-load-more" disabled={inbox.isFetchingNextPage} onClick={() => void inbox.fetchNextPage()}>{inbox.isFetchingNextPage ? "Loading…" : "Load more"}</button>}
    </div> : <div className="messages-empty"><span className="messages-empty-icon"><MessageCircle size={26} /></span><h2>No messages yet</h2><p>Start a conversation from a model profile.</p><Button onClick={explore}>Explore models</Button></div>}
  </section>;
}

type PendingMessage = { id: string; text: string; status: "sending" | "waiting" | "failed"; createdAt: number };
type DisplayMessage = ChatMessageDTO & { status?: PendingMessage["status"] | "accepted" };

export function ModelChat({ slug, back }: { slug: string; back: () => void }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");
  const [remember, setRemember] = useState<boolean | null>(null);
  const [pending, setPending] = useState<PendingMessage[]>([]);
  const [ageRequired, setAgeRequired] = useState(false);
  const [providerUnavailable, setProviderUnavailable] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const historyRoot = useRef<HTMLDivElement>(null);
  const { visible } = useMessagesUnread();
  const fieldRef = useRef<HTMLTextAreaElement>(null);
  const model = useQuery({ queryKey: ["creator", slug], queryFn: () => creator(slug), retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 2 });
  const state = useQuery({ queryKey: ["messages-state"], queryFn: messagesState });
  const creatorId = model.data?.id;
  const history = useInfiniteQuery({
    queryKey: ["creator-messages", creatorId],
    queryFn: ({ pageParam }) => creatorMessages(creatorId!, pageParam),
    refetchInterval: 3000,
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.nextCursor ?? undefined,
    enabled: Boolean(visible && creatorId && state.data?.enabled && state.data.chatHistory && !state.data.disclosureRequired),
  });
  const consent = useMutation({
    mutationFn: acceptMessagesConsent,
    onSuccess: (next) => queryClient.setQueryData(["messages-state"], next),
  });
  const send = useMutation({
    mutationFn: ({ id, text }: { id: string; text: string }) => sendCreatorMessage(creatorId!, { clientMessageId: id, text }),
    onSuccess: (result, variables) => {
      const key = ["creator-messages", creatorId];
      if (result.status === "pending" || !result.response) {
        setPending((all) => all.map((item) => item.id === variables.id ? { ...item, status: "waiting" } : item));
        void queryClient.invalidateQueries({ queryKey: key });
        void queryClient.invalidateQueries({ queryKey: ["messages-inbox"] });
        return;
      }
      const response = result.response;
      if (result.status !== "completed" || !response) return;
      queryClient.setQueryData<any>(key, (current: any) => {
        if (!current?.pages?.length) return current;
        const pages = [...current.pages];
        const first = pages[0];
        const items = [...first.items];
        if (!items.some((item: ChatMessageDTO) => item.clientMessageId === variables.id || item.id === variables.id)) items.push({ id: variables.id, clientMessageId: variables.id, role: "user", text: variables.text, occurredAt: new Date().toISOString() });
        if (!items.some((item: ChatMessageDTO) => item.id === response.id)) items.push(response);
        pages[0] = { ...first, items };
        return { ...current, pages };
      });
      setPending((all) => all.filter((item) => item.id !== variables.id));
      void queryClient.invalidateQueries({ queryKey: key });
      void queryClient.invalidateQueries({ queryKey: ["messages-inbox"] });
    },
    onError: (error, variables) => {
      if (error instanceof ApiError && error.code === "AGE_ASSURANCE_REQUIRED") {
        window.dispatchEvent(new Event("gpoint:age-assurance-required"));
        void queryClient.invalidateQueries({ queryKey: ["messages-state"] });
      }
      if (error instanceof ApiError && error.code === "AI_UNAVAILABLE") {
        setProviderUnavailable(true);
        setPending((all) => all.map((item) => item.id === variables.id ? { ...item, status: "waiting" } : item));
        void queryClient.invalidateQueries({ queryKey: ["creator-messages", creatorId] });
        void queryClient.invalidateQueries({ queryKey: ["messages-inbox"] });
        return;
      }
      setPending((all) => all.map((item) => item.id === variables.id ? { ...item, status: "failed" } : item));
      void queryClient.invalidateQueries({ queryKey: ["creator-messages", creatorId] });
    },
  });
  const pages = history.data?.pages ?? [];
  const historyItems = pages.flatMap((page) => page.items).sort((a, b) => Date.parse(a.occurredAt) - Date.parse(b.occurredAt));
  const displayItems = mergePendingMessages(historyItems, pending) as DisplayMessage[];
  const ageBlocked = ageRequired || state.data?.ageAssuranceRequired === true;
  const aiUnavailable = providerUnavailable || state.data?.aiAvailable === false;
  useEffect(() => {
    if (state.data?.ageAssuranceRequired) window.dispatchEvent(new Event("gpoint:age-assurance-required"));
  }, [state.data?.ageAssuranceRequired]);
  const needsConsent = !!state.data && (!state.data.chatHistory || !state.data.onboardingCompleted || state.data.disclosureRequired);
  useVisibleMessageReads(creatorId, historyRoot, displayItems, Boolean(state.data?.enabled && !needsConsent && !ageBlocked && !history.isLoading && !history.isError));
  const canSend = Boolean(creatorId && state.data?.enabled && !needsConsent && !ageBlocked && !aiUnavailable && !send.isPending);

  useEffect(() => { bottomRef.current?.scrollIntoView({ block: "end", behavior: "smooth" }); }, [displayItems.length]);
  useEffect(() => {
    const persistedClientIds = new Set(historyItems.flatMap((item) => item.clientMessageId ? [item.clientMessageId] : []));
    if (!persistedClientIds.size) return;
    setPending((all) => all.filter((item) => !persistedClientIds.has(item.id)));
  }, [history.data, creatorId]);
  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    const text = draft.trim();
    if (!text || text.length > 4000 || !canSend) return;
    const id = crypto.randomUUID();
    setPending((all) => [...all, { id, text, status: "sending", createdAt: Date.now() }]);
    setProviderUnavailable(false);
    setDraft("");
    send.mutate({ id, text });
  };
  const retry = (item: PendingMessage) => {
    if (!ageBlocked && !send.isPending) {
      setPending((all) => all.map((candidate) => candidate.id === item.id ? { ...candidate, status: "sending" } : candidate));
      send.mutate({ id: item.id, text: item.text });
    }
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  return <section className="messages-chat" aria-label="Chat">
    <header className="chat-header"><button type="button" onClick={back} aria-label="Back to messages"><ArrowLeft size={22} /></button>{model.data && <><Avatar src={model.data.avatarUrl} name={model.data.displayName} /><div className="chat-header-name"><strong>{model.data.displayName}</strong><span>@{model.data.slug}</span></div></>}</header>
    {model.isLoading || state.isLoading ? <div className="messages-state"><span className="messages-spinner" /><p>Opening conversation…</p></div> : model.isError || !model.data ? <div className="messages-state"><h2>Model unavailable</h2><p>This model profile can't be opened right now.</p><Button variant="secondary" onClick={back}>Back to messages</Button></div> : state.isError || !state.data?.enabled ? <div className="messages-state"><h2>Messages unavailable</h2><p>Conversations are temporarily unavailable.</p></div> : ageBlocked ? <div className="messages-gate" role="alert"><ShieldAlert size={25} /><h2>Messages are unavailable</h2><p>Age assurance is required before you can send messages. Messages cannot collect or change your age status.</p></div> : needsConsent ? <div className="messages-gate"><span className="messages-empty-icon"><MessageCircle size={25} /></span><p className="eyebrow">BEFORE YOUR FIRST MESSAGE</p><h2>AI character conversation</h2><p>You’re chatting with an AI character for this creator. Your conversation history is saved so you can continue this chat later.</p><label className="messages-memory-choice"><input type="checkbox" checked={remember ?? state.data.longTermMemory} onChange={(event) => setRemember(event.target.checked)} /><span><strong>Remember things from our conversations</strong><small>Optional. You can continue without long-term memory.</small></span></label><Button loading={consent.isPending} disabled={consent.isPending} onClick={() => consent.mutate(remember ?? state.data!.longTermMemory)}>Continue</Button>{consent.isError && <p className="messages-error" role="alert">Consent couldn't be saved. Try again.</p>}</div> : <>
        <div className="chat-history" ref={historyRoot} aria-live="polite">
        {aiUnavailable && <div className="chat-provider-unavailable" role="status">AGATA is temporarily unavailable. Please try again later.</div>}
      {history.isError && <div className="chat-load-error" role="alert">Couldn't load chat history. <button type="button" onClick={() => void history.refetch()}>Retry</button></div>}
        {history.hasNextPage && <button className="chat-older" disabled={history.isFetchingNextPage} type="button" onClick={() => void history.fetchNextPage()}>{history.isFetchingNextPage ? "Loading…" : "Load earlier messages"}</button>}
        {history.isLoading && <div className="messages-state"><span className="messages-spinner" /><p>Loading conversation…</p></div>}
        {!history.isLoading && displayItems.map((item) => <div className={`chat-message-row ${item.role === "user" ? "mine" : "theirs"}`} data-incoming-message-id={item.role === "assistant" ? item.id : undefined} key={item.id}><article className={`chat-bubble ${item.role === "user" ? "mine" : "theirs"}`}><p>{item.text}</p>{item.status && <small className={item.status}>{item.status === "sending" ? "Sending…" : item.status === "waiting" ? "Агата отвечает…" : item.status === "accepted" ? "Sent" : <>Couldn't confirm · <button type="button" onClick={() => retry({ id: item.clientMessageId ?? item.id, text: item.text, status: "failed", createdAt: Date.now() })}>Retry</button></>}</small>}<time>{activityTime(item.occurredAt)}</time></article></div>)}
        {!history.isLoading && !displayItems.length && <div className="chat-welcome"><Avatar src={model.data.avatarUrl} name={model.data.displayName} className="large" /><h2>{model.data.displayName}</h2><p>Start a conversation. You’re chatting with an AI character for this creator.</p></div>}
        <div ref={bottomRef} />
      </div>
      {displayItems.some((item) => item.status === "failed") && <p className="chat-send-error" role="alert">Delivery couldn't be confirmed. Retrying uses the same message ID.</p>}
      <form className="chat-composer" onSubmit={submit}><textarea ref={fieldRef} value={draft} onChange={(event) => setDraft(event.target.value.slice(0, 4000))} onKeyDown={onKeyDown} placeholder="Message…" aria-label="Message" rows={1} maxLength={4000} disabled={!canSend} /><span className="chat-composer-meta">{draft.length > 3600 ? `${draft.length}/4000` : ""}</span><button type="submit" disabled={!draft.trim() || !canSend} aria-label="Send message"><ArrowUp size={19} /></button></form>
    </>}
  </section>;
}
