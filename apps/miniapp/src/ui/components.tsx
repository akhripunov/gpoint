import type { ReactNode } from "react";
import { badgeText } from "../messages-unread-policy.js";
import { ChevronLeft, ChevronRight, LoaderCircle, Send } from "lucide-react";

export type StatusTone = "success" | "warm" | "danger" | "neutral";

export function BackButton({ onClick, label = "Back", className = "" }: { onClick?: () => void; label?: string; className?: string }) {
  return <button className={`gpoint-back-button ${className}`} type="button" aria-label={label} onClick={onClick}><ChevronLeft size={22} /></button>;
}

export function Button({ children, variant = "primary", loading = false, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "destructive"; loading?: boolean }) {
  return <button {...props} type={props.type ?? "button"} className={`gpoint-button gpoint-button-${variant} ${props.className ?? ""}`} disabled={props.disabled || loading}><span>{loading && <LoaderCircle className="gpoint-button-spinner" size={16} />}{children}</span></button>;
}

export function StatusBadge({ status, tone = "neutral" }: { status: string; tone?: StatusTone }) {
  return <span className={`gpoint-status-badge gpoint-status-${tone}`}>{status}</span>;
}

export function Tabs({ items, active, onChange, className = "" }: { items: string[]; active: string; onChange: (item: string) => void; className?: string }) {
  return <div className={`gpoint-tabs ${className}`} role="tablist">{items.map((item) => <button type="button" aria-selected={active === item} className={active === item ? "selected" : ""} onClick={() => onChange(item)} key={item}>{item}</button>)}</div>;
}

export function BottomNav({ items, active, onChange }: { items: Array<{ id: string; label: string; icon: ReactNode; badgeCount?: number }>; active: string; onChange: (id: string) => void }) {
  return <nav className="gpoint-bottom-nav bottom-nav" aria-label="Primary navigation">{items.map((item) => <button type="button" className={`gpoint-nav-item${active === item.id ? " active" : ""}`} onClick={() => onChange(item.id)} key={item.id} aria-current={active === item.id ? "page" : undefined} aria-label={item.badgeCount ? `${item.label}, ${item.badgeCount} unread messages` : item.label}><span className="gpoint-nav-icon">{item.badgeCount ? <span className="nav-icon-with-badge">{item.icon}<UnreadBadge count={item.badgeCount} /></span> : item.icon}</span><span className="gpoint-nav-label">{item.label}</span></button>)}</nav>;
}

export function SectionEyebrow({ children }: { children: ReactNode }) { return <p className="gpoint-section-eyebrow">{children}</p>; }

export function WorkspaceStatusCard({ icon, title, body, status, tone = "warm" }: { icon: ReactNode; title: string; body: string; status: string; tone?: StatusTone }) {
  return <div className="gpoint-workspace-status"><span className="gpoint-status-icon">{icon}</span><div className="gpoint-workspace-copy"><h2>{title}</h2><p>{body}</p><StatusBadge status={`Public profile · ${status}`} tone={tone} /></div><ChevronRight className="gpoint-workspace-chevron" size={20} /></div>;
}

export function TelegramChannelCard({ title, subtitle = "Telegram channel", lastSync, status = "Connected", icon, avatarUrl, meta }: { title: string; subtitle?: string; lastSync?: string; status?: string; icon?: ReactNode; avatarUrl?: string | null; meta?: ReactNode }) {
  return <div className="gpoint-telegram-channel"><span className="gpoint-channel-avatar">{avatarUrl ? <img src={avatarUrl} alt="" /> : icon ?? title.slice(0, 1).toUpperCase()}</span><div className="gpoint-channel-copy"><h2>{title}</h2><p>{subtitle}</p><StatusBadge status={status} tone="success" /></div><Send className="gpoint-channel-motif" size={42} strokeWidth={1.15} aria-hidden="true" /><ChevronRight className="gpoint-channel-chevron" size={20} />{(lastSync || meta) && <div className="gpoint-channel-meta"><span>Last sync</span><strong>{meta ?? lastSync}</strong></div>}</div>;
}

export function CreatorToolCard({ title, description, icon, onClick }: { title: string; description: string; icon: ReactNode; onClick?: () => void }) {
  return <button type="button" className="gpoint-tool-card" onClick={onClick}><span className="gpoint-tool-icon">{icon}</span><span className="gpoint-tool-copy"><strong>{title}</strong><small>{description}</small></span><ChevronRight className="gpoint-tool-chevron" size={18} /></button>;
}

export function ImportSettingsCard({ children }: { children: ReactNode }) { return <div className="gpoint-import-settings">{children}</div>; }

export function UnreadBadge({count}:{count:number}) { const text=badgeText(count); return text ? <span className="messages-unread-badge" aria-hidden="true">{text}</span> : null; }
