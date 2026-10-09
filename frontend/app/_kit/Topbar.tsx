"use client";
// app/_kit/Topbar.tsx
// AA-752 — the admin shell topbar (60px, on every admin page). Left: breadcrumb (Admin / <page>).
// Centre: the global search trigger (a pill that opens the ⌘K palette, with a ⌘K / Ctrl K hint).
// Right: theme switch (moved from the sidebar), notifications bell + dropdown (moved as-is), and
// the account menu (avatar initial + name → Settings, Sign out — the existing admin-logout). No
// environment label.

import { useEffect, useRef, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { Search, Bell, Sun, Moon, Monitor, Settings, LogOut, ChevronDown, Menu as MenuIcon } from "lucide-react";
import { K, sans, serif, alpha } from "./tokens";
import { useThemeChoice } from "./useTheme";
import type { ThemeChoice } from "./theme";
import { useAdminIdentity } from "./useAdminIdentity";
import { pageTitle } from "./adminNav";

interface Notif {
  id: number; event_type: string; title: string; message: string;
  entity_type: string; entity_id: string; is_read: boolean; created_at: string;
}

export function Topbar({ onOpenSearch, onOpenMenu }: { onOpenSearch: () => void; onOpenMenu: () => void }) {
  const pathname = usePathname();
  const title = pageTitle(pathname);
  const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

  return (
    <header
      style={{
        height: 60, flexShrink: 0, background: K.card, borderBottom: `1px solid ${K.line}`,
        display: "flex", alignItems: "center", gap: 16, padding: "0 24px",
        position: "sticky", top: 0, zIndex: 20, fontFamily: sans,
      }}
    >
      {/* Mobile hamburger — opens the drawer (< 768px only). */}
      <button
        className="aa-topbar-menu"
        onClick={onOpenMenu}
        aria-label="Open navigation menu"
        style={{
          display: "none", alignItems: "center", justifyContent: "center",
          width: 40, height: 40, borderRadius: 8, border: "none",
          background: "transparent", color: K.ink3, cursor: "pointer", flexShrink: 0,
        }}
      >
        <MenuIcon size={20} />
      </button>

      {/* Breadcrumb */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
        <span className="aa-topbar-crumb-root" style={{ fontSize: 13, color: K.muted2 }}>Admin</span>
        <span className="aa-topbar-crumb-root" style={{ fontSize: 13, color: K.muted2 }}>/</span>
        <span style={{ fontSize: 13, fontWeight: 600, color: K.ink }}>{title}</span>
      </div>

      {/* Search trigger (full pill on desktop) */}
      <button
        className="aa-topbar-search"
        onClick={onOpenSearch}
        aria-label="Open global search"
        style={{
          display: "flex", alignItems: "center", gap: 8, maxWidth: 420, flex: 1,
          minWidth: 0, margin: "0 auto", padding: "7px 12px", borderRadius: 999,
          border: `1px solid ${K.line}`, background: K.bg, color: K.muted,
          cursor: "pointer", fontFamily: sans, fontSize: 13,
        }}
      >
        <Search size={15} style={{ flexShrink: 0 }} />
        <span style={{ flex: 1, textAlign: "left", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          Search tours, tenants, jobs…
        </span>
        <kbd style={{
          fontSize: 11, color: K.muted, border: `1px solid ${K.line}`, borderRadius: 6,
          padding: "1px 6px", background: K.card, flexShrink: 0,
        }}>{isMac ? "⌘K" : "Ctrl K"}</kbd>
      </button>

      {/* Spacer so the right cluster stays right-aligned on mobile (where the pill is hidden). */}
      <span className="aa-topbar-spacer" style={{ display: "none", flex: 1 }} />

      {/* Search icon (mobile only) */}
      <button
        className="aa-topbar-search-icon"
        onClick={onOpenSearch}
        aria-label="Search"
        style={{
          display: "none", alignItems: "center", justifyContent: "center",
          width: 40, height: 40, borderRadius: 8, border: "none",
          background: "transparent", color: K.ink3, cursor: "pointer", flexShrink: 0,
        }}
      >
        <Search size={18} />
      </button>

      {/* Right cluster */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
        <span className="aa-topbar-theme"><ThemeToggle /></span>
        <span className="aa-topbar-notif"><Notifications /></span>
        <AccountMenu />
      </div>
    </header>
  );
}

// ── Theme switch (moved from AdminSidebar) ──────────────────────────────────
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
      title={`Theme: ${cur.label} — click for ${next.label}`}
      aria-label={`Theme: ${cur.label}. Switch to ${next.label}`}
      style={{
        display: "flex", alignItems: "center", justifyContent: "center",
        width: 36, height: 36, borderRadius: 8, border: "none",
        background: "transparent", cursor: "pointer",
        color: choice === "system" ? K.muted : K.accent,
      }}
    >
      {cur.icon}
    </button>
  );
}

// ── Notifications (moved from AdminSidebar) ─────────────────────────────────
function Notifications() {
  const [open, setOpen] = useState(false);
  const [unread, setUnread] = useState(0);
  const [notifs, setNotifs] = useState<Notif[]>([]);
  const ref = useRef<HTMLDivElement>(null);

  // Poll the unread count (same cadence as before). setState runs in the fetch callback.
  useEffect(() => {
    const fetchCount = () => {
      fetch("/api/admin/notifications/count")
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => d && setUnread(d.unread))
        .catch(() => {});
    };
    fetchCount();
    const id = window.setInterval(fetchCount, 30000);
    return () => window.clearInterval(id);
  }, []);

  // Click-outside closes the dropdown.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [open]);

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next) {
      fetch("/api/admin/notifications?unread_only=false&limit=10")
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => d && setNotifs(d.items))
        .catch(() => {});
    }
  }

  function markAllRead() {
    fetch("/api/admin/notifications/read-all", { method: "PUT" })
      .then(() => { setUnread(0); setNotifs((n) => n.map((x) => ({ ...x, is_read: true }))); })
      .catch(() => {});
  }

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        onClick={toggle}
        title="Notifications"
        aria-label="Notifications"
        style={{
          position: "relative", display: "flex", alignItems: "center", justifyContent: "center",
          width: 36, height: 36, borderRadius: 8, border: "none",
          background: "transparent", cursor: "pointer", color: K.ink3,
        }}
      >
        <Bell size={17} />
        {unread > 0 && (
          <span style={{
            position: "absolute", top: 4, right: 4,
            background: K.danger, color: "var(--aa-on-solid)",
            borderRadius: 999, fontSize: 9, fontWeight: 700,
            minWidth: 14, height: 14, display: "grid", placeItems: "center", padding: "0 3px",
          }}>{unread > 99 ? "99+" : unread}</span>
        )}
      </button>
      {open && (
        <div style={{
          position: "absolute", top: "100%", right: 0, marginTop: 6, width: 320, zIndex: 100,
          background: K.card, border: `1px solid ${K.line}`, borderRadius: 10,
          boxShadow: "0 12px 32px rgba(0,0,0,0.18)", maxHeight: 360, overflowY: "auto",
        }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 14px", borderBottom: `1px solid ${K.line}` }}>
            <span style={{ fontSize: 12, fontWeight: 600, color: K.ink }}>Notifications</span>
            {unread > 0 && (
              <button onClick={markAllRead} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 11, color: K.accent, fontWeight: 600 }}>
                Mark all read
              </button>
            )}
          </div>
          {notifs.length === 0 ? (
            <div style={{ padding: "18px 14px", fontSize: 12, color: K.muted, textAlign: "center" }}>No notifications</div>
          ) : notifs.map((n) => (
            <div key={n.id} style={{ padding: "9px 14px", background: n.is_read ? "transparent" : alpha(K.accent, 8), borderBottom: `1px solid ${K.line2}` }}>
              <div style={{ fontSize: 12, color: K.ink, fontWeight: n.is_read ? 400 : 600 }}>{n.title || n.event_type}</div>
              {n.message && <div style={{ fontSize: 11, color: K.muted, marginTop: 2 }}>{n.message}</div>}
              <div style={{ fontSize: 10, color: K.muted2, marginTop: 2 }}>{new Date(n.created_at).toLocaleString()}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Account menu (avatar → Settings, Sign out) ──────────────────────────────
function AccountMenu() {
  const router = useRouter();
  const { userName, isAdmin } = useAdminIdentity();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [open]);

  async function logout() {
    try {
      await fetch("/api/auth/admin-logout", { method: "POST" });
    } catch {
      // ignore — redirect either way; middleware re-verifies on the next request.
    }
    router.push("/login");
  }

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-label="Account menu"
        style={{
          display: "flex", alignItems: "center", gap: 8, padding: "4px 8px 4px 4px",
          borderRadius: 999, border: `1px solid ${K.line}`, background: K.card, cursor: "pointer",
          fontFamily: sans,
        }}
      >
        <span style={{
          width: 28, height: 28, borderRadius: "50%", background: K.accent,
          display: "grid", placeItems: "center", color: "var(--aa-on-accent)", fontWeight: 700, fontSize: 12,
        }}>{userName.charAt(0).toUpperCase()}</span>
        <span style={{ fontSize: 13, fontWeight: 500, color: K.ink, maxWidth: 120, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {userName}
        </span>
        <ChevronDown size={14} style={{ color: K.muted2 }} />
      </button>
      {open && (
        <div style={{
          position: "absolute", top: "100%", right: 0, marginTop: 6, width: 200, zIndex: 100,
          background: K.card, border: `1px solid ${K.line}`, borderRadius: 10,
          boxShadow: "0 12px 32px rgba(0,0,0,0.18)", overflow: "hidden",
        }}>
          <div style={{ padding: "10px 14px", borderBottom: `1px solid ${K.line}` }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: K.ink, fontFamily: serif }}>{userName}</div>
            <div style={{ fontSize: 11, color: K.muted }}>{isAdmin ? "Administrator" : "Content Team"}</div>
          </div>
          <MenuRow icon={<Settings size={15} />} label="Settings" onClick={() => { setOpen(false); router.push("/admin/settings"); }} />
          <MenuRow icon={<LogOut size={15} />} label="Sign out" onClick={logout} />
        </div>
      )}
    </div>
  );
}

function MenuRow({ icon, label, onClick }: { icon: React.ReactNode; label: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      style={{
        display: "flex", alignItems: "center", gap: 10, width: "100%", padding: "9px 14px",
        border: "none", background: "transparent", cursor: "pointer", color: K.ink3,
        fontSize: 13, fontFamily: sans, textAlign: "left",
      }}
    >
      <span style={{ color: K.muted2, display: "flex" }}>{icon}</span>
      {label}
    </button>
  );
}
