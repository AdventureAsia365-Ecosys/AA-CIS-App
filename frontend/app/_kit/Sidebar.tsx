"use client";
// app/_kit/Sidebar.tsx
// AA-752 — the admin shell sidebar. Navy panel in both themes (--aa-side-* tokens). The logo is a
// link to /admin/overview; the nav sections/groups/items/roles come from adminNav.tsx.
// Collapse/expand via the chevron button and the `[` key (ignored while typing); collapsed = a
// 68px icon rail with tooltips (title + aria-label). The active item gets a gold rail + tinted bg.
// The Review Queue item shows the pending count (useReviewPendingCount: one query shared with the
// BottomNav, 180 s; badge hidden on error). Collapsed state is read via useSyncExternalStore (useSidebarCollapsed) — no
// setState-in-effect, no flash.
//
// Collapse presentation is CSS-driven off `data-collapsed` on the <aside> (the labels/group titles
// are always in the DOM, hidden by the `.aa-sidebar-label` / `.aa-sidebar-group-title` rules in
// globals.css). This lets the phone drawer (always `data-collapsed="false"` under the media query)
// render full labels regardless of the desktop collapsed state.
//
// AA-883 (S241 admin console) — the nav is split into two sections, Platform and CIS Content
// (adminNav.tsx NAV_SECTIONS). Each section has a header that folds it (state in useNavSections,
// localStorage, useSyncExternalStore). Headers render only when the role sees both sections, so
// reviewer/content keep the flat CIS list. In the collapsed icon rail (desktop only: the phone drawer
// is always the full panel) the headers become a plain divider and folded sections still show their
// icons. Below the nav, an "Apps" block links to the other apps from lib/apps.ts (new tab each), with
// one icon per app so the collapsed rail tells them apart.

import { useEffect } from "react";
import { useRouter, usePathname } from "next/navigation";
import { ArrowUpRight, CalendarCheck, ChevronDown, ChevronRight, ChevronsLeft, ChevronsRight, Map as MapIcon } from "lucide-react";
import { A, alpha, serif, sans } from "../admin/_components/adminUi";
import { LOGO_SRC } from "../_brand/tokens";
import {
  SETTINGS_ENTRY, navActive, sectionGroups, visibleSections,
  type NavEntry, type NavGroup, type NavSection,
} from "./adminNav";
import { useNavSections } from "./useNavSections";
import { APP_LINKS, type AppId, type AppLink } from "@/lib/apps";
import { useSidebarCollapsed } from "./useSidebar";
import { useHydrated } from "./useHydrated";
import { canSeePage } from "@/lib/rbac";
import { useAdminIdentity } from "./useAdminIdentity";
import { useReviewPendingCount } from "./useReviewPendingCount";

export function Sidebar({ onNavigate, drawerOpen = false }: { onNavigate?: () => void; drawerOpen?: boolean }) {
  const router = useRouter();
  const pathname = usePathname();
  const { role } = useAdminIdentity();
  const [collapsed, setCollapsed] = useSidebarCollapsed();
  // AA-834 — e2e hook: `data-hydrated="true"` appears only once the toggle handlers are live.
  const hydrated = useHydrated();

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

  // Review Queue pending count — shared with the BottomNav (one key, one request).
  const pending = useReviewPendingCount();

  // AA-883 — sections the role sees; headers only when there is more than one.
  const sections = visibleSections(role);
  const showHeaders = sections.length > 1;
  const [isClosed, toggleSection] = useNavSections();
  // The 68px icon rail: desktop collapsed. The phone drawer always shows the full panel (globals.css),
  // whatever the stored desktop state, so folding works there as on the expanded desktop sidebar.
  const rail = collapsed && !drawerOpen;

  function go(href: string) {
    router.push(href);
    onNavigate?.();
  }

  return (
    <aside
      className={`aa-admin-sidebar aa-shell-sidebar${drawerOpen ? " aa-admin-sidebar--open" : ""}`}
      data-testid="admin-sidebar"
      data-collapsed={collapsed ? "true" : "false"}
      data-hydrated={hydrated ? "true" : undefined}
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
          data-testid="sidebar-logo"
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

      {/* Nav — AA-883: Platform / CIS Content sections */}
      <nav style={{ flex: 1, display: "flex", flexDirection: "column", gap: 22 }}>
        {sections.map((section) => {
          const groups = sectionGroups(section.id)
            .map((g) => ({ ...g, items: g.items.filter((n) => canSeePage(role, n.href)) }))
            .filter((g) => g.items.length > 0);
          // The icon rail has no header label to unfold with, so a folded section still shows there.
          const closed = showHeaders && isClosed(section.id) && !rail;
          const hasActive = groups.some((g) => g.items.some((n) => navActive(n, pathname)));
          return (
            <div key={section.id} data-testid={`nav-section-${section.id}`}
              style={{ display: "flex", flexDirection: "column" }}>
              {showHeaders && (rail ? (
                <div role="separator" aria-label={section.label}
                  data-testid={`nav-section-divider-${section.id}`}
                  style={{ borderTop: "1px solid rgba(255,255,255,0.07)", margin: "0 6px" }} />
              ) : (
                <SectionHeader section={section} closed={closed} hasActive={hasActive}
                  onToggle={() => toggleSection(section.id)} />
              ))}
              {!closed && groups.map((group, i) => (
                <NavGroupBlock key={group.label} group={group} pathname={pathname} pending={pending} go={go}
                  spaceAbove={groupSpaceAbove(group, i, showHeaders, rail)} />
              ))}
            </div>
          );
        })}
      </nav>

      {/* AA-883 — the other apps of the ecosystem (lib/apps.ts), each in a new tab. */}
      <div data-testid="sidebar-apps" style={{ display: "flex", flexDirection: "column", gap: 1 }}>
        <div className="aa-sidebar-group-title" style={groupTitleStyle}>Apps</div>
        {APP_LINKS.map((a) => <AppLinkItem key={a.id} app={a} />)}
      </div>

      {/* Settings + collapse toggle */}
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <NavItem
          entry={SETTINGS_ENTRY}
          active={navActive(SETTINGS_ENTRY, pathname)}
          onClick={() => go(SETTINGS_ENTRY.href)}
        />
        <button
          onClick={() => setCollapsed(!collapsed)}
          data-testid="sidebar-collapse"
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

const groupTitleStyle: React.CSSProperties = {
  fontSize: 9.5, textTransform: "uppercase", letterSpacing: "0.16em",
  color: "var(--aa-side-text3)", padding: "0 10px 8px", fontWeight: 600,
};

// A one-item group named like its item (Overview, Tenants, Jobs) needs no title over it.
function groupHasTitle(group: NavGroup): boolean {
  return !(group.items.length === 1 && group.items[0].label === group.label);
}

// Vertical rhythm (S241 UI verify: untitled one-item groups sat ~51px apart vs 37px inside a group).
// A titled group keeps the 16px break above its title; an untitled one only gets a 6px nudge, so
// Staff → Tenants → Jobs read as rows (≈42px pitch), not as islands. The first group keeps 16px under a
// section header (0 with no header). In the icon rail the titles are hidden, so every group gets 16.
function groupSpaceAbove(group: NavGroup, index: number, showHeaders: boolean, rail: boolean): number {
  if (index === 0) return showHeaders ? 16 : 0;
  return rail || groupHasTitle(group) ? 16 : 6;
}

function NavGroupBlock({ group, pathname, pending, go, spaceAbove }: {
  group: NavGroup; pathname: string; pending: number | null | undefined; go: (href: string) => void;
  spaceAbove: number;
}) {
  const showTitle = groupHasTitle(group);
  return (
    <div style={{ marginTop: spaceAbove }}>
      {showTitle && <div className="aa-sidebar-group-title" style={groupTitleStyle}>{group.label}</div>}
      <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
        {group.items.map((n) => (
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
}

function SectionHeader({ section, closed, hasActive, onToggle }: {
  section: NavSection; closed: boolean; hasActive: boolean; onToggle: () => void;
}) {
  const color = closed && hasActive ? A.gold : "var(--aa-side-title)";
  return (
    <button
      onClick={onToggle}
      aria-expanded={!closed}
      data-testid={`nav-section-toggle-${section.id}`}
      title={closed ? `Show ${section.label}` : `Hide ${section.label}`}
      style={{
        display: "flex", alignItems: "center", gap: 8, width: "100%",
        padding: "6px 10px 6px", border: "none", borderTop: "1px solid rgba(255,255,255,0.07)",
        background: "transparent", cursor: "pointer", fontFamily: sans, color,
        textAlign: "left" as const,
      }}
    >
      <span className="aa-sidebar-label" style={{
        flex: 1, minWidth: 0, fontSize: 10.5, fontWeight: 700, letterSpacing: "0.14em",
        textTransform: "uppercase" as const, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
      }}>
        {section.label}
      </span>
      <span className="aa-sidebar-label" style={{ display: "flex", flexShrink: 0, opacity: 0.8 }}>
        {closed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
      </span>
    </button>
  );
}

const APP_ICON: Record<AppId, React.ReactNode> = {
  booking: <CalendarCheck size={15} />,
  tripplanner: <MapIcon size={15} />,
};

function AppLinkItem({ app }: { app: AppLink }) {
  return (
    <a
      href={app.href}
      target={app.external ? "_blank" : undefined}
      rel={app.external ? "noopener noreferrer" : undefined}
      title={app.hint}
      aria-label={`${app.label} (opens in a new tab)`}
      data-testid={`sidebar-app-${app.id}`}
      style={{
        display: "flex", alignItems: "center", gap: 10, width: "100%",
        padding: "7px 10px", borderRadius: 7, textDecoration: "none",
        color: "var(--aa-side-text2)", fontSize: 12.5, fontWeight: 500, fontFamily: sans,
      }}
    >
      <span style={{ flexShrink: 0, opacity: 0.75, display: "flex" }}>{APP_ICON[app.id]}</span>
      <span className="aa-sidebar-label" style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        {app.label}
      </span>
      {app.external && (
        <span className="aa-sidebar-label" aria-hidden="true" style={{ flexShrink: 0, opacity: 0.6 }}>
          <ArrowUpRight size={12} />
        </span>
      )}
    </a>
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
