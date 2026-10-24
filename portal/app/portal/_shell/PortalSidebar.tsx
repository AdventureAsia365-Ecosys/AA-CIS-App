"use client";
// portal/app/portal/_shell/PortalSidebar.tsx
// AA-759 — the portal sidebar: navy panel in both themes (--aa-side-* tokens), the nav grouped by
// workflow (portalNav.tsx NAV_GROUPS), a collapse rail (chevron button or the `[` key, ignored while
// typing; state in the vendored sidebarStore, restored before paint by the root layout script).
//
// Presentation is CSS-driven (globals.css `data-pshell` rules): the width, the collapsed rail
// (labels and group titles hidden, a dot instead of the count pill) and the phone drawer (fixed,
// off-canvas, `data-open`). Nothing here sets width or position inline, so the CSS always wins.

import { useEffect } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ChevronsLeft, ChevronsRight, ExternalLink, Users } from "lucide-react";
import { alpha, sans, serif, useHydrated, useSidebarCollapsed } from "@/app/_kit";
import { LOGO_SRC } from "@/app/_brand/tokens";
import { NAV_GROUPS, ROUTES, navActive, type NavEntry } from "./portalNav";

// AA-935 (CRM-07, design v3 Fix C10): the cross-link to the agency CRM (AA-Partner web on its own host) until the
// Content studio moves into the CRM shell (AA-979). Outside NAV_GROUPS on purpose: it is another app, so neither
// the ⌘K palette nor the phone bottom bar routes to it. The CRM has its own sign-in until then (design-final §4.4).
// NEXT_PUBLIC_CRM_URL (Vercel aa-cis-portal) overrides the Dev host.
const CRM_URL = process.env.NEXT_PUBLIC_CRM_URL || "https://partner-dev.lumiguides.it.com";

export function PortalSidebar({ drawerOpen, poolCount, catalogCount, onNavigate }: {
  drawerOpen: boolean;
  poolCount: number | null;
  catalogCount: number;
  onNavigate: () => void;
}) {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useSidebarCollapsed();
  const hydrated = useHydrated();

  // `[` toggles the rail, ignored while typing (input / textarea / select / contenteditable).
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

  const counts = { pool: poolCount, catalog: catalogCount };

  return (
    <aside
      data-pshell="sidebar"
      data-testid="portal-sidebar"
      data-open={drawerOpen ? "true" : "false"}
      data-hydrated={hydrated ? "true" : undefined}
      aria-label="Portal navigation"
      style={{
        flexShrink: 0, background: "var(--aa-side-bg)", color: "var(--aa-side-text)",
        display: "flex", flexDirection: "column", gap: 22,
        overflowY: "auto", overflowX: "hidden", fontFamily: sans,
      }}
    >
      {/* Brand → Dashboard */}
      <div style={{ paddingBottom: 16, borderBottom: "1px solid rgba(255,255,255,0.07)" }}>
        <Link
          href={ROUTES.dashboard}
          onClick={onNavigate}
          data-pshell-row="true"
          aria-label="Adventure Asia Partner Portal — go to Dashboard"
          title="Adventure Asia — Dashboard"
          style={{ display: "flex", alignItems: "center", gap: 10, textDecoration: "none" }}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={LOGO_SRC} alt="" width={38} height={24}
            style={{ width: 38, height: "auto", flexShrink: 0, display: "block" }} />
          <span data-pshell-label="true" style={{ minWidth: 0 }}>
            <span style={{ display: "block", fontFamily: serif, fontSize: 14, fontWeight: 500, color: "var(--aa-side-title)", letterSpacing: "-0.01em", lineHeight: 1.2 }}>
              Adventure Asia
            </span>
            <span style={{ display: "block", fontSize: 9.5, textTransform: "uppercase", letterSpacing: "0.18em", color: "var(--aa-accent)", fontWeight: 600, marginTop: 1 }}>
              Partner Portal
            </span>
          </span>
        </Link>
      </div>

      <nav aria-label="Primary" style={{ flex: 1, display: "flex", flexDirection: "column", gap: 16 }}>
        {NAV_GROUPS.map((group) => (
          <div key={group.label} data-testid={`portal-nav-group-${group.label.toLowerCase()}`}>
            {group.label !== "Overview" && (
              <div data-pshell-label="true" style={groupTitleStyle}>{group.label}</div>
            )}
            <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
              {group.items.map((entry) => (
                <NavItem
                  key={entry.href}
                  entry={entry}
                  active={navActive(entry, pathname)}
                  count={entry.count ? (counts[entry.count] ?? undefined) : undefined}
                  onClick={onNavigate}
                />
              ))}
            </div>
          </div>
        ))}
        <div data-testid="portal-nav-group-crm">
          <div data-pshell-label="true" style={groupTitleStyle}>Partner CRM</div>
          <a
            href={`${CRM_URL}/`}
            data-pshell-row="true"
            data-testid="portal-crm-link"
            title="Open the Partner CRM"
            style={{
              display: "flex", alignItems: "center", gap: 10, width: "100%",
              padding: "8px 10px", borderRadius: 7, textDecoration: "none",
              color: "var(--aa-side-text)", fontSize: 13, fontWeight: 500, fontFamily: sans,
            }}
          >
            <span aria-hidden style={{ flexShrink: 0, opacity: 0.75, display: "flex" }}><Users size={16} /></span>
            <span data-pshell-label="true" style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              Open CRM
            </span>
            <span data-pshell-label="true" aria-hidden style={{ display: "flex", opacity: 0.6 }}><ExternalLink size={12} /></span>
          </a>
        </div>
      </nav>

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <button
          data-pshell="collapse-btn"
          data-pshell-row="true"
          onClick={() => setCollapsed(!collapsed)}
          data-testid="portal-sidebar-collapse"
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
          <span data-pshell-label="true">Collapse</span>
        </button>
      </div>
    </aside>
  );
}

const groupTitleStyle: React.CSSProperties = {
  fontSize: 9.5, textTransform: "uppercase", letterSpacing: "0.16em",
  color: "var(--aa-side-text3)", padding: "0 10px 6px", fontWeight: 600,
};

function NavItem({ entry, active, count, onClick }: {
  entry: NavEntry; active: boolean; count?: number; onClick: () => void;
}) {
  const showCount = count != null && count > 0;
  const shown = showCount ? (count > 999 ? "999+" : count.toLocaleString()) : "";
  return (
    <Link
      href={entry.href}
      onClick={onClick}
      data-pshell-row="true"
      aria-current={active ? "page" : undefined}
      title={entry.label}
      style={{
        display: "flex", alignItems: "center", gap: 10, width: "100%",
        padding: "8px 10px", borderRadius: 7, textDecoration: "none",
        background: active ? alpha("var(--aa-accent)", 12) : "transparent",
        color: active ? "var(--aa-on-solid)" : "var(--aa-side-text)",
        fontSize: 13, fontWeight: 500, fontFamily: sans, position: "relative",
        transition: "background .15s, color .15s",
      }}
    >
      {active && (
        <span aria-hidden style={{ position: "absolute", left: 0, top: 8, bottom: 8, width: 2, background: "var(--aa-accent)", borderRadius: "0 2px 2px 0" }} />
      )}
      <span aria-hidden style={{ flexShrink: 0, opacity: active ? 1 : 0.75, position: "relative", display: "flex" }}>
        {entry.icon}
        {showCount && (
          <span data-pshell-dot="true" style={{
            position: "absolute", top: -4, right: -6, width: 7, height: 7, borderRadius: 999,
            background: "var(--aa-accent)",
          }} />
        )}
      </span>
      <span data-pshell-label="true" style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        {entry.label}
      </span>
      {showCount && (
        <span data-pshell-label="true" aria-label={`${count} ${entry.count === "pool" ? "tours available" : "tours"}`}
          style={{ fontSize: 11, color: active ? "var(--aa-accent)" : "var(--aa-side-text2)", fontVariantNumeric: "tabular-nums", flexShrink: 0 }}>
          {shown}
        </span>
      )}
    </Link>
  );
}
