"use client";
// portal/app/portal/_shell/PortalTopbar.tsx
// AA-759 — the portal topbar (56 px, every portal page). Left: ☰ (below 900 px) + breadcrumb
// (<nav group> / <page>, kit Breadcrumb). Centre: the search pill that opens ⌘K (an icon below
// 768 px — search is no longer hidden on phones). Right: theme switch (light → dark → system),
// the notifications bell, the account menu (tenant, plan, settings, legal links, sign out).

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ChevronDown, LogOut, Menu, Monitor, Moon, Search, Settings, Sun, CreditCard } from "lucide-react";
import { Breadcrumb, K, sans, serif, useThemeChoice, type ThemeChoice } from "@/app/_kit";
import { PRIVACY_PATH, TERMS_PATH } from "@/lib/hosts";
import NotificationsBell from "../_components/NotificationsBell";
import type { ShellActivityView } from "../_components/PortalShellContext";
import { ROUTES, pageGroup, pageTitle } from "./portalNav";

export function PortalTopbar({ tenantName, planTier, activity, onOpenSearch, onOpenMenu }: {
  tenantName: string;
  planTier: string;
  activity: ShellActivityView[];
  onOpenSearch: () => void;
  onOpenMenu: () => void;
}) {
  const pathname = usePathname();
  const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

  return (
    <header
      data-pshell="topbar"
      style={{
        height: 56, flexShrink: 0, background: K.card, borderBottom: `1px solid ${K.line}`,
        display: "flex", alignItems: "center", position: "sticky", top: 0, zIndex: 20,
        fontFamily: sans, minWidth: 0,
      }}
    >
      <button data-pshell="menu-btn" onClick={onOpenMenu} aria-label="Open menu" style={iconBtn}>
        <Menu size={18} />
      </button>

      <Breadcrumb root={pageGroup(pathname)} title={pageTitle(pathname)} fontSize={12.5} />

      <button
        data-pshell="search-pill"
        onClick={onOpenSearch}
        aria-label="Search tours, content and pages"
        style={{
          display: "flex", alignItems: "center", gap: 8, maxWidth: 380, flex: 1, minWidth: 0,
          margin: "0 auto", padding: "7px 12px", borderRadius: 999, border: `1px solid ${K.line}`,
          background: K.bg, color: K.muted, cursor: "pointer", fontFamily: sans, fontSize: 13,
        }}
      >
        <Search size={15} style={{ flexShrink: 0 }} />
        <span style={{ flex: 1, textAlign: "left", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          Search tours, content, pages…
        </span>
        <kbd style={{ fontSize: 11, color: K.muted, border: `1px solid ${K.line}`, borderRadius: 6, padding: "1px 6px", background: K.card, flexShrink: 0, fontFamily: sans }}>
          {isMac ? "⌘K" : "Ctrl K"}
        </kbd>
      </button>

      <span data-pshell="spacer" style={{ flex: 1 }} />

      <div style={{ display: "flex", alignItems: "center", gap: 4, flexShrink: 0 }}>
        <button data-pshell="search-icon" onClick={onOpenSearch} aria-label="Search" style={iconBtn}>
          <Search size={18} />
        </button>
        <ThemeToggle />
        <NotificationsBell activity={activity} />
        <AccountMenu tenantName={tenantName} planTier={planTier} />
      </div>
    </header>
  );
}

const iconBtn: React.CSSProperties = {
  alignItems: "center", justifyContent: "center", width: 36, height: 36, borderRadius: 8,
  border: "none", background: "transparent", color: K.ink3, cursor: "pointer", flexShrink: 0,
};

// ── Theme switch (same cycle as the admin topbar: light → dark → system) ─────
const THEME_OPTIONS: { key: ThemeChoice; label: string; icon: React.ReactNode }[] = [
  { key: "light", label: "Light", icon: <Sun size={16} /> },
  { key: "dark", label: "Dark", icon: <Moon size={16} /> },
  { key: "system", label: "System", icon: <Monitor size={16} /> },
];

function ThemeToggle() {
  const [choice, setChoice] = useThemeChoice();
  const i = Math.max(0, THEME_OPTIONS.findIndex((o) => o.key === choice));
  const cur = THEME_OPTIONS[i];
  const next = THEME_OPTIONS[(i + 1) % THEME_OPTIONS.length];
  return (
    <button
      onClick={() => setChoice(next.key)}
      data-testid="portal-theme-toggle"
      title={`Theme: ${cur.label} — click for ${next.label}`}
      aria-label={`Theme: ${cur.label}. Switch to ${next.label}`}
      style={{ ...iconBtn, display: "flex", color: choice === "system" ? K.muted : K.accent }}
    >
      {cur.icon}
    </button>
  );
}

// ── Account menu ─────────────────────────────────────────────────────────────
function AccountMenu({ tenantName, planTier }: { tenantName: string; planTier: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  // Initials from the words that start with a letter or digit ("Atlas & Hearth" → "AH", not "A&").
  const initials = tenantName.split(/\s+/).filter((w) => /^[\p{L}\p{N}]/u.test(w)).map((w) => w[0])
    .join("").slice(0, 2).toUpperCase() || "P";
  const plan = planTier ? `${planTier.charAt(0).toUpperCase()}${planTier.slice(1)} plan` : "";

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("mousedown", onDown); window.removeEventListener("keydown", onKey); };
  }, [open]);

  async function logout() {
    // AA-427: the tenant cookies are httpOnly — cleared server-side. Redirect either way; the page
    // gate re-verifies the token on the next request.
    try { await fetch("/api/auth/tenant-logout", { method: "POST" }); } catch { /* ignore */ }
    window.location.href = "/tenant-login";
  }

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-label="Account menu"
        aria-expanded={open}
        aria-haspopup="menu"
        style={{
          display: "flex", alignItems: "center", gap: 8, padding: "3px 8px 3px 3px", borderRadius: 999,
          border: `1px solid ${K.line}`, background: K.card, cursor: "pointer", fontFamily: sans,
        }}
      >
        <span aria-hidden style={{
          width: 28, height: 28, borderRadius: "50%", background: K.accent, display: "grid", placeItems: "center",
          color: "var(--aa-on-accent)", fontWeight: 700, fontSize: 11.5,
        }}>{initials}</span>
        <span data-pshell="account-name" style={{ fontSize: 13, fontWeight: 500, color: K.ink, maxWidth: 140, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {tenantName}
        </span>
        <ChevronDown size={14} style={{ color: K.muted2 }} />
      </button>
      {open && (
        <div role="menu" aria-label="Account" style={{
          position: "absolute", top: "100%", right: 0, marginTop: 6, width: 220, zIndex: 100,
          background: K.card, border: `1px solid ${K.line}`, borderRadius: 10,
          boxShadow: "0 12px 32px rgba(0,0,0,0.18)", overflow: "hidden",
        }}>
          <div style={{ padding: "10px 14px", borderBottom: `1px solid ${K.line}` }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: K.ink, fontFamily: serif, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{tenantName}</div>
            {plan && <div style={{ fontSize: 11, color: K.muted }}>{plan}</div>}
          </div>
          <MenuLink href={ROUTES.settings} icon={<Settings size={15} />} label="Team & Settings" onClick={() => setOpen(false)} />
          <MenuLink href={ROUTES.billing} icon={<CreditCard size={15} />} label="Billing" onClick={() => setOpen(false)} />
          <button role="menuitem" onClick={logout} style={menuRow}>
            <span style={{ color: K.muted2, display: "flex" }}><LogOut size={15} /></span>
            Sign out
          </button>
          {/* AA-885/AA-886 — the portal's draft legal pages (moved here from the old sidebar footer). */}
          <div data-testid="portal-legal-links" style={{ display: "flex", gap: 14, padding: "8px 14px 10px", borderTop: `1px solid ${K.line}`, fontSize: 11 }}>
            <a href={PRIVACY_PATH} target="_blank" rel="noopener noreferrer" style={{ color: K.muted, textDecoration: "none" }}>Privacy</a>
            <a href={TERMS_PATH} target="_blank" rel="noopener noreferrer" style={{ color: K.muted, textDecoration: "none" }}>Terms</a>
          </div>
        </div>
      )}
    </div>
  );
}

const menuRow: React.CSSProperties = {
  display: "flex", alignItems: "center", gap: 10, width: "100%", padding: "9px 14px",
  border: "none", background: "transparent", cursor: "pointer", color: K.ink3,
  fontSize: 13, fontFamily: sans, textAlign: "left", textDecoration: "none", boxSizing: "border-box",
};

function MenuLink({ href, icon, label, onClick }: { href: string; icon: React.ReactNode; label: string; onClick: () => void }) {
  return (
    <Link href={href} role="menuitem" onClick={onClick} style={menuRow}>
      <span style={{ color: K.muted2, display: "flex" }}>{icon}</span>
      {label}
    </Link>
  );
}
