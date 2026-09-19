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
  telegramChannelConnectionStatus,
  openTelegramStartChannel,
} from "./api/client";
import { ChevronLeft, Compass, Heart, House, Search, UserRound, Video } from "lucide-react";
import {
  AdminPanel,
  CreatorWorkspaceDashboard,
} from "./platform-ui";
import "./style.css";
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
  | "following";
const V = ({ on }: { on?: boolean }) =>
  on ? <span className="verified">✓</span> : null;
function Nav({
  active,
  target,
  icon,
  label,
  go,
}: {
  active: Screen;
  target: Screen;
  icon: React.ReactNode;
  label: string;
  go: (s: Screen) => void;
}) {
  return (
    <button
      aria-label={label === "For You" ? label : undefined}
      className={"nav-item " + (active === target ? "active" : "")}
      onClick={() => go(target)}
    >
      <b aria-hidden="true">{icon}</b>
      <span>{label}</span>
    </button>
  );
}
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
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open(c);
        }
      }}
    >
      <div
        className="cover"
        style={{
          background: `linear-gradient(130deg,hsl(${c.id.length * 17} 22% 25%),hsl(${c.id.length * 17} 45% 62%))`,
        }}
      >
        <span className="avatar small">
          {c.displayName.slice(0, 2).toUpperCase()}
        </span>
      </div>
      <div className="card-copy">
        <div className="name-line">
          <strong>{c.displayName}</strong>
          <V on={c.verified} />
        </div>
        <span className="handle">@{c.slug}</span>
        <p>{c.bio ?? "A GPoint creator."}</p>
        <div className="card-meta">
          <span>Free content</span>
          <span>VIP from {c.vipPriceStars ?? "—"} ⭐</span>
        </div>
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
  const isFollowing = tab === "Following";
  const q = useInfiniteQuery({
    queryKey: ["explore", tab],
    queryFn: ({ pageParam }) => explore(tab, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.nextCursor ?? undefined,
    enabled: !isFollowing,
  });
  const items = q.data?.pages.flatMap((x) => x.items) ?? [];
  return (
    <>
      <section className="hero">
        <p className="eyebrow">THE CREATOR EDIT</p>
        <h1>
          Find your
          <br />
          <i>favorite</i> world.
        </h1>
        <span className="hero-mark">✳</span>
      </section>
      <div className="segmented">
        {["For You", "Following", "Trending", "Popular", "New"].map((x) => (
          <button
            className={tab === x ? "selected" : ""}
            onClick={() => setTab(x)}
            key={x}
          >
            {x}
          </button>
        ))}
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
              <div className="section-head">
                <div>
                  <p className="eyebrow">
                    {tab === "For You"
                      ? "PERSONALIZED FOR YOU"
                      : "CURATED FOR YOU"}
                  </p>
                  <h2>{tab} creators</h2>
                </div>
                <span className="muted">{items.length} profiles</span>
              </div>
              <div className="creator-grid">
                {items.map((c) => (
                  <Card
                    c={c}
                    open={open}
                    surface={tab === "For You" ? "FOR_YOU" : undefined}
                    key={c.id}
                  />
                ))}
              </div>
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
function Profile({ slug, back }: { slug: string; back: () => void }) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["creator", slug],
    queryFn: () => creator(slug),
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
        <h3>Loading creator</h3>
      </div>
    );
  if (q.isError || !q.data)
    return (
      <div className="empty">
        <h3>Creator unavailable</h3>
        <button className="back" onClick={back}>
          ← Back
        </button>
      </div>
    );
  const c = q.data;
  return (
    <section className="profile-view">
      <button className="back" onClick={back}>
        ← Back
      </button>
      <div className="profile-cover" />
      <div className="profile-identity">
        <span className="avatar large">
          {c.displayName.slice(0, 2).toUpperCase()}
        </span>
        <div>
          <h1>
            {c.displayName} <V on={c.verified} />
          </h1>
          <span className="handle">@{c.slug}</span>
        </div>
        <button
          className={"follow " + (c.following ? "following" : "")}
          disabled={f.isPending}
          onClick={() => f.mutate(!c.following)}
        >
          {c.following ? "Following" : "Follow"}
        </button>
      </div>
      <p className="profile-bio">{c.bio}</p>
      <div className="tag-row">
        {c.tags.map((t) => (
          <span className="tag" key={t}>
            #{t}
          </span>
        ))}
        <span className="muted">{c.followersCount} followers</span>
      </div>
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
      <div className="content-tabs">
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
          {p.data?.items.map((x) => (
            <div className="free-tile tile-1" key={x.id}>
              <span>{x.type} · FREE</span>
            </div>
          ))}
        </div>
      )}
      {tab === "Media" && (
        <div className="free-grid">
          {p.data?.items
            .flatMap((x) => x.media)
            .map((x) => (
              <div className="free-tile tile-2" key={x.id}>
                <span>{x.kind}</span>
              </div>
            ))}
        </div>
      )}
      {tab === "Reels" && (
        <div className="empty">
          <p>Open Reels to browse published short media.</p>
        </div>
      )}
    </section>
  );
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
  icon,
  title,
  subtitle,
  onClick,
}: {
  icon: string;
  title: string;
  subtitle?: string;
  onClick?: () => void;
}) {
  return (
    <button
      className="profile-setting-row"
      onClick={onClick}
      disabled={!onClick}
    >
      <span className="setting-icon" aria-hidden="true">
        {icon}
      </span>
      <span>
        <strong>{title}</strong>
        {subtitle && <small>{subtitle}</small>}
      </span>
      <span className="setting-chevron" aria-hidden="true">
        ›
      </span>
    </button>
  );
}
function MyProfile({
  user,
  onBecomeCreator,
}: {
  user: any;
  onBecomeCreator: () => void;
}) {
  const creatorQ = useQuery({ queryKey: ["creator-me"], queryFn: creatorMe });
  const name = user?.firstName || "GPoint member";
  const initials = name
    .split(/\s+/)
    .map((x: string) => x[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  const creator = creatorQ.data?.creator;
  return (
    <section className="my-profile">
      <div className="my-profile-brand">
        GPoint<span>•</span>
      </div>
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
      </div>
      <div className="profile-stats" aria-label="Profile information">
        <div>
          <strong>GPoint</strong>
          <span>{creator ? "creator" : "member"}</span>
        </div>
        {user?.isPremium && (
          <div>
            <strong>Premium</strong>
            <span>Telegram status</span>
          </div>
        )}
      </div>
      <div className="profile-primary-actions">
        {creator ? (
          <button
            className="profile-action-card creator-action"
            onClick={onBecomeCreator}
          >
            <span className="action-glyph">✦</span>
            <span>
              <strong>Creator tools</strong>
              <small>Manage your creator space</small>
            </span>
            <b>›</b>
          </button>
        ) : (
          <button
            className="profile-action-card creator-action"
            onClick={onBecomeCreator}
          >
            <span className="action-glyph">✦</span>
            <span>
              <strong>Become a Creator</strong>
              <small>Connect a Telegram channel</small>
            </span>
            <b>›</b>
          </button>
        )}
      </div>
      <div className="profile-settings">
        <p className="eyebrow">YOUR SPACE</p>
        <ProfileSettingsRow
          icon="⌁"
          title="Settings"
          subtitle="Recommendation controls"
          onClick={() => undefined}
        />
        <RecommendationSettings />
      </div>
    </section>
  );
}
function FeedCard({
  post,
  open,
  mode,
}: {
  post: any;
  open: (c: any) => void;
  mode: "for-you" | "following";
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
  const stamp = post.publishedAt
    ? new Date(post.publishedAt).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
      })
    : "";
  return (
    <article className="post-card">
      <div className="post-header">
        <button className="post-creator" onClick={() => open(post.creator)}>
          <span className="avatar tiny">
            {post.creator.displayName.slice(0, 2).toUpperCase()}
          </span>
          <span className="post-identity">
            <strong>{post.creator.displayName}</strong>
            {post.creator.verified && <V on />}
            <small>{stamp}</small>
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
      {items.map((p: any) => (
        <FeedCard key={p.id} post={p} open={open} mode={mode} />
      ))}
      {mode === "for-you" && (
        <div className="feed-discovery">
          <Explore tab="For You" setTab={() => undefined} open={open} />
        </div>
      )}
    </section>
  );
}
function App() {
  const creatorRoute = window.location.pathname.startsWith("/creator");
  const [screen, setScreen] = useState<Screen>(
    window.location.pathname.startsWith("/admin")
      ? "admin"
      : creatorRoute
        ? "account"
        : "for-you",
  );
  const [tab, setTab] = useState("Trending");
  const [slug, setSlug] = useState("");
  const [user, setUser] = useState<any>(null);
  const [authReady, setAuthReady] = useState(false);
  const [accountView, setAccountView] = useState<"profile" | "creator">(
    creatorRoute ? "creator" : "profile",
  );
  useEffect(() => {
    auth()
      .then(setUser)
      .catch(() => undefined)
      .finally(() => setAuthReady(true));
  }, []);
  const open = (c: any) => {
    if (c.slug) {
      setSlug(c.slug);
      setScreen("profile");
    } else setScreen("explore");
  };
  const clickReel = (s: string) => {
    setSlug(s);
    setScreen("profile");
  };
  const creatorWorkspace = screen === "account" && accountView === "creator";
  if ((screen === "account" || screen === "admin") && !authReady)
    return (
      <div className="app-shell">
        <main className="page">
          <div className="empty">Loading…</div>
        </main>
      </div>
    );
  return (
    <div className="app-shell">
      <header className={`topbar${creatorWorkspace ? " creator-topbar" : ""}`}>
        {creatorWorkspace && (
          <button className="creator-back" aria-label="Back to Profile" onClick={() => setAccountView("profile")}>
            <ChevronLeft size={22} aria-hidden="true" />
          </button>
        )}
        <button className="brand" onClick={() => setScreen("for-you")}>
          GPoint<span>•</span>
        </button>
        {!creatorWorkspace && <button className="icon-button" aria-label="Search"><Search size={18} strokeWidth={1.8} aria-hidden="true" /></button>}
      </header>
      <main className="page">
        {screen === "for-you" && (
          <Feed mode="for-you" open={open} go={setScreen} />
        )}{" "}
        {screen === "following" && (
          <Feed mode="following" open={open} go={setScreen} />
        )}{" "}
        {screen === "explore" && (
          <Explore tab={tab} setTab={setTab} open={open} />
        )}{" "}
        {screen === "profile" && (
          <Profile slug={slug} back={() => setScreen("for-you")} />
        )}{" "}
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
      <nav className="bottom-nav">
        <Nav
          active={screen}
          target="for-you"
          icon={<House size={18} strokeWidth={1.8} />}
          label="For You"
          go={setScreen}
        />
        <Nav
          active={screen}
          target="reels"
          icon={<Video size={18} strokeWidth={1.8} />}
          label="Reels"
          go={setScreen}
        />
        <Nav
          active={screen}
          target="explore"
          icon={<Compass size={18} strokeWidth={1.8} />}
          label="Explore"
          go={setScreen}
        />
        <Nav
          active={screen}
          target="following"
          icon={<Heart size={18} strokeWidth={1.8} />}
          label="Following"
          go={setScreen}
        />
        <Nav
          active={screen}
          target="account"
          icon={<UserRound size={18} strokeWidth={1.8} />}
          label="Profile"
          go={setScreen}
        />
      </nav>
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
        <button onClick={() => set.mutate("NOT_INTERESTED")}>
          Not interested
        </button>
        <button onClick={() => set.mutate("DO_NOT_RECOMMEND_CREATOR")}>
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
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  useEffect(() => {
    auth()
      .then(() => setState("ready"))
      .catch(() => {
        localStorage.removeItem("gpoint.session");
        setState("failed");
      });
  }, []);
  if (state === "loading")
    return (
      <div className="app-shell">
        <main className="page">
          <div className="empty">
            <h3>Connecting to Telegram…</h3>
          </div>
        </main>
      </div>
    );
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
  return <App />;
}
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={new QueryClient()}>
    <AuthenticatedApp />
  </QueryClientProvider>,
);
import { decode } from "blurhash";
function BlurHashImage({
  hash,
  src,
  alt,
}: {
  hash?: string;
  src: string;
  alt: string;
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
      loading="lazy"
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
