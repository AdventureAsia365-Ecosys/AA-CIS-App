"use client";
// app/_kit/Sidebar.tsx
// AA-752 — the admin shell sidebar. Navy panel in both themes (--aa-side-* tokens). The logo is a
// link to /admin/overview; the nav groups/items/roles are the shared AA-663 IA (adminNav.tsx).
// Collapse/expand via the chevron button and the `[` key (ignored while typing); collapsed = a
// 68px icon rail with tooltips (title + aria-label). The active item gets a gold rail + tinted bg.
// The Review Queue item shows the pending count (review-queue total, cached ~60s; badge hidden on
// error). Collapsed state is read via useSyncExternalStore (useSidebarCollapsed) — no
// setState-in-effect, no flash.
//
// Collapse presentation is CSS-driven off `data-collapsed` on the <aside> (the labels/group titles
// are always in the DOM, hidden by the `.aa-sidebar-label` / `.aa-sidebar-group-title` rules in
// globals.css). This lets the phone drawer (always `data-collapsed="false"` under the media query)
// render full labels regardless of the desktop collapsed state.

import { useEffect } from "react";
import { useRouter, usePathname } from "next/navigation";
import { ChevronsLeft, ChevronsRight } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { A, alpha, serif, sans } from "../admin/_components/adminUi";
import { LOGO_SRC } from "../_brand/tokens";
import { NAV_GROUPS, SETTINGS_ENTRY, navActive, type NavEntry } from "./adminNav";
import { useSidebarCollapsed } from "./useSidebar";
import { useAdminIdentity } from "./useAdminIdentity";
import { apiGet } from "./api";

interface ReviewTotal {
  pagination?: { total?: number };
  total?: number;
}

export function Sidebar({ onNavigate, drawerOpen = false }: { onNavigate?: () => void; drawerOpen?: boolean }) {
  const router = useRouter();
  const pathname = usePathname();
  const { isAdmin } = useAdminIdentity();
  const [collapsed, setCollapsed] = useSidebarCollapsed();

  // `[` toggles collapse, ignored while typing in an input/textarea/select or contenteditable.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "[" || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      const tag = t?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || t?.isContentEditable) return;
      e.preventDefault();
      setCollapsed(!collapsed);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [collapsed, setCollapsed]);

  // Review Queue pending count — reuse the review-queue reader's `pagination.total`. Cached ~60s;
  // the badge is simply hidden on error (never a blocking failure for the shell).
  const { data: reviewTotal } = useQuery({
    queryKey: ["sidebar", "review-pending"],
    queryFn: () =>
      apiGet<ReviewTotal>("/api/admin/review-queue?status=pending&page=1&page_size=1", { admin: true }),
    staleTime: 60_000,
    refetchInterval: 60_000,
    retry: false,
  });
  const pending = reviewTotal?.pagination?.total ?? reviewTotal?.total;

  function go(href: string) {
    router.push(href);
    onNavigate?.();
  }

  return (
    <aside
      className={`aa-admin-sidebar aa-shell-sidebar${drawerOpen ? " aa-admin-sidebar--open" : ""}`}
      data-collapsed={collapsed ? "true" : "false"}
      style={{
        width: collapsed ? 68 : 236, flexShrink: 0,
        background: "var(--aa-side-bg)", color: "var(--aa-side-text)",
        padding: "22px 14px 24px",
        display: "flex", flexDirection: "column", gap: 24,
        height: "100vh", overflowY: "auto", overflowX: "hidden",
        transition: "width .18s ease",
      }}
    >
      {/* Brand → Overview */}
      <div style={{ paddingBottom: 16, borderBottom: "1px solid rgba(255,255,255,0.07)" }}>
        <button
          onClick={() => go("/admin/overview")}
          title="Adventure Asia — Overview"
          aria-label="Adventure Asia CIS Admin — go to Overview"
          style={{
            display: "flex", alignItems: "center", gap: 10, width: "100%",
            background: "none", border: "none", cursor: "pointer", padding: 0,
          }}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={LOGO_SRC} alt="Adventure Asia" width={38} height={24}
            style={{ width: 38, height: "auto", flexShrink: 0, display: "block" }} />
          <span className="aa-sidebar-label" style={{ flex: 1, minWidth: 0, textAlign: "left" }}>
            <span style={{ display: "block", fontFamily: serif, fontSize: 14, fontWeight: 500, color: "var(--aa-side-title)", letterSpacing: "-0.01em", lineHeight: 1.2 }}>
              Adventure Asia
            </span>
            <span style={{ display: "block", fontSize: 9.5, textTransform: "uppercase" as const, letterSpacing: "0.18em", color: A.accent, fontWeight: 600, marginTop: 1 }}>
              CIS Admin
            </span>
          </span>
        </button>
      </div>

      {/* Nav */}
      <nav style={{ flex: 1, display: "flex", flexDirection: "column", gap: 20 }}>
        {NAV_GROUPS.map((group) => {
          const items = group.items.filter((n) => isAdmin || !n.adminOnly);
          if (!items.length) return null;
          return (
            <div key={group.label}>
              <div className="aa-sidebar-group-title" style={{ fontSize: 9.5, textTransform: "uppercase" as const, letterSpacing: "0.16em", color: "var(--aa-side-text3)", padding: "0 10px 8px", fontWeight: 600 }}>
                {group.label}
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
                {items.map((n) => (
                  <NavItem
                    key={n.href}
                    entry={n}
                    active={navActive(n, pathname)}
                    badge={n.href === "/admin/review" && pending ? pending : undefined}
                    onClick={() => go(n.href)}
                  />
                ))}
              </div>
            </div>
          );
        })}
      </nav>

      {/* Settings + collapse toggle */}
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <NavItem
          entry={SETTINGS_ENTRY}
          active={navActive(SETTINGS_ENTRY, pathname)}
          onClick={() => go(SETTINGS_ENTRY.href)}
        />
        <button
          onClick={() => setCollapsed(!collapsed)}
          title={collapsed ? "Expand sidebar ([)" : "Collapse sidebar ([)"}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          style={{
            display: "flex", alignItems: "center", gap: 10, width: "100%",
            padding: "8px 10px", borderRadius: 7, border: "none",
            background: "transparent", color: "var(--aa-side-text2)", cursor: "pointer",
            fontSize: 12, fontFamily: sans,
          }}
        >
          {collapsed ? <ChevronsRight size={16} /> : <ChevronsLeft size={16} />}
          <span className="aa-sidebar-label">Collapse</span>
        </button>
      </div>
    </aside>
  );
}

function NavItem({ entry, active, badge, onClick }: {
  entry: NavEntry; active: boolean; badge?: number; onClick: () => void;
}) {
  const accent = A.gold;
  return (
    <button
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      title={entry.label}
      aria-label={entry.label}
      style={{
        display: "flex", alignItems: "center", gap: 10, width: "100%",
        padding: "8px 10px", borderRadius: 7, border: "none",
        background: active ? alpha(accent, 9) : "transparent",
        color: active ? "var(--aa-on-solid)" : "var(--aa-side-text)",
        fontSize: 13, fontWeight: 500, cursor: "pointer",
        textAlign: "left" as const, fontFamily: sans, position: "relative",
        transition: "background .15s, color .15s",
      }}
    >
      {active && (
        <span style={{ position: "absolute", left: 0, top: 8, bottom: 8, width: 2, background: accent, borderRadius: "0 2px 2px 0" }} />
      )}
      <span style={{ flexShrink: 0, opacity: active ? 1 : 0.75, position: "relative", display: "flex" }}>
        {entry.icon}
        {/* Collapsed badge — a small dot on the icon (shown only when the rail is collapsed). */}
        {badge != null && (
          <span className="aa-sidebar-badge-dot" style={{
            position: "absolute", top: -4, right: -6,
            background: A.accent, color: "var(--aa-on-accent)",
            borderRadius: 999, fontSize: 9, fontWeight: 700,
            minWidth: 15, height: 15, display: "none", placeItems: "center", padding: "0 3px",
          }}>{badge > 99 ? "99+" : badge}</span>
        )}
      </span>
      <span className="aa-sidebar-label" style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        {entry.label}
      </span>
      {badge != null && (
        <span className="aa-sidebar-badge-pill" style={{
          background: A.accent, color: "var(--aa-on-accent)",
          borderRadius: 999, fontSize: 10, fontWeight: 700,
          minWidth: 18, height: 16, display: "grid", placeItems: "center",
          padding: "0 5px", flexShrink: 0,
        }}>{badge > 99 ? "99+" : badge}</span>
      )}
    </button>
  );
}
