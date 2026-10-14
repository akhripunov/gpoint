import "./boot-diagnostics";
import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  QueryClient,
  QueryClientProvider,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  auth,
  ageAssuranceState,
  creator,
  creatorMe,
  creatorOnboarding,
  creatorSubmit,
  engagement,
  explore,
  feedFollowing,
  feedForYou,
  follow,
  following,
  likePost,
  mediaUrl,
  posts,
  reels,
  unlikePost,
  vipClick,
  preferences,
  removePreference,
  resetPreferences,
  setPreference,
  startTelegramChannelConnection,
  selfAttest18Plus,
  telegramChannelConnectionStatus,
  openTelegramStartChannel,
} from "./api/client";
import {
  BadgeCheck,
  Bookmark,
  ChevronLeft,
  ChevronRight,
  Crown,
  Flame,
  House,
  MessageCircle,
  Pencil,
  Search,
  Share2,
  Settings,
  Settings2,
  Sparkles,
  Star,
  UserRound,
  Users,
  Video,
  WalletCards,
  type LucideIcon,
} from "lucide-react";
import type { PostDTO } from "../../../packages/contracts/src/index.js";
import {
  AdminPanel,
  CreatorWorkspaceDashboard,
} from "./platform-ui";
import { AvatarImage, BackButton, BottomNav } from "./ui";
import { MessagesInbox, ModelChat } from "./messages-ui";
import { MessagesUnreadProvider, useMessagesUnread } from "./messages-unread";
import { GPointAppHeader } from "./ui/brand";
import { userProfileCounts } from "./user-profile-data.js";
import { messagesPath, messagesRouteFromLocation } from "./messages-routing";
import {
  canonicalModelProfileUrl,
  modelHandleFromStartParam,
  modelProfilePath,
  modelRouteFromLocation,
  shouldRetryModelProfileQuery,
  telegramModelStartParam,
} from "./model-routing";
import "./style.css";
import "./ui/tokens.css";
import { BootErrorBoundary, markBootStage } from "./boot-diagnostics";
markBootStage("TELEGRAM_GLOBAL_READY", { telegramGlobalPresent: Boolean((window as any).Telegram?.WebApp) });
console.info("[GPoint] build", {
  APP_BUILD_SHA: __GPOINT_BUILD_SHA__,
  APP_BUILD_TIME: __GPOINT_BUILD_TIME__,
});
type Screen =
  | "for-you"
  | "explore"
  | "reels"
  | "vip"
  | "profile"
  | "account"
  | "admin"
  | "following"
  | "messages"
  | "chat";
const isReferenceVisualFixture = import.meta.env.DEV && new URLSearchParams(window.location.search).get("referenceFixture") === "1";
const V = ({ on }: { on?: boolean }) =>
  on ? <span className="verified">✓</span> : null;
function VipButton({
  offerId,
  placement,
}: {
  offerId: string;
  placement: string;
}) {
  const m = useMutation({ mutationFn: () => vipClick(offerId, placement) });
  return (
    <>
      <button
        disabled={m.isPending}
        onClick={() =>
          m.mutate(undefined, {
            onSuccess: (r) =>
              window.open(r.redirectUrl, "_blank", "noopener,noreferrer"),
          })
        }
      >
        {m.isPending ? "OPENING…" : "GET VIP ↗"}
      </button>
      {m.isError && (
        <small role="alert">VIP link unavailable. Try again.</small>
      )}
    </>
  );
}
function Card({
  c,
  open,
  surface,
}: {
  c: any;
  open: (c: any) => void;
  surface?: string;
}) {
  const ref = useRef<HTMLElement>(null);
  const [coverFailed, setCoverFailed] = useState(false);
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((x) => x.isIntersecting))
          engagement("CREATOR_IMPRESSION", "CREATOR", c.id, surface).catch(
            () => undefined,
          );
      },
      { threshold: 0.6 },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [c.id, surface]);
  return (
    <article
      ref={ref}
      className="creator-card"
      role="button"
      tabIndex={0}
      onClick={() => {
        engagement("CREATOR_PROFILE_VIEW", "CREATOR", c.id, surface).catch(
          () => undefined,
        );
        open(c);
      }}
      onKeyDown={(e) => {
        // Let nested controls, including the recommendation menu's native
        // <summary>, handle their own keyboard activation.
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open(c);
        }
      }}
    >
      <div
        className="cover"
        style={{ background: "linear-gradient(145deg, #322b35, #121821)" }}
      >
        {(c.coverUrl || c.avatarUrl) && !coverFailed ? <img src={mediaUrl(c.coverUrl || c.avatarUrl)} alt={`${c.displayName} creator profile`} loading="lazy" onError={() => setCoverFailed(true)} /> : <span className="cover-fallback" aria-hidden="true">{c.displayName.slice(0, 2).toUpperCase()}</span>}
      </div>
      <div className="card-copy">
        <AvatarImage src={c.avatarUrl} name={c.displayName} imageClassName="creator-avatar" fallbackClassName="creator-avatar creator-avatar-fallback" fallbackLength={2} />
        <div className="name-line">
          <strong>{c.displayName}</strong>
          <V on={c.verified} />
        </div>
        <span className="handle">@{c.slug}</span>
        {c.bio && <p>{c.bio}</p>}
      </div>
      <PreferenceMenu entityType="CREATOR" entityId={c.id} creatorId={c.id} />
    </article>
  );
}
function Following() {
  const q = useQuery({ queryKey: ["following"], queryFn: following });
  if (q.isLoading)
    return (
      <div className="empty">
        <h3>Loading followed creators</h3>
      </div>
    );
  if (q.isError)
    return (
      <div className="empty">
        <h3>Following is unavailable</h3>
      </div>
    );
  if (!q.data?.items?.length)
    return (
      <div className="empty">
        <h3>Your following feed is quiet</h3>
        <p>Follow a creator in Explore to see new posts and Reels here.</p>
      </div>
    );
  return (
    <div className="free-grid">
      {q.data.items.map((p: any) => (
        <article key={p.id} className="free-tile tile-1">
          <strong>{p.creator.displayName}</strong>
          <span>
            {p.isReel ? "REEL" : "POST"} · {p.caption ?? "New content"}
          </span>
        </article>
      ))}
    </div>
  );
}
function Explore({
  tab,
  setTab,
  open,
}: {
  tab: string;
  setTab: (x: string) => void;
  open: (c: any) => void;
}) {
  const [search, setSearch] = useState("");
  const [searchExpanded, setSearchExpanded] = useState(false);
  const isFollowing = tab === "Following";
  const q = useInfiniteQuery({
    queryKey: ["explore", tab],
    queryFn: ({ pageParam }) => explore(tab, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.nextCursor ?? undefined,
    enabled: !isFollowing,
  });
  const items = q.data?.pages.flatMap((x) => x.items) ?? [];
  const filteredItems = search.trim()
    ? items.filter((creator) => `${creator.displayName} ${creator.slug}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()))
    : items;
  return (
    <>
      <section className="explore-heading">
        <h1>Explore</h1>
      </section>
      <div className="explore-controls">
        {!isFollowing && <label className={`explore-search${searchExpanded ? " expanded" : ""}`}>
          <button type="button" aria-label="Search creators" aria-expanded={searchExpanded} onClick={() => setSearchExpanded((value) => !value)}><Search size={20} aria-hidden="true" /></button>
          <input tabIndex={searchExpanded ? 0 : -1} value={search} onChange={(event) => setSearch(event.target.value)} aria-label="Search loaded creators" placeholder="Search creators" />
        </label>}
        <div className="segmented explore-filters">
          {["Trending", "Popular", "New", "For You", "Following"].map((x) => (
            <button
              className={tab === x ? "selected" : ""}
              onClick={() => setTab(x)}
              key={x}
            >
              {x === "Trending" ? <Flame size={19} strokeWidth={1.8} aria-hidden="true" /> : x === "Popular" ? <Star size={19} strokeWidth={1.8} aria-hidden="true" /> : x === "New" ? <Sparkles size={19} strokeWidth={1.8} aria-hidden="true" /> : null}
              {x}
            </button>
          ))}
        </div>
      </div>
      {isFollowing ? (
        <Following />
      ) : (
        <>
          {q.isLoading && (
            <div className="empty">
              <span>◌</span>
              <h3>Finding your next world</h3>
              <p>Loading from GPoint.</p>
            </div>
          )}
          {q.isError && (
            <div className="empty">
              <span>!</span>
              <h3>Explore is unavailable</h3>
              <p>Try again in a moment.</p>
              <button className="primary" onClick={() => q.refetch()}>
                Retry
              </button>
            </div>
          )}
          {q.data && (
            <>
              <div className="creator-grid" aria-live="polite">
                {filteredItems.map((c) => (
                  <Card
                    c={c}
                    open={open}
                    surface={tab === "For You" ? "FOR_YOU" : undefined}
                    key={c.id}
                  />
                ))}
              </div>
              {search && !filteredItems.length && <div className="empty explore-no-results"><h3>No creators found</h3><p>Try a different name or handle.</p></div>}
              {q.hasNextPage && (
                <button
                  className="primary"
                  onClick={() => q.fetchNextPage()}
                  disabled={q.isFetchingNextPage}
                >
                  Load more
                </button>
              )}
            </>
          )}
        </>
      )}
    </>
  );
}
function Profile({ slug, back, explore, message }: { slug: string; back: () => void; explore: () => void; message: (slug: string) => void }) {
  const qc = useQueryClient();
  const [shareStatus, setShareStatus] = useState("");
  const q = useQuery({
    queryKey: ["creator", slug],
    queryFn: () => creator(slug),
    retry: shouldRetryModelProfileQuery,
  });
  const [tab, setTab] = useState("Posts");
  const f = useMutation({
    mutationFn: (v: boolean) => follow(q.data!.id, v),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["creator", slug] }),
  });
  const p = useQuery({
    queryKey: ["posts", q.data?.id],
    queryFn: () => posts(q.data!.id),
    enabled: !!q.data,
  });
  if (q.isLoading)
    return (
      <div className="empty">
        <h3>Loading model</h3>
      </div>
    );
  if (q.isError || !q.data)
    return (
      <div className="empty">
        <h3>Model unavailable</h3>
        <button className="back" onClick={back}>
          ← Back
        </button>
      </div>
    );
  const c = q.data;
  const profilePosts = p.data?.items ?? c.posts;
  const fixturePostCount = (c as typeof c & { postsCount?: number }).postsCount;
  const postCount = isReferenceVisualFixture
    ? fixturePostCount ?? profilePosts.length
    : profilePosts.length;
  const mediaPosts = profilePosts.flatMap((post) => post.media.map((media) => ({ post, media })));
  const reelPosts = mediaPosts.filter(({ media }) => media.kind === "VIDEO");
  const shareProfile = async () => {
    const url = canonicalModelProfileUrl(c.slug);
    const telegram = (window as Window & {
      Telegram?: { WebApp?: { openTelegramLink?: (target: string) => void } };
    }).Telegram?.WebApp;
    if (telegram?.openTelegramLink) {
      telegram.openTelegramLink(
        `https://t.me/share/url?url=${encodeURIComponent(url)}&text=${encodeURIComponent(c.displayName)}`,
      );
      return;
    }
    try {
      if (typeof navigator.share === "function") {
        await navigator.share({ title: c.displayName, url });
        return;
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
    }
    try {
      await navigator.clipboard.writeText(url);
      setShareStatus("Profile link copied");
      return;
    } catch {
      window.prompt("Copy this model profile link", url);
      setShareStatus("Copy the canonical model profile link");
    }
  };
  return (
    <section className="profile-view model-profile" aria-label={`${c.displayName} model profile`}>
      <GPointAppHeader centered className="model-profile-header" left={<button type="button" className="model-profile-icon" onClick={back} aria-label="Back to feed"><ChevronLeft size={25} strokeWidth={1.6} aria-hidden="true" /></button>} right={<button type="button" className="model-profile-icon model-profile-search" onClick={explore} aria-label="Explore models"><Search size={22} strokeWidth={1.6} aria-hidden="true" /></button>} />
      <div className="model-profile-main">
        <div className="model-profile-overview">
          <span className="model-profile-avatar">
            {c.avatarUrl ? <img src={mediaUrl(c.avatarUrl)} alt={`${c.displayName} avatar`} /> : c.displayName.slice(0, 2).toUpperCase()}
          </span>
          <div className="model-profile-details">
            <h1>{c.displayName}{c.verified && <BadgeCheck size={19} className="model-profile-verified" aria-label="Verified" />}</h1>
            <span className="model-profile-handle">@{c.slug}</span>
            {c.bio && <p className="model-profile-bio">{c.bio}</p>}
            <div className="model-profile-stats">
              <span><strong>{postCount}</strong><small>Posts</small></span>
              <span><strong>{Intl.NumberFormat("en", { notation: "compact" }).format(c.followersCount)}</strong><small>Followers</small></span>
            </div>
            <div className="model-profile-controls">
              <button type="button" className={`model-profile-follow${c.following ? " following" : ""}`} disabled={f.isPending} onClick={() => f.mutate(!c.following)}>
                {c.following ? "Following" : "Follow"}
              </button>
              <button type="button" className="model-profile-message" onClick={() => message(c.slug)} aria-label={`Message ${c.displayName}`}>
                <MessageCircle size={16} strokeWidth={1.8} aria-hidden="true" /> Message
              </button>
              <button type="button" className="model-profile-share" onClick={shareProfile} aria-label={`Share ${c.displayName}'s model profile`}>
                <Share2 size={20} strokeWidth={1.7} aria-hidden="true" />
              </button>
            </div>
            {shareStatus && <small className="model-profile-share-status" role="status">{shareStatus}</small>}
          </div>
        </div>
        {c.tags.length > 0 && <div className="model-profile-tags">{c.tags.map((tag) => <span key={tag}>#{tag}</span>)}</div>}
      {c.vipOffer && (
        <div className="vip-card">
          <div>
            <p className="eyebrow">VIP ACCESS</p>
            <h3>{c.vipOffer.title}</h3>
            <p>{c.vipOffer.description}</p>
          </div>
          <button
            onClick={async () => {
              const r = await vipClick(c.vipOffer!.id, "CREATOR_PROFILE");
              window.open(r.redirectUrl, "_blank", "noopener,noreferrer");
            }}
          >
            GET VIP ↗
          </button>
          <small>Payment happens in the creator's Telegram seller flow.</small>
        </div>
      )}
      <div className="content-tabs model-profile-tabs">
        {["Posts", "Media", "Reels"].map((x) => (
          <button
            className={tab === x ? "selected" : ""}
            onClick={() => setTab(x)}
            key={x}
          >
            {x}
          </button>
        ))}
      </div>
      {tab === "Posts" && (
        <div className="free-grid">
          {profilePosts.map((post) => <ProfilePostTile key={post.id} post={post} />)}
        </div>
      )}
      {tab === "Media" && (
        <div className="free-grid">
          {mediaPosts.map(({ media, post }) => <ProfileMediaTile key={media.id} media={media} name={c.displayName} caption={post.caption} />)}
        </div>
      )}
      {tab === "Reels" && (
        <div className="free-grid">
          {reelPosts.map(({ media, post }) => <ProfileMediaTile key={media.id} media={media} name={c.displayName} caption={post.caption} />)}
        </div>
      )}
      {(tab === "Posts" && !profilePosts.length || tab === "Media" && !mediaPosts.length || tab === "Reels" && !reelPosts.length) &&
        <p className="model-profile-empty">No {tab.toLowerCase()} yet.</p>}
      </div>
    </section>
  );
}
function ProfileMediaTile({ media, name, caption }: { media: PostDTO["media"][number]; name: string; caption: string | null }) {
  return <div className="model-profile-tile">
    {media.kind === "IMAGE"
      ? <img src={mediaUrl(media.readUrl)} alt={caption || `${name} post`} loading="lazy" />
      : <video src={mediaUrl(media.readUrl)} muted playsInline preload="metadata" aria-label={caption || `${name} reel`} />}
  </div>;
}
function ProfilePostTile({ post }: { post: PostDTO }) {
  const media = post.media[0];
  if (media) return <ProfileMediaTile media={media} name={post.creator?.displayName || "Model"} caption={post.caption} />;
  return <div className="model-profile-tile model-profile-text-tile"><p>{post.caption || "Post"}</p></div>;
}
function Reels({ open }: { open: (slug: string) => void }) {
  const q = useQuery({ queryKey: ["reels"], queryFn: () => reels() });
  return (
    <section className="reels">
      <div className="section-head">
        <div>
          <p className="eyebrow">DISCOVER IN MOTION</p>
          <h2>Reels</h2>
        </div>
        <span className="muted">Free previews</span>
      </div>
      {q.isLoading && (
        <div className="empty">
          <h3>Loading reels</h3>
        </div>
      )}
      {q.isError && (
        <div className="empty">
          <h3>Reels unavailable</h3>
        </div>
      )}
      {q.data?.items.map((r: any, i: number) => (
        <article className="reel-card" key={r.id}>
          <div className={"reel-visual visual-" + (i % 4)}>
            <span className="reel-play">▶</span>
          </div>
          <div className="reel-overlay">
            <button onClick={() => open(r.creator.slug)}>
              <span className="avatar tiny">
                {r.creator.displayName.slice(0, 2).toUpperCase()}
              </span>
              <strong>{r.creator.displayName}</strong>
            </button>
            <p>{r.caption}</p>
          </div>
        </article>
      ))}
    </section>
  );
}
function RealReels({ open }: { open: (slug: string) => void }) {
  const q = useInfiniteQuery({
    queryKey: ["reels"],
    queryFn: ({ pageParam }) => reels(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.nextCursor ?? undefined,
  });
  const items = q.data?.pages.flatMap((x) => x.items) ?? [];
  return (
    <section className="reels">
      <div className="section-head">
        <div>
          <p className="eyebrow">DISCOVER IN MOTION</p>
          <h2>Reels</h2>
        </div>
        <span className="muted">Free previews</span>
      </div>
      {q.isLoading && (
        <div className="empty">
          <h3>Loading reels</h3>
        </div>
      )}
      {q.isError && (
        <div className="empty">
          <h3>Reels unavailable</h3>
          <button className="primary" onClick={() => q.refetch()}>
            Retry
          </button>
        </div>
      )}
      {items.map((r: any, i: number) => (
        <ReelCard key={r.id} reel={r} index={i} open={open} />
      ))}
      {q.hasNextPage && (
        <button
          className="primary"
          disabled={q.isFetchingNextPage}
          onClick={() => q.fetchNextPage()}
        >
          {q.isFetchingNextPage ? "Loading…" : "Load more reels"}
        </button>
      )}
    </section>
  );
}
function ReelCard({
  reel: r,
  index: i,
  open,
}: {
  reel: any;
  index: number;
  open: (slug: string) => void;
}) {
  const ref = useRef<HTMLElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          engagement("REEL_IMPRESSION", "REEL", r.id, "REELS").catch(
            () => undefined,
          );
          video.current?.play().catch(() => undefined);
        } else video.current?.pause();
      },
      { threshold: 0.65 },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [r.id]);
  const media = r.media?.find((m: any) => m.kind === "VIDEO");
  const toggle = () => {
    if (!video.current) return;
    video.current.paused
      ? video.current.play().catch(() => undefined)
      : video.current.pause();
  };
  return (
    <article ref={ref} className="reel-card">
      <div className={"reel-visual visual-" + (i % 4)}>
        {media ? (
          <>
            <video
              ref={video}
              muted
              playsInline
              preload="metadata"
              src={mediaUrl(media.readUrl)}
              onClick={toggle}
              onPlay={() =>
                engagement("REEL_START", "REEL", r.id, "REELS").catch(
                  () => undefined,
                )
              }
              onEnded={() =>
                engagement("REEL_COMPLETE", "REEL", r.id, "REELS").catch(
                  () => undefined,
                )
              }
            />
            <button
              className="reel-play"
              aria-label="Play or pause reel"
              onClick={toggle}
            >
              ▶
            </button>
          </>
        ) : (
          <span className="reel-play">▶</span>
        )}
      </div>
      <div className="reel-overlay">
        <button onClick={() => open(r.creator.slug)}>
          <span className="avatar tiny">
            {r.creator.displayName.slice(0, 2).toUpperCase()}
          </span>
          <strong>{r.creator.displayName}</strong>
        </button>
        <p>{r.caption}</p>
        {r.creator.vipOffer && (
          <VipButton offerId={r.creator.vipOffer.id} placement="REEL" />
        )}
      </div>
      <PreferenceMenu
        entityType="REEL"
        entityId={r.id}
        creatorId={r.creator.id}
      />
    </article>
  );
}
function CreatorArea() {
  const q = useQuery({ queryKey: ["creator-me"], queryFn: creatorMe });
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [bio, setBio] = useState("");
  const m = useMutation({
    mutationFn: () =>
      creatorOnboarding({ displayName: name, slug, bio, tags: [] }),
    onSuccess: () => q.refetch(),
  });
  const s = useMutation({
    mutationFn: creatorSubmit,
    onSuccess: () => q.refetch(),
  });
  if (q.isLoading)
    return (
      <div className="empty">
        <h3>Loading creator workspace</h3>
      </div>
    );
  const c = q.data?.creator;
  if (!c)
    return (
      <section className="account">
        <p className="eyebrow">CREATOR PLATFORM</p>
        <h1>Become a Creator</h1>
        <p className="profile-bio">
          Build a public profile, publish free content and configure your
          Telegram VIP destination.
        </p>
        <input
          aria-label="Display name"
          placeholder="Display name"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <input
          aria-label="Slug"
          placeholder="slug"
          value={slug}
          onChange={(e) => setSlug(e.target.value)}
        />
        <textarea
          aria-label="Bio"
          placeholder="Bio"
          value={bio}
          onChange={(e) => setBio(e.target.value)}
        />
        <button
          className="primary"
          disabled={m.isPending || !name || !slug}
          onClick={() => m.mutate()}
        >
          Create draft
        </button>
        {m.isError && <p role="alert">Could not create draft.</p>}
      </section>
    );
  return (
    <section className="account">
      <p className="eyebrow">CREATOR PLATFORM</p>
      <h1>Creator Dashboard</h1>
      <div className="tag-row">
        <span className="tag">{c.status}</span>
        <span className="muted">@{c.slug}</span>
      </div>
      <h3>{c.displayName}</h3>
      <p className="profile-bio">{c.bio || "Add a bio in profile settings."}</p>
      {c.moderationReason && (
        <p role="alert">Moderation note: {c.moderationReason}</p>
      )}
      <div className="empty">
        <p>
          Content, media, VIP and analytics are available through the creator
          API.
        </p>
        <button
          className="primary"
          disabled={
            s.isPending || c.status === "PENDING" || c.status === "APPROVED"
          }
          onClick={() => s.mutate()}
        >
          {c.status === "PENDING" ? "Under review" : "Submit for moderation"}
        </button>
      </div>
    </section>
  );
}
function CreatorConnectionConfirmation({
  channel,
  onContinue,
}: {
  channel?: any;
  onContinue: () => void;
}) {
  return (
    <section className="account">
      <p className="eyebrow">CREATOR ONBOARDING</p>
      <h1>✓ Channel connected</h1>
      <h2>{channel?.title || "Telegram channel"}</h2>
      <p className="profile-bio">
        GPoint is connected and ready to import new posts automatically.
      </p>
      <button className="primary" onClick={onContinue}>
        Continue to Creator tools
      </button>
    </section>
  );
}
function CreatorChannelOnboarding() {
  const q = useQuery({
    queryKey: ["creator-me"],
    queryFn: creatorMe,
    refetchInterval: (query) => (query.state.data?.creator ? false : 2000),
  });
  const [state, setState] = useState<
    "IDLE" | "WAITING_FOR_CHANNEL" | "VERIFYING" | "CONNECTED" | "FAILED"
  >("IDLE");
  const [challengeId, setChallengeId] = useState("");
  const [channel, setChannel] = useState<any>(null);
  const [message, setMessage] = useState("");
  const [deadline, setDeadline] = useState(0);
  const start = () => {
    if (state === "VERIFYING") return;
    setMessage("");
    setState("VERIFYING");
    startTelegramChannelConnection()
      .then((result) => {
        setChallengeId(result.challengeId);
        if (result.status === "CONNECTED") {
          setChannel(result.channel);
          setState("CONNECTED");
          window.dispatchEvent(
            new CustomEvent("gpoint:creator-connected", {
              detail: result.channel,
            }),
          );
          q.refetch();
          return;
        }
        setDeadline(Date.now() + 15000);
        setState("WAITING_FOR_CHANNEL");
        try {
          openTelegramStartChannel();
        } catch (error: any) {
          setState("FAILED");
          setMessage(
            error?.status === 426
              ? "Update Telegram to connect a channel."
              : "Could not open Telegram channel connection.",
          );
        }
      })
      .catch(() => {
        setState("FAILED");
        setMessage("Couldn't start Telegram channel connection.");
      });
  };
  const statusQ = useQuery({
    queryKey: ["creator-channel-start-status", challengeId],
    queryFn: () => telegramChannelConnectionStatus(challengeId),
    enabled:
      !!challengeId &&
      (state === "WAITING_FOR_CHANNEL" || state === "VERIFYING"),
    refetchInterval: deadline > Date.now() ? 1500 : false,
    retry: false,
  });
  useEffect(() => {
    const resume = () => {
      if (challengeId) {
        setDeadline(Date.now() + 15000);
        setState("VERIFYING");
        statusQ.refetch();
      }
    };
    window.addEventListener("focus", resume);
    document.addEventListener("visibilitychange", resume);
    return () => {
      window.removeEventListener("focus", resume);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [challengeId]);
  useEffect(() => {
    if (!deadline) return;
    const timer = window.setTimeout(
      () => {
        if (state === "WAITING_FOR_CHANNEL" || state === "VERIFYING") {
          setState("FAILED");
          setMessage("Couldn't verify the connection yet.");
        }
      },
      Math.max(0, deadline - Date.now() + 100),
    );
    return () => window.clearTimeout(timer);
  }, [deadline, state]);
  useEffect(() => {
    const next = statusQ.data?.status;
    if (next === "CONNECTED") {
      setChannel(statusQ.data.channel);
      setState("CONNECTED");
      window.dispatchEvent(
        new CustomEvent("gpoint:creator-connected", {
          detail: statusQ.data.channel,
        }),
      );
      q.refetch();
    } else if (next === "FAILED") {
      setState("FAILED");
      setMessage(
        statusQ.data?.message || "Telegram channel connection failed.",
      );
    }
  }, [statusQ.data?.status]);
  if (state === "CONNECTED")
    return (
      <CreatorConnectionConfirmation
        channel={channel}
        onContinue={() =>
          window.dispatchEvent(
            new CustomEvent("gpoint:creator-tools-requested"),
          )
        }
      />
    );
  if (q.data?.creator)
    return <CreatorWorkspaceDashboard />;
  return (
    <section className="account">
      <p className="eyebrow">CREATOR ONBOARDING</p>
      <h1>Become a Creator</h1>
      <p className="profile-bio">
        Connect a Telegram channel you manage. Telegram will ask you to choose a
        channel and add GPoint as an administrator.
      </p>
      {state === "IDLE" ? (
        <button className="primary" onClick={start}>
          Connect Telegram channel
        </button>
      ) : state === "VERIFYING" ? (
        <div className="empty" role="status">
          <p>Verifying connection…</p>
        </div>
      ) : state === "WAITING_FOR_CHANNEL" ? (
        <div className="empty" role="status">
          <p>
            Choose a Telegram channel and approve GPoint administrator access.
          </p>
          <button className="primary" onClick={start}>
            Connect another channel
          </button>
        </div>
      ) : (
        <div className="empty" role="status">
          <p>{message || "Could not connect Telegram channel."}</p>
          <button className="primary" onClick={start}>
            Continue connection
          </button>
        </div>
      )}
    </section>
  );
}
function CreatorDashboard() {
  const q = useQuery({
    queryKey: ["creator-me"],
    queryFn: creatorMe,
    refetchInterval: (query) => (query.state.data?.creator ? false : 2000),
  });
  const [confirmation, setConfirmation] = useState<any>(null);
  const [tools, setTools] = useState(false);
  useEffect(() => {
    const connected = (event: Event) =>
      setConfirmation((event as CustomEvent).detail || {});
    const requested = () => setTools(true);
    window.addEventListener("gpoint:creator-connected", connected);
    window.addEventListener("gpoint:creator-tools-requested", requested);
    return () => {
      window.removeEventListener("gpoint:creator-connected", connected);
      window.removeEventListener("gpoint:creator-tools-requested", requested);
    };
  }, []);
  if (q.isLoading)
    return (
      <section className="account">
        <div className="empty">Preparing creator workspace…</div>
      </section>
    );
  if (confirmation && !tools)
    return (
      <CreatorConnectionConfirmation
        channel={confirmation}
        onContinue={() => setTools(true)}
      />
    );
  if (!q.data?.creator) return <CreatorChannelOnboarding />;
  return <CreatorWorkspaceDashboard />;
}
function Account({ user }: { user: any }) {
  return (
    <section className="account">
      <div className="account-head">
        <span className="avatar large">
          {(user?.firstName ?? "GP").slice(0, 2).toUpperCase()}
        </span>
        <div>
          <p className="eyebrow">YOUR PROFILE</p>
          <h1>{user?.firstName ?? "Guest"}</h1>
          <span className="handle">
            {user?.telegramUsername
              ? "@" + user.telegramUsername
              : "Telegram profile"}
          </span>
        </div>
      </div>
      <CreatorArea />
      <RecommendationSettings />
    </section>
  );
}
function ProfileSettingsRow({
  icon: Icon,
  title,
  subtitle,
  onClick,
}: {
  icon: LucideIcon;
  title: string;
  subtitle?: string;
  onClick?: () => void;
}) {
  return (
    <button
      className="profile-setting-row"
      onClick={onClick}
      disabled={!onClick}
      type="button"
    >
      <span className="setting-icon" aria-hidden="true">
        <Icon size={19} strokeWidth={1.8} />
      </span>
      <span>
        <strong>{title}</strong>
        {subtitle && <small>{subtitle}</small>}
      </span>
      <span className="setting-chevron" aria-hidden="true">
        <ChevronRight size={19} strokeWidth={1.8} />
      </span>
    </button>
  );
}
function ProfileStatCard({ label, value, icon: Icon }: { label: string; value: number | null; icon: LucideIcon }) {
  return (
    <div className="profile-stat-card" aria-label={`${label}: ${value === null ? "unavailable" : value}`}>
      <span className="profile-stat-icon" aria-hidden="true"><Icon size={18} strokeWidth={1.8} /></span>
      <strong>{value === null ? "—" : value}</strong>
      <span>{label}</span>
      {value === null && <small className="profile-stat-unavailable">Unavailable</small>}
    </div>
  );
}
function MyProfile({
  user,
  onBecomeCreator,
}: {
  user: any;
  onBecomeCreator: () => void;
}) {
  const name = user?.firstName || "GPoint member";
  const initials = name
    .split(/\s+/)
    .map((x: string) => x[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  const counts = userProfileCounts(user);
  return (
    <section className="my-profile">
      <div className="profile-identity-header">
        <span className="profile-avatar">{initials || "GP"}</span>
        <div className="profile-name-block">
          <p className="eyebrow">MY PROFILE</p>
          <h1>{name}</h1>
          <span className="handle">
            {user?.telegramUsername
              ? "@" + user.telegramUsername
              : "GPoint member"}
          </span>
        </div>
        <button className="profile-edit-button" type="button" disabled aria-label="Profile editing unavailable" title="Profile editing is not available yet">
          <Pencil size={17} strokeWidth={1.8} />
        </button>
      </div>
      <div className="profile-stats" aria-label="Profile information">
        <ProfileStatCard label="Saved" value={counts.saved} icon={Bookmark} />
        <ProfileStatCard label="Following" value={counts.following} icon={Users} />
      </div>
      <div className="profile-space-block">
        <p className="eyebrow">YOUR SPACE</p>
        <div className="profile-settings">
          <ProfileSettingsRow
            icon={Crown}
            title="My Subscription"
            subtitle="Subscriptions are not available yet"
          />
          <ProfileSettingsRow
            icon={WalletCards}
            title="Wallet"
            subtitle="Wallet is not available yet"
          />
          <ProfileSettingsRow
            icon={Settings2}
            title="Settings"
            subtitle="Account, privacy and more"
          />
        </div>
      </div>
      <div className="profile-recommendations">
        <p className="eyebrow">RECOMMENDATIONS</p>
        <button className="profile-action-card creator-action" onClick={onBecomeCreator} type="button">
          <span className="action-glyph" aria-hidden="true"><Sparkles size={22} strokeWidth={1.7} /></span>
          <span>
            <strong>Creator tools</strong>
            <small>Manage your creator space</small>
          </span>
          <ChevronRight size={21} strokeWidth={1.8} aria-hidden="true" />
        </button>
      </div>
    </section>
  );
}
function FeedCard({
  post,
  open,
  mode,
  prioritizeMedia,
}: {
  post: any;
  open: (c: any) => void;
  mode: "for-you" | "following";
  prioritizeMedia: boolean;
}) {
  const qc = useQueryClient();
  const [liked, setLiked] = useState(!!post.likedByMe);
  const [likes, setLikes] = useState(post.likesCount ?? 0);
  const like = useMutation<any, Error, void, { liked: boolean; likes: number }>(
    {
      mutationFn: () => (liked ? unlikePost(post.id) : likePost(post.id)),
      onMutate: async () => {
        const previous = { liked, likes };
        setLiked(!liked);
        setLikes(Math.max(0, likes + (liked ? -1 : 1)));
        return previous;
      },
      onError: (_e, _v, ctx) => {
        if (ctx) {
          setLiked(ctx.liked);
          setLikes(ctx.likes);
        }
      },
      onSuccess: (r: any) => {
        setLiked(r.likedByMe);
        setLikes(r.likesCount);
      },
    },
  );
  const f = useMutation({
    mutationFn: () => follow(post.creator.id, !post.following),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["feed"] }),
  });
  useEffect(() => {
    engagement(
      "POST_IMPRESSION",
      "POST",
      post.id,
      mode === "for-you" ? "FOR_YOU" : "FOLLOWING",
    ).catch(() => undefined);
  }, [post.id, mode]);
  return (
    <article
      className="post-card"
      tabIndex={0}
      aria-label={`Post by ${post.creator.displayName}. Open model profile.`}
      onClick={(event) => {
        if ((event.target as HTMLElement).closest("button, a, video, details")) return;
        open(post.creator);
      }}
      onKeyDown={(event) => {
        if (
          (event.key === "Enter" || event.key === " ") &&
          event.target === event.currentTarget
        ) {
          event.preventDefault();
          open(post.creator);
        }
      }}
    >
      <div className="post-header">
        <button className="post-creator" onClick={() => open(post.creator)}>
          <AvatarImage src={post.creator.avatarUrl} name={post.creator.displayName} imageClassName="avatar tiny" fallbackClassName="avatar tiny" fallbackLength={2} />
          <span className="post-identity">
            <strong>{post.creator.displayName}</strong>
            {post.creator.verified && <V on />}
          </span>
        </button>
        <button className="post-follow" onClick={() => f.mutate()}>
          {post.following ? "Following" : "Follow"}
        </button>
        <PreferenceMenu
          entityType="POST"
          entityId={post.id}
          creatorId={post.creator.id}
        />
      </div>
      {post.media?.map((m: any) => (
        <div className="post-media" key={m.id}>
          {m.kind === "VIDEO" ? (
            <video
              muted
              playsInline
              preload="metadata"
              src={mediaUrl(m.readUrl)}
            />
          ) : (
            <BlurHashImage
              hash={m.blurHash}
              alt={`${post.creator.displayName} post`}
              src={mediaUrl(m.readUrl)}
              width={m.width}
              height={m.height}
              prioritize={prioritizeMedia}
            />
          )}
        </div>
      ))}
      <div className="post-actions">
        <button
          aria-label={liked ? "Unlike" : "Like"}
          onClick={() => like.mutate()}
        >
          {liked ? "♥" : "♡"} <span>{likes}</span>
        </button>
        <button aria-label="Comment" disabled>
          ◌
        </button>
        <button aria-label="Share" disabled>
          ↗
        </button>
      </div>
      {post.caption && (
        <p className="post-caption">
          <strong>{post.creator.displayName}</strong> {post.caption}
        </p>
      )}
    </article>
  );
}
function FeedTabs({
  mode,
  go,
}: {
  mode: "for-you" | "following";
  go: (screen: Screen) => void;
}) {
  return (
    <div className="feed-tabs" role="tablist">
      <button
        role="tab"
        aria-selected={mode === "for-you"}
        className={mode === "for-you" ? "selected" : ""}
        onClick={() => go("for-you")}
      >
        For You
      </button>
      <button
        role="tab"
        aria-selected={mode === "following"}
        className={mode === "following" ? "selected" : ""}
        onClick={() => go("following")}
      >
        Following
      </button>
    </div>
  );
}
function FeedSkeleton() {
  return (
    <div aria-label="Loading feed">
      <div className="skeleton-card">
        <div className="skeleton skeleton-head" />
        <div className="skeleton skeleton-media" />
        <div className="skeleton skeleton-actions" />
      </div>
      <div className="skeleton-card">
        <div className="skeleton skeleton-head" />
        <div className="skeleton skeleton-media" />
      </div>
    </div>
  );
}
function Feed({
  mode,
  open,
  go,
}: {
  mode: "for-you" | "following";
  open: (c: any) => void;
  go: (screen: Screen) => void;
}) {
  const q = useInfiniteQuery({
    queryKey: ["feed", mode],
    queryFn: ({ pageParam }) =>
      mode === "for-you" ? feedForYou(pageParam) : feedFollowing(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.nextCursor ?? undefined,
  });
  const items = q.data?.pages.flatMap((x) => x.items) ?? [];
  if (q.isLoading)
    return (
      <>
        <FeedTabs mode={mode} go={go} />
        <FeedSkeleton />
      </>
    );
  if (q.isError)
    return (
      <>
        <FeedTabs mode={mode} go={go} />
        <div className="empty">
          <h3>Something went wrong</h3>
          <p>We couldn't load your feed.</p>
          <button className="primary" onClick={() => q.refetch()}>
            Try again
          </button>
        </div>
      </>
    );
  if (!items.length)
    return mode === "for-you" ? (
      <>
        <FeedTabs mode={mode} go={go} />
        <div className="empty">
          <h3>Your For You feed is quiet</h3>
          <p>New creator posts will appear here when available.</p>
          <button className="primary" onClick={() => go("explore")}>
            Explore creators
          </button>
        </div>
      </>
    ) : (
      <>
        <FeedTabs mode={mode} go={go} />
        <div className="empty">
          <h3>Your following feed is quiet</h3>
          <p>Follow a creator in Explore to see new posts here.</p>
          <button className="primary" onClick={() => go("explore")}>
            Explore creators
          </button>
        </div>
      </>
    );
  return (
    <section className="feed">
      <FeedTabs mode={mode} go={go} />
      {items.map((p: any, index: number) => (
        <FeedCard key={p.id} post={p} open={open} mode={mode} prioritizeMedia={index < 2} />
      ))}
      {mode === "for-you" && (
        <div className="feed-discovery">
          <Explore tab="For You" setTab={() => undefined} open={open} />
        </div>
      )}
    </section>
  );
}
function App({ initialUser }: { initialUser: any }) {
  const [launchStartParam] = useState(() => telegramModelStartParam());
  const [initialMessagesRoute] = useState(() => messagesRouteFromLocation(window.location.pathname));
  const [initialRoute] = useState(() =>
    modelRouteFromLocation(window.location.pathname, launchStartParam),
  );
  const creatorRoute = initialRoute.screen === "creator";
  const [screen, setScreen] = useState<Screen>(
    initialRoute.screen === "profile"
      ? "profile"
      : initialRoute.screen === "admin"
        ? "admin"
        : creatorRoute
          ? "account"
          : initialMessagesRoute?.screen ?? "for-you",
  );
  const [tab, setTab] = useState("Trending");
  const [slug, setSlug] = useState(initialRoute.screen === "profile" ? initialRoute.handle : "");
  const [chatSlug, setChatSlug] = useState(initialMessagesRoute?.screen === "chat" ? initialMessagesRoute.slug : "");
  const user = initialUser;
  const unread = useMessagesUnread();
  const [accountView, setAccountView] = useState<"profile" | "creator">(
    creatorRoute ? "creator" : "profile",
  );
  const initialStartHandle = modelHandleFromStartParam(launchStartParam);
  useEffect(() => {
    if (initialRoute.screen !== "profile" || !initialStartHandle) return;
    const path = modelProfilePath(initialStartHandle);
    if (window.location.pathname !== path) {
      window.history.replaceState(
        window.history.state,
        "",
        `${path}${window.location.search}${window.location.hash}`,
      );
    }
  }, []);
  useEffect(() => {
    const onPopState = () => {
      const messageRoute = messagesRouteFromLocation(window.location.pathname);
      const route = modelRouteFromLocation(window.location.pathname);
      if (messageRoute) {
        setScreen(messageRoute.screen as Screen);
        if (messageRoute.screen === "chat") setChatSlug(messageRoute.slug);
      } else if (route.screen === "profile") {
        setSlug(route.handle);
        setScreen("profile");
      } else if (route.screen === "admin") {
        setScreen("admin");
      } else if (route.screen === "creator") {
        setAccountView("creator");
        setScreen("account");
      } else {
        setScreen("for-you");
      }
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);
  const navigate = (next: Screen) => {
    const path = next === "admin" ? "/admin" : next === "account" ? "/creator" : next === "messages" ? "/messages" : "/";
    if (window.location.pathname !== path) window.history.pushState({ gpoint: true }, "", path);
    setScreen(next);
  };
  const openChat = (handle: string) => {
    setChatSlug(handle);
    const path = messagesPath(handle);
    if (window.location.pathname !== path) window.history.pushState({ gpoint: true, modelHandle: handle }, "", path);
    setScreen("chat");
  };
  const backToMessages = () => {
    if (window.location.pathname !== "/messages") window.history.pushState({ gpoint: true }, "", "/messages");
    setScreen("messages");
  };
  const backToFeed = () => {
    if (window.location.pathname.startsWith("/model/")) {
      window.history.replaceState(window.history.state, "", "/");
    }
    setScreen("for-you");
  };
  const open = (c: any) => {
    if (c.slug) {
      setSlug(c.slug);
      window.history.pushState({ gpoint: true, modelHandle: c.slug }, "", modelProfilePath(c.slug));
      setScreen("profile");
    } else navigate("explore");
  };
  const clickReel = (s: string) => {
    setSlug(s);
    window.history.pushState({ gpoint: true, modelHandle: s }, "", modelProfilePath(s));
    setScreen("profile");
  };
  const creatorWorkspace = screen === "account" && accountView === "creator";
  const messagesView = screen === "messages" || screen === "chat";
  const activeNavigation = screen === "profile" ? "account" : screen === "following" ? "for-you" : screen;
  return (
    <div className={`app-shell${screen === "account" && accountView === "profile" ? " profile-screen" : ""}${screen === "profile" ? " model-profile-screen" : ""}${screen === "messages" ? " messages-screen" : ""}${screen === "chat" ? " chat-screen" : ""}${screen === "for-you" ? " feed-screen" : ""}${screen === "explore" ? " explore-screen" : ""}`}>
      {screen !== "chat" && <GPointAppHeader
        className={`topbar${creatorWorkspace ? " creator-topbar" : ""}${messagesView ? " messages-root-header" : ""}`}
        onHome={() => navigate("for-you")}
        logo={undefined}
        subtitle={screen === "for-you" ? "Curated for you" : undefined}
        centered={false}
        left={undefined}
        right={messagesView ? undefined : creatorWorkspace ? <span className="creator-settings-control" aria-hidden="true"><Settings size={20} strokeWidth={1.7} /></span> : <button type="button" className="icon-button" aria-label="Explore creators" onClick={() => navigate("explore")}><Search size={18} strokeWidth={1.8} aria-hidden="true" /></button>}
      />}
      <main className={`page${messagesView ? " messages-page" : ""}`}>
        {screen === "for-you" && (
          <Feed mode="for-you" open={open} go={navigate} />
        )}{" "}
        {screen === "following" && (
          <Feed mode="following" open={open} go={navigate} />
        )}{" "}
        {screen === "explore" && (
          <Explore tab={tab} setTab={setTab} open={open} />
        )}{" "}
        {screen === "profile" && (
          <Profile slug={slug} back={backToFeed} explore={() => navigate("explore")} message={openChat} />
        )}{" "}
        {screen === "messages" && <MessagesInbox openChat={openChat} explore={() => navigate("explore")} />}{" "}
        {screen === "chat" && <ModelChat slug={chatSlug} back={backToMessages} />}{" "}
        {screen === "reels" && <RealReels open={clickReel} />}{" "}
        {screen === "account" &&
          (accountView === "creator" ? (
            <CreatorDashboard />
          ) : (
            <MyProfile
              user={user}
              onBecomeCreator={() => setAccountView("creator")}
            />
          ))}{" "}
        {screen === "admin" && <AdminPanel />}{" "}
        {screen === "vip" && (
          <div className="empty">
            <h3>Your VIP space is quiet</h3>
            <p>VIP access appears here after a verified connection.</p>
          </div>
        )}
      </main>
      {screen !== "chat" && <BottomNav active={activeNavigation} onChange={(id) => {
        if (creatorWorkspace && id === "account") { setAccountView("profile"); return; }
        navigate(id as Screen);
      }} items={[
              { id: "for-you", label: "For You", icon: <House size={23} strokeWidth={1.8} /> },
              { id: "reels", label: "Reels", icon: <Video size={23} strokeWidth={1.8} /> },
              { id: "messages", label: "Messages", badgeCount: unread.total, icon: <MessageCircle size={23} strokeWidth={1.8} /> },
              { id: "explore", label: "Explore", icon: <Search size={23} strokeWidth={1.8} /> },
              { id: "account", label: "Profile", icon: <UserRound size={23} strokeWidth={1.8} /> },
      ]} />}
    </div>
  );
}
function PreferenceMenu({
  entityType,
  entityId,
  creatorId,
}: {
  entityType: string;
  entityId: string;
  creatorId: string;
}) {
  const qc = useQueryClient();
  const [message, setMessage] = useState("");
  const [preferenceType, setPreferenceType] = useState("NOT_INTERESTED");
  const targetType = entityType === "CREATOR" ? "CREATOR" : entityType;
  const targetId = entityType === "CREATOR" ? creatorId : entityId;
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["explore"] });
    qc.invalidateQueries({ queryKey: ["reels"] });
    qc.invalidateQueries({ queryKey: ["preferences"] });
  };
  const set = useMutation({
    mutationFn: (type: string) => setPreference(type, targetType, targetId),
    onSuccess: (_, type) => {
      setPreferenceType(type);
      setMessage(
        type === "NOT_INTERESTED"
          ? "We'll show less like this"
          : "We won't recommend this creator",
      );
      qc.invalidateQueries({ queryKey: ["preferences"] });
    },
  });
  const undo = useMutation({
    mutationFn: () => removePreference(preferenceType, targetType, targetId),
    onSuccess: () => {
      setMessage("Restored");
      refresh();
    },
  });
  return (
    <div className="preference-menu" onClick={(e) => e.stopPropagation()}>
      <details>
        <summary aria-label="Recommendation actions">⋯</summary>
        <button onClick={(event) => { set.mutate("NOT_INTERESTED"); event.currentTarget.closest("details")?.removeAttribute("open"); }}>
          Not interested
        </button>
        <button onClick={(event) => { set.mutate("DO_NOT_RECOMMEND_CREATOR"); event.currentTarget.closest("details")?.removeAttribute("open"); }}>
          Don't recommend this creator
        </button>
      </details>
      {message && (
        <small role="status">
          {message} <button onClick={() => undo.mutate()}>Undo</button>
        </small>
      )}
    </div>
  );
}
function RecommendationSettings() {
  const q = useQuery({ queryKey: ["preferences"], queryFn: preferences });
  const reset = useMutation({
    mutationFn: resetPreferences,
    onSuccess: () => q.refetch(),
  });
  return (
    <section className="account">
      <p className="eyebrow">RECOMMENDATIONS</p>
      <h2>Recommendation controls</h2>
      <p className="profile-bio">
        Manage creators you do not want recommended. Following relationships
        stay unchanged.
      </p>
      <button
        className="primary"
        disabled={reset.isPending}
        onClick={() => {
          if (
            window.confirm(
              "Reset recommendation learning and preferences? Follows and analytics remain.",
            )
          )
            reset.mutate();
        }}
      >
        Reset recommendations
      </button>
      <div className="free-grid">
        {q.data?.items
          ?.filter((x: any) => x.preferenceType === "DO_NOT_RECOMMEND_CREATOR")
          .map((x: any) => (
            <button
              className="free-tile tile-2"
              key={x.entityId}
              onClick={() =>
                removePreference(
                  x.preferenceType,
                  x.entityType,
                  x.entityId,
                ).then(() => q.refetch())
              }
            >
              Restore creator
            </button>
          ))}
      </div>
    </section>
  );
}
function AuthenticatedApp() {
  const [user, setUser] = useState<any>(null);
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [ageState, setAgeState] = useState<"loading" | "required" | "ready" | "failed">("loading");
  const [ageSubmitting, setAgeSubmitting] = useState(false);
  useEffect(() => {
    const requireAgeAssurance = () => setAgeState("required");
    window.addEventListener("gpoint:age-assurance-required", requireAgeAssurance);
    return () => window.removeEventListener("gpoint:age-assurance-required", requireAgeAssurance);
  }, []);
  useEffect(() => {
    markBootStage("AUTH_REQUEST_STARTED");
    auth()
      .then((authenticatedUser) => {
        markBootStage("AUTH_SUCCESS", { creatorProfileStatus: "unknown", channelStatus: "unknown" });
        setUser(authenticatedUser);
        setState("ready");
        ageAssuranceState()
          .then((age) => setAgeState(age.required ? "required" : "ready"))
          .catch(() => setAgeState("failed"));
      })
      .catch(() => {
        localStorage.removeItem("gpoint.session");
        setState("failed");
      });
  }, []);
  if (state === "loading" || (state === "ready" && ageState === "loading"))
    return (
      <main className="gpoint-boot-splash" role="status" aria-live="polite">
        <span className="gpoint-boot-indicator" aria-hidden="true" />
        <span>Открываем GPoint…</span>
      </main>
    );
  if (state === "ready" && ageState === "required") {
    return <main className="age-assurance-gate" role="main"><p className="eyebrow">AGE ASSURANCE</p><h1>Продолжить в GPoint</h1><p>Подтвердите, что вам исполнилось 18 лет. Это самостоятельное подтверждение возраста, не проверка документов.</p><button type="button" disabled={ageSubmitting} onClick={() => { setAgeSubmitting(true); selfAttest18Plus().then((age) => setAgeState(age.required ? "required" : "ready")).catch(() => setAgeState("failed")).finally(() => setAgeSubmitting(false)); }}>{ageSubmitting ? "Проверяем…" : "Мне 18+"}</button></main>;
  }
  if (state === "ready" && ageState === "failed") {
    return <main className="age-assurance-gate" role="main"><p className="eyebrow">AGE ASSURANCE</p><h1>Не удалось проверить возраст</h1><p>Доступ закрыт, пока сервер не подтвердит состояние. Попробуйте снова.</p><button type="button" onClick={() => { setAgeState("loading"); ageAssuranceState().then((age) => setAgeState(age.required ? "required" : "ready")).catch(() => setAgeState("failed")); }}>Повторить</button></main>;
  }
  if (state === "failed")
    return (
      <div className="app-shell">
        <main className="page">
          <div className="empty">
            <h3>Telegram authorization unavailable</h3>
            <p>Close and reopen GPoint from Telegram.</p>
          </div>
        </main>
      </div>
    );
  if (ageState !== "ready") return null;
  markBootStage("MAIN_VIEW_RENDER_STARTED");
  const view = <MessagesUnreadProvider key={user?.id ?? ""} userId={user?.id ?? ""}><App initialUser={user} /></MessagesUnreadProvider>;
  markBootStage("MAIN_VIEW_RENDER_FINISHED");
  return view;
}
const root = createRoot(document.getElementById("root")!);
markBootStage("REACT_ROOT_CREATED");
root.render(<BootErrorBoundary><QueryClientProvider client={new QueryClient()}><AuthenticatedApp /></QueryClientProvider></BootErrorBoundary>);
markBootStage("REACT_RENDER_CALLED");
import { decode } from "blurhash";
function BlurHashImage({
  hash,
  src,
  alt,
  width,
  height,
  prioritize,
}: {
  hash?: string;
  src: string;
  alt: string;
  width?: number | null;
  height?: number | null;
  prioritize: boolean;
}) {
  const [placeholder, setPlaceholder] = useState<string>();
  useEffect(() => {
    if (!hash) return;
    try {
      const pixels = decode(hash, 32, 32);
      const canvas = document.createElement("canvas");
      canvas.width = 32;
      canvas.height = 32;
      canvas
        .getContext("2d")!
        .putImageData(
          new ImageData(new Uint8ClampedArray(pixels).slice(), 32, 32),
          0,
          0,
        );
      setPlaceholder(canvas.toDataURL());
    } catch {
      setPlaceholder(undefined);
    }
  }, [hash]);
  return (
    <img
      loading={prioritize ? "eager" : "lazy"}
      decoding="async"
      ref={(image) => {
        if (image) image.setAttribute("fetchpriority", prioritize ? "high" : "auto");
      }}
      width={width ?? undefined}
      height={height ?? undefined}
      alt={alt}
      src={src}
      style={
        placeholder
          ? { backgroundImage: `url(${placeholder})`, backgroundSize: "cover" }
          : undefined
      }
    />
  );
}
