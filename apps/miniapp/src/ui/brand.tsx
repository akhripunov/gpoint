import type { ReactNode } from "react";

export function GPointLogo() {
  return <span className="gpoint-logo" aria-label="GPoint">GPoint<span aria-hidden="true">•</span></span>;
}

export function GPointAppHeader({
  onHome,
  logo: logoOverride,
  left,
  right,
  subtitle,
  centered = false,
  className = "",
}: {
  onHome?: () => void;
  logo?: ReactNode;
  left?: ReactNode;
  right?: ReactNode;
  subtitle?: string;
  centered?: boolean;
  className?: string;
}) {
  const logo = logoOverride ?? <GPointLogo />;
  const brand = onHome
    ? <button type="button" className="gpoint-app-home" onClick={onHome} aria-label="GPoint home">{logo}{subtitle && <span className="gpoint-app-subtitle">{subtitle}</span>}</button>
    : <div className="gpoint-app-brand">{logo}{subtitle && <span className="gpoint-app-subtitle">{subtitle}</span>}</div>;

  if (centered) {
    return <header className={`gpoint-app-header gpoint-app-header-centered ${className}`}><div className="gpoint-header-left">{left}</div><div className="gpoint-header-center">{brand}</div><div className="gpoint-header-right">{right}</div></header>;
  }
  return <header className={`gpoint-app-header ${className}`}><div className="gpoint-header-brand">{brand}</div><div className="gpoint-header-right">{right}</div></header>;
}
