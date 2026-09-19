import { useMutation, useQuery } from "@tanstack/react-query";
import { ChangeEvent, useEffect, useState } from "react";
import {
  adminCreators,
  adminModerateCreator,
  adminModeratePost,
  adminModerateVip,
  adminPosts,
  adminVip,
  apiBase,
  creatorAnalytics,
  creatorMedia,
  creatorMediaFinalize,
  creatorMediaIntent,
  creatorMe,
  creatorOnboarding,
  creatorPostCreate,
  creatorPostUpdate,
  creatorProfileUpdate,
  creatorReelCreate,
  creatorReels,
  creatorSubmit,
  creatorVip,
  creatorVipUpdate,
  disconnectTelegramChannel,
  getToken,
  mediaUrl,
  startTelegramChannelConnection,
  telegramChannel,
  updateTelegramChannel,
} from "./api/client";
import {
  BarChart3,
  Check,
  CheckCircle2,
  ChevronRight,
  FileText,
  Import,
  Save,
  Settings2,
  ShieldCheck,
  Unplug,
} from "lucide-react";

function Preview({ asset, admin = false }: { asset: any; admin?: boolean }) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    let active = true;
    const headers: any = {};
    const token = getToken();
    if (token) headers.authorization = `Bearer ${token}`;
    fetch(mediaUrl(`/media/assets/${asset?.id}`), { headers })
      .then((r) => (r.ok ? r.blob() : Promise.reject()))
      .then((b) => {
        if (active) setUrl(URL.createObjectURL(b));
      })
      .catch(() => {});
    return () => {
      active = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [asset?.id]);
  if (!asset?.id || !url) return <span>{admin ? "Loading media…" : ""}</span>;
  return asset.kind === "VIDEO" ? (
    <video
      controls
      preload="metadata"
      src={url}
      data-media-id={asset.id}
      style={{ maxWidth: "100%" }}
    />
  ) : (
    <img
      alt="uploaded media"
      src={url}
      data-media-id={asset.id}
      style={{ maxWidth: "100%" }}
    />
  );
}
function Library({ onSelect }: { onSelect: (a: any) => void }) {
  const q = useQuery({ queryKey: ["creator-media"], queryFn: creatorMedia });
  return (
    <div className="free-grid">
      {q.data?.items
        .filter((x: any) => x.status === "READY")
        .map((x: any) => (
          <button
            className="free-tile tile-2"
            key={x.id}
            onClick={() => onSelect(x)}
          >
            {x.kind} · {Math.round(x.fileSize / 1024)}KB
          </button>
        ))}
    </div>
  );
}
export function CreatorDashboard() {
  const q = useQuery({ queryKey: ["creator-me"], queryFn: creatorMe });
  const [tab, setTab] = useState("Overview");
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [bio, setBio] = useState("");
  const [caption, setCaption] = useState("");
  const [assets, setAssets] = useState<any[]>([]);
  const [cover, setCover] = useState<any>();
  const [reel, setReel] = useState<any>();
  const [reelCaption, setReelCaption] = useState("");
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const posts = useQuery({
    queryKey: ["creator-posts"],
    queryFn: async () => {
      const r = await fetch(`${apiBase}/creator/me/posts`, {
        headers: { authorization: `Bearer ${getToken()}` },
      });
      return r.json();
    },
    enabled: !!q.data?.creator,
  });
  const rs = useQuery({
    queryKey: ["creator-reels"],
    queryFn: creatorReels,
    enabled: tab === "Reels",
  });
  const save = useMutation({
    mutationFn: () =>
      creatorOnboarding({ displayName: name, slug, bio, tags: [] }),
    onSuccess: () => q.refetch(),
  });
  const post = useMutation({
    mutationFn: () =>
      creatorPostCreate({
        type: "IMAGE",
        caption,
        mediaIds: assets.map((x) => x.id),
      }),
    onSuccess: () => {
      setCaption("");
      posts.refetch();
    },
  });
  const reelSave = useMutation({
    mutationFn: () =>
      creatorReelCreate({
        caption: reelCaption,
        mediaIds: reel ? [reel.id] : [],
      }),
    onSuccess: () => {
      setReel(undefined);
      rs.refetch();
    },
  });
  const submit = useMutation({
    mutationFn: creatorSubmit,
    onSuccess: () => q.refetch(),
  });
  const v = useQuery({
    queryKey: ["creator-vip"],
    queryFn: creatorVip,
    enabled: !!q.data?.creator,
  });
  const a = useQuery({
    queryKey: ["creator-analytics"],
    queryFn: creatorAnalytics,
    enabled: tab === "Analytics",
  });
  const vip = useMutation({
    mutationFn: () => creatorVipUpdate({ title, affiliateUrl: url }),
    onSuccess: () => v.refetch(),
  });
  if (q.isLoading) return <div className="empty">Loading…</div>;
  const c = q.data?.creator;
  if (!c)
    return (
      <section className="account">
        <h1>Become a Creator</h1>
        <input
          aria-label="Display name"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <input
          aria-label="Slug"
          value={slug}
          onChange={(e) => setSlug(e.target.value)}
        />
        <textarea
          aria-label="Bio"
          value={bio}
          onChange={(e) => setBio(e.target.value)}
        />
        <button
          className="primary"
          disabled={!name || !slug}
          onClick={() => save.mutate()}
        >
          Save Draft
        </button>
      </section>
    );
  const resubmitPost = (p: any) =>
    creatorPostUpdate(p.id, {
      caption: p.caption,
      status: "PENDING",
      mediaIds: p.media?.map((m: any) => m.mediaAssetId),
    }).then(() => posts.refetch());
  return (
    <section className="account">
      <p className="eyebrow">CREATOR PLATFORM</p>
      <h1>Creator Dashboard</h1>
      <div className="content-tabs">
        {["Overview", "Profile", "Content", "Reels", "VIP", "Analytics"].map(
          (x) => (
            <button
              className={tab === x ? "selected" : ""}
              onClick={() => setTab(x)}
              key={x}
            >
              {x}
            </button>
          ),
        )}
      </div>
      <span className="tag">{c.status}</span>
      {c.moderationReason && <p role="alert">{c.moderationReason}</p>}
      {tab === "Overview" && (
        <div className="empty">
          <h3>{c.status === "PENDING" ? "Under review" : "Workspace"}</h3>
          <button
            className="primary"
            disabled={c.status === "PENDING" || c.status === "APPROVED"}
            onClick={() => submit.mutate()}
          >
            {c.status === "REJECTED" ? "Resubmit" : "Submit for Moderation"}
          </button>
        </div>
      )}
      {tab === "Profile" && (
        <div className="empty">
          <input
            aria-label="Display name"
            value={name || c.displayName}
            onChange={(e) => setName(e.target.value)}
          />
          <input
            aria-label="Slug"
            value={slug || c.slug}
            onChange={(e) => setSlug(e.target.value)}
          />
          <textarea
            aria-label="Bio"
            value={bio || c.bio}
            onChange={(e) => setBio(e.target.value)}
          />
          <p>Avatar</p>
          <MediaUpload
            kind="IMAGE"
            onReady={(x) =>
              creatorProfileUpdate({ avatarMediaId: x.id }).then(() =>
                q.refetch(),
              )
            }
          />
          <Library
            onSelect={(x) =>
              creatorProfileUpdate({ avatarMediaId: x.id }).then(() =>
                q.refetch(),
              )
            }
          />
          <p>Cover</p>
          <MediaUpload
            kind="IMAGE"
            onReady={(x) => {
              setCover(x);
              creatorProfileUpdate({ coverMediaId: x.id }).then(() =>
                q.refetch(),
              );
            }}
          />
          <Library
            onSelect={(x) => {
              setCover(x);
              creatorProfileUpdate({ coverMediaId: x.id }).then(() =>
                q.refetch(),
              );
            }}
          />
          <Preview asset={cover} />
          <button
            className="primary"
            onClick={() =>
              creatorProfileUpdate({
                displayName: name || c.displayName,
                slug: slug || c.slug,
                bio: bio || c.bio,
              }).then(() => q.refetch())
            }
          >
            Save Draft
          </button>
        </div>
      )}
      {tab === "Content" && (
        <div className="empty">
          <MediaUpload
            kind="IMAGE"
            onReady={(x) => setAssets((y) => [...y, x])}
          />
          <MediaUpload
            kind="VIDEO"
            onReady={(x) => setAssets((y) => [...y, x])}
          />
          <Library onSelect={(x) => setAssets((y) => [...y, x])} />
          {assets.map((x) => (
            <Preview key={x.id} asset={x} />
          ))}
          <textarea
            aria-label="Post caption"
            value={caption}
            onChange={(e) => setCaption(e.target.value)}
          />
          <button
            className="primary"
            disabled={!caption || !assets.length}
            onClick={() => post.mutate()}
          >
            Save Draft
          </button>
          {posts.data?.items?.map((p: any) => (
            <article key={p.id}>
              <p>
                {p.status} {p.moderationReason}
              </p>
              {p.status === "REJECTED" && (
                <button onClick={() => resubmitPost(p)}>
                  Edit &amp; Resubmit
                </button>
              )}
              {p.status === "DRAFT" && (
                <button
                  onClick={() =>
                    creatorPostUpdate(p.id, {
                      status: "PENDING",
                      caption: p.caption,
                      mediaIds: p.media?.map((m: any) => m.mediaAssetId),
                    }).then(() => posts.refetch())
                  }
                >
                  Submit for Moderation
                </button>
              )}
            </article>
          ))}
        </div>
      )}
      {tab === "Reels" && (
        <div className="empty">
          <h3>Reel editor</h3>
          <MediaUpload kind="IMAGE" onReady={setReel} />
          <MediaUpload kind="VIDEO" onReady={setReel} />
          <Library onSelect={setReel} />
          <Preview asset={reel} />
          <textarea
            aria-label="Reel caption"
            value={reelCaption}
            onChange={(e) => setReelCaption(e.target.value)}
          />
          <button
            className="primary"
            disabled={!reel}
            onClick={() => reelSave.mutate()}
          >
            Save Draft
          </button>
          {rs.data?.items?.map((r: any) => (
            <article key={r.id}>
              <p>
                {r.status} {r.moderationReason}
              </p>
              <button
                onClick={() =>
                  creatorPostUpdate(r.id, {
                    status: r.status === "REJECTED" ? "PENDING" : "PENDING",
                    caption: r.caption,
                    mediaIds: r.media?.map((m: any) => m.mediaAssetId),
                  }).then(() => rs.refetch())
                }
              >
                {r.status === "REJECTED"
                  ? "Edit & Resubmit"
                  : "Submit for Moderation"}
              </button>
            </article>
          ))}
        </div>
      )}
      {tab === "VIP" && (
        <div className="empty">
          <input
            aria-label="VIP title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
          <input
            aria-label="Affiliate URL"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
          <button
            className="primary"
            disabled={!title || !url}
            onClick={() => vip.mutate()}
          >
            Save / Submit
          </button>
          <p>
            {v.data?.status} {v.data?.moderationReason}
          </p>
        </div>
      )}
      {tab === "Analytics" && (
        <div className="empty">
          <p>
            {a.data &&
              `Views ${a.data.profileViews} · Followers ${a.data.followers}`}
          </p>
        </div>
      )}
    </section>
  );
}
export function AdminPanel() {
  const [tab, setTab] = useState("Creators");
  const [reason, setReason] = useState("");
  const q = useQuery({
    queryKey: ["admin-creators"],
    queryFn: adminCreators,
    enabled: tab === "Creators",
  });
  const p = useQuery({
    queryKey: ["admin-posts"],
    queryFn: adminPosts,
    enabled: tab === "Content",
  });
  const v = useQuery({
    queryKey: ["admin-vip"],
    queryFn: adminVip,
    enabled: tab === "VIP",
  });
  const cm = useMutation({
    mutationFn: ({ id, action }: { id: string; action: string }) =>
      adminModerateCreator(id, action, reason),
    onSuccess: () => q.refetch(),
  });
  const pm = useMutation({
    mutationFn: ({ id, action }: { id: string; action: string }) =>
      adminModeratePost(id, action, reason),
    onSuccess: () => p.refetch(),
  });
  const vm = useMutation({
    mutationFn: ({ id, action }: { id: string; action: string }) =>
      adminModerateVip(id, action, reason),
    onSuccess: () => v.refetch(),
  });
  const act = (m: any, id: string, a: string) => {
    if (a !== "APPROVE" && !reason.trim()) return;
    m.mutate({ id, action: a });
  };
  return (
    <section className="account">
      <h1>Moderation</h1>
      <div className="content-tabs">
        {["Creators", "Content", "VIP"].map((x) => (
          <button
            className={tab === x ? "selected" : ""}
            onClick={() => setTab(x)}
            key={x}
          >
            {x}
          </button>
        ))}
      </div>
      <textarea
        aria-label="Moderation reason"
        placeholder="Reason"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
      />
      {tab === "Creators" &&
        q.data?.map((c: any) => (
          <article className="vip-card" key={c.id}>
            <h3>
              {c.displayName} · {c.profile?.status}
            </h3>
            <button onClick={() => act(cm, c.id, "APPROVE")}>Approve</button>
            <button onClick={() => act(cm, c.id, "REJECT")}>Reject</button>
          </article>
        ))}
      {tab === "Content" &&
        p.data?.map((x: any) => (
          <article className="vip-card" key={x.id}>
            <h3>
              {x.creator?.displayName} · {x.status}
            </h3>
            <p>
              {x.source === "TELEGRAM_CHANNEL" ? "TELEGRAM_CHANNEL" : "GPOINT"}{" "}
              · {x.type} · {x.sanitizedText || x.caption} · safety:{" "}
              {x.safetyClassification || "UNKNOWN"} ·{" "}
              {x.moderationReason || x.safetyReason || x.ingestStatus || ""}
            </p>
            {x.media?.map((m: any) => (
              <Preview
                key={m.mediaAssetId}
                asset={{ id: m.mediaAssetId, kind: m.media.kind }}
                admin
              />
            ))}
            <button onClick={() => act(pm, x.id, "APPROVE")}>Approve</button>
            <button onClick={() => act(pm, x.id, "REJECT")}>Reject</button>
            <button onClick={() => act(pm, x.id, "HIDE")}>Hide</button>
          </article>
        ))}
      {tab === "VIP" &&
        v.data?.map((x: any) => (
          <article className="vip-card" key={x.id}>
            <h3>
              {x.creator?.displayName} · {x.title} · {x.status}
            </h3>
            <p>
              {x.affiliateUrl} · {x.moderationReason}
            </p>
            <button onClick={() => act(vm, x.id, "APPROVE")}>Approve</button>
            <button onClick={() => act(vm, x.id, "REJECT")}>Reject</button>
          </article>
        ))}
    </section>
  );
}
function xhr(
  url: string,
  method: string,
  body: XMLHttpRequestBodyInit,
  headers: Record<string, string>,
  progress: (n: number) => void,
) {
  return new Promise<void>((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open(method, url);
    Object.entries(headers).forEach(([k, v]) => x.setRequestHeader(k, v));
    x.upload.onprogress = (e) =>
      e.lengthComputable && progress(Math.round((e.loaded / e.total) * 100));
    x.onload = () =>
      x.status >= 200 && x.status < 300
        ? resolve()
        : reject(Error(`upload ${x.status}`));
    x.onerror = () => reject(Error("network upload failed"));
    x.send(body);
  });
}

export function MediaUpload({
  kind,
  onReady,
}: {
  kind: "IMAGE" | "VIDEO";
  onReady: (asset: any) => void;
}) {
  const [state, setState] = useState("IDLE");
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState("");
  const choose = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const allowed =
      kind === "IMAGE"
        ? ["image/jpeg", "image/png", "image/webp"]
        : ["video/mp4"];
    if (!allowed.includes(file.type)) {
      setError("Unsupported MIME");
      setState("ERROR");
      return;
    }
    if (file.size > (kind === "IMAGE" ? 10 : 50) * 1024 * 1024) {
      setError("File too large");
      setState("ERROR");
      return;
    }
    try {
      setState("VALIDATING");
      const intent = await creatorMediaIntent({
        kind,
        mimeType: file.type,
        fileSize: file.size,
      });
      setState("UPLOADING");
      const bytes = await file.arrayBuffer();
      if (intent.uploadUrl.startsWith("http"))
        await xhr(
          intent.uploadUrl,
          "PUT",
          bytes,
          { "content-type": file.type },
          setProgress,
        );
      else {
        const dataBase64 = btoa(String.fromCharCode(...new Uint8Array(bytes)));
        await xhr(
          `${apiBase}${intent.uploadUrl}`,
          "POST",
          JSON.stringify({ dataBase64 }),
          {
            "content-type": "application/json",
            authorization: `Bearer ${getToken()}`,
          },
          setProgress,
        );
      }
      setState("FINALIZING");
      const asset = await creatorMediaFinalize(intent.mediaId);
      setProgress(100);
      setState("READY");
      onReady(asset);
    } catch (err) {
      setState("ERROR");
      setError(err instanceof Error ? err.message : "Upload failed");
    }
  };
  return (
    <div className="empty">
      <input
        type="file"
        accept={
          kind === "IMAGE" ? "image/jpeg,image/png,image/webp" : "video/mp4"
        }
        onChange={choose}
      />
      <p>
        {state}
        {state === "UPLOADING" && ` ${progress}%`}
      </p>
      {state === "ERROR" && (
        <>
          <p role="alert">{error}</p>
          <button
            onClick={() => {
              setState("IDLE");
              setError("");
            }}
          >
            Retry
          </button>
        </>
      )}
    </div>
  );
}

function formatDashboardDate(value?: string | null) {
  if (!value) return "Not synchronized yet";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Not synchronized yet";
  return new Intl.DateTimeFormat(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function StatusBadge({ status, tone = "neutral" }: { status: string; tone?: string }) {
  return <span className={`dashboard-status dashboard-status-${tone}`}>{status}</span>;
}

export function TelegramChannelControls() {
  const q = useQuery({
    queryKey: ["telegram-channel"],
    queryFn: telegramChannel,
  });
  const [mode, setMode] = useState("AUTO");
  const [hashtag, setHashtag] = useState("gpoint");
  const connect = useMutation({ mutationFn: startTelegramChannelConnection });
  const update = useMutation({
    mutationFn: () =>
      updateTelegramChannel({ importMode: mode, importHashtag: hashtag }),
    onSuccess: () => q.refetch(),
  });
  const disconnect = useMutation({
    mutationFn: disconnectTelegramChannel,
    onSuccess: () => q.refetch(),
  });
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const [saved, setSaved] = useState(false);
  const c = q.data;
  useEffect(() => {
    if (c) {
      setMode(c.importMode);
      setHashtag(c.importHashtag || "gpoint");
    }
  }, [c?.id, c?.importMode, c?.importHashtag]);
  if (q.isLoading) return <div className="dashboard-skeleton" aria-label="Loading channel" />;
  if (!c)
    return (
      <section className="dashboard-section dashboard-channel-panel">
        <p className="dashboard-eyebrow">MY TELEGRAM CHANNEL</p>
        <div className="dashboard-empty-card">
          <div className="dashboard-icon-circle"><Import size={20} /></div>
          <div>
            <h2>Connect Telegram Channel</h2>
            <p className="profile-bio">
          Select any Telegram channel where you are an administrator. GPoint
          verifies the channel and its own posting permission after selection.
            </p>
          </div>
          <button className="primary dashboard-primary" disabled={connect.isPending} onClick={() => connect.mutate()}>
            {connect.isPending ? "Preparing…" : "Select Telegram Channel"}
          </button>
        </div>
        {connect.data && <p role="status">{connect.data.instructions}</p>}
        {connect.error && (
          <p role="alert">{(connect.error as Error).message}</p>
        )}
      </section>
    );
  return (
    <section className="dashboard-section dashboard-channel-panel" aria-label="Telegram channel">
      <p className="dashboard-eyebrow">MY TELEGRAM CHANNEL</p>
      <div className="channel-card">
        <div className="channel-avatar">{(c.title || c.username || "G").slice(0, 1).toUpperCase()}</div>
        <div className="channel-copy">
          <h2>{c.title || c.username || "Telegram channel"}</h2>
          <p>Telegram channel</p>
          <StatusBadge status="Connected" tone="success" />
        </div>
        <ChevronRight className="channel-chevron" size={20} aria-hidden="true" />
      </div>
      <div className="channel-meta"><span>Last sync</span><strong>{formatDashboardDate(c.lastSuccessfulIngestAt)}</strong></div>
      {c.lastError && <p className="dashboard-error" role="alert">{c.lastError}</p>}
      <div className="settings-card">
        <div className="settings-card-heading"><div className="dashboard-icon-circle"><Settings2 size={18} /></div><div><p className="dashboard-eyebrow">IMPORT SETTINGS</p><h3>Keep your channel in sync</h3></div></div>
        <label className="dashboard-field">Import mode
          <select aria-label="Import mode" value={mode} onChange={(e) => { setMode(e.target.value); setSaved(false); }}>
            <option value="AUTO">Auto</option><option value="HASHTAG">Hashtag</option><option value="MANUAL">Manual</option>
          </select>
        </label>
        <p className="field-hint">{mode === "AUTO" ? "New posts will be imported automatically from your Telegram channel." : mode === "HASHTAG" ? "Only posts with this hashtag will be imported." : "Choose imported posts manually from your connected channel."}</p>
        {mode === "HASHTAG" && <label className="dashboard-field">Hashtag<input aria-label="Import hashtag" value={hashtag} onChange={(e) => { setHashtag(e.target.value); setSaved(false); }} placeholder="gpoint" /></label>}
        <div className="settings-actions">
          <button className="dashboard-primary" disabled={update.isPending} onClick={() => { setSaved(false); update.mutate(undefined, { onSuccess: () => setSaved(true) }); }}><Save size={15} />{update.isPending ? "Saving…" : "Save settings"}</button>
          {saved && <span className="save-confirm" role="status"><Check size={14} /> Saved</span>}
        </div>
      </div>
      <div className="danger-zone">
        <button className="danger-link" onClick={() => setConfirmDisconnect(true)}><Unplug size={15} /> Channel settings</button>
        {confirmDisconnect && <div className="confirm-panel" role="dialog" aria-label="Confirm channel disconnect"><strong>Disconnect this channel?</strong><p>Future sync will stop. Historical GPoint posts remain.</p><div><button onClick={() => setConfirmDisconnect(false)}>Cancel</button><button className="danger-confirm" disabled={disconnect.isPending} onClick={() => disconnect.mutate(undefined, { onSuccess: () => setConfirmDisconnect(false) })}>Disconnect</button></div></div>}
      </div>
    </section>
  );
}

export function CreatorWorkspaceDashboard() {
  const q = useQuery({ queryKey: ["creator-me"], queryFn: creatorMe });
  const [tab, setTab] = useState("Overview");
  const posts = useQuery({
    queryKey: ["creator-imported-posts"],
    queryFn: async () => {
      const response = await fetch(`${apiBase}/creator/me/posts`, {
        headers: { authorization: `Bearer ${getToken()}` },
      });
      if (!response.ok) throw new Error(`posts ${response.status}`);
      return response.json();
    },
    enabled: tab === "Imported Posts",
  });
  const stats = useQuery({
    queryKey: ["creator-analytics"],
    queryFn: creatorAnalytics,
    enabled: tab === "Stats",
  });
  const profileSave = useMutation({ mutationFn: (body: any) => creatorProfileUpdate(body), onSuccess: () => q.refetch() });
  const [profileName, setProfileName] = useState("");
  const [profileSlug, setProfileSlug] = useState("");
  const [profileBio, setProfileBio] = useState("");
  useEffect(() => {
    if (q.data?.creator) {
      setProfileName(q.data.creator.displayName || "");
      setProfileSlug(q.data.creator.slug || "");
      setProfileBio(q.data.creator.bio || "");
    }
  }, [q.data?.creator?.id, q.data?.creator?.displayName, q.data?.creator?.slug, q.data?.creator?.bio]);
  if (q.isLoading) return <div className="empty">Loading creator workspace…</div>;
  const creator = q.data?.creator;
  if (!creator) return <div className="empty">Creator workspace unavailable.</div>;
  const tools = [
    { tab: "Profile settings", title: "Profile settings", copy: "Manage your creator profile", icon: <Settings2 size={18} /> },
    { tab: "Imported Posts", title: "Imported posts", copy: "View and manage content", icon: <FileText size={18} /> },
    { tab: "Stats", title: "Stats", copy: "Track your growth", icon: <BarChart3 size={18} /> },
  ];
  return (
    <section className="creator-workspace" aria-label="Creator tools">
      <div className="dashboard-heading"><p className="dashboard-eyebrow">CREATOR TOOLS</p><h1>Creator Dashboard</h1><p>Your content, your audience, more possibilities.</p></div>
      <div className="content-tabs">
        {['Overview', 'Profile settings', 'Imported Posts', 'Stats'].map((item) => <button className={tab === item ? 'selected' : ''} onClick={() => setTab(item)} key={item}>{item}</button>)}
      </div>
      {tab === 'Overview' && (
        <>
          <div className="workspace-hero"><div className="hero-status-icon"><CheckCircle2 size={25} /></div><div><p className="dashboard-eyebrow">WORKSPACE STATUS</p><h2>Creator workspace active</h2><p>Telegram channel connected and ready for ingestion.</p></div><StatusBadge status={creator.status === "APPROVED" ? "Live" : "Pending review"} tone={creator.status === "APPROVED" ? "success" : "warm"} /></div>
              <TelegramChannelControls />
          <div className="dashboard-section"><p className="dashboard-eyebrow">CREATOR TOOLS</p><div className="tool-grid">{tools.map((tool) => <button className="tool-card" key={tool.tab} onClick={() => setTab(tool.tab)}><span className="tool-icon">{tool.icon}</span><span><strong>{tool.title}</strong><small>{tool.copy}</small></span><ChevronRight size={17} /></button>)}</div></div>
        </>
      )}
      {tab === 'Profile settings' && (
        <div className="dashboard-section profile-settings-panel">
          <p className="dashboard-eyebrow">PROFILE SETTINGS</p><h2>Shape your public profile</h2><p className="dashboard-copy">Keep your creator identity clear and easy to recognize.</p>
          <label className="dashboard-field">Display name<input aria-label="Creator display name" value={profileName} onChange={(e) => setProfileName(e.target.value)} /></label>
          <label className="dashboard-field">GPoint handle<input aria-label="Creator handle" value={profileSlug} onChange={(e) => setProfileSlug(e.target.value)} /></label>
          <label className="dashboard-field">Bio<textarea aria-label="Creator bio" rows={4} value={profileBio} onChange={(e) => setProfileBio(e.target.value)} placeholder="Tell people what you create." /></label>
          <button className="dashboard-primary" disabled={profileSave.isPending} onClick={() => profileSave.mutate({ displayName: profileName, slug: profileSlug, bio: profileBio, tags: [] })}><Save size={15} />{profileSave.isPending ? "Saving…" : "Save profile"}</button>
          <div className="profile-state"><ShieldCheck size={16} /><span>Public profile:</span><StatusBadge status={creator.status === "APPROVED" ? "Live" : "Pending review"} tone={creator.status === "APPROVED" ? "success" : "warm"} /></div>
        </div>
      )}
      {tab === 'Imported Posts' && (
        <div className="dashboard-section">
          <div className="dashboard-section-title"><div><p className="dashboard-eyebrow">CONTENT LIBRARY</p><h2>Imported Posts</h2></div><span className="muted">{posts.data?.items?.length ?? 0} total</span></div>
          {posts.isLoading && <p>Loading imported posts…</p>}
          {posts.isError && <p className="dashboard-error" role="alert">Couldn't load creator data. Try again.</p>}
          {posts.data?.items?.length ? posts.data.items.map((post: any) => (
            <article className="imported-post-card" key={post.id}>
              {post.media?.[0]?.readUrl ? <img src={mediaUrl(post.media[0].readUrl)} alt="" /> : <div className="post-placeholder"><FileText size={20} /></div>}
              <div><p>{post.caption || 'Imported Telegram post'}</p><StatusBadge status={post.safetyClassification === 'UNCERTAIN' || post.ingestStatus === 'PENDING_MODERATION' ? 'Pending review' : post.status === 'PUBLISHED' ? 'Published' : post.status === 'REJECTED' ? 'Rejected' : 'Pending review'} tone={post.status === 'REJECTED' ? 'danger' : post.status === 'PUBLISHED' ? 'success' : 'warm'} /></div><ChevronRight size={17} />
            </article>
          )) : !posts.isLoading && <div className="dashboard-empty-card"><FileText size={19} /><p>No imported posts yet.</p></div>}
        </div>
      )}
      {tab === 'Stats' && (
        <div className="dashboard-section">
          <p className="dashboard-eyebrow">YOUR MOMENTUM</p><h2>Stats</h2><p className="dashboard-copy">A simple view of how your profile is being discovered.</p>
          <div className="stat-grid">{[["Profile views", stats.data?.profileViews ?? 0], ["Followers", stats.data?.followers ?? 0], ["Post views", stats.data?.postViews ?? 0], ["Reel views", stats.data?.reelViews ?? 0]].map(([label, value]) => <div className="stat-card" key={String(label)}><strong>{value}</strong><span>{label}</span></div>)}</div>
        </div>
      )}
    </section>
  );
}
