"use client";
// app/_kit/Sidebar.tsx
// AA-752 — the admin shell sidebar. Navy panel in both themes (--aa-side-* tokens). The logo is a
// link to /admin/overview; the nav groups/items/roles come from adminNav.tsx.
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
// AA-883 (S241 admin console) — below the nav, an "Apps" block links to the other apps from lib/apps.ts (new tab each),
// with one icon per app so the collapsed rail tells them apart.
//
// S264 — the titled groups fold through the kit NavGroup + useNavGroups (core, scope "cis-admin"): folded unless the
// user opened them, the group holding the current page always open on arrival. A folded group shows the Review Queue
// count on its header. One-item groups named like their item have no header and stay open. The icon rail shows every
// item, folded or not.
//
// AA-1039 R6 slice 1 (S266, root ADR 0019 d13) — seven groups (Home, Tenants, AA Catalogue, Tenant content QA,
// AI & spend, Writing Lab, Platform) from lib/adminIa.ts; the two foldable sections of S241 (Platform / CIS Content)
// and the footer Settings entry are gone (Settings is in AA Catalogue). Hrefs nest (/admin/tenants and
// /admin/tenants/agencies), so the active item is the longest match (adminNav isActiveEntry), never two at once.
//
// AA-1037 (root ADR 0019 d9): the Trade group's items are pages of Booking admin; they open in the same tab (adminNav
// followExternal, a full page load) with the arrow mark of an outside link, and are never the active item.

import { useEffect } from "react";
import { useRouter, usePathname } from "next/navigation";
import { ArrowUpRight, CalendarCheck, ChevronsLeft, ChevronsRight, Map as MapIcon } from "lucide-react";
import { LOGO_SRC } from "./core/brand";
import { activeGroup, followExternal, isActiveEntry, visibleGroups, type NavEntry, type NavGroup } from "./adminNav";
import { APP_LINKS, type AppId, type AppLink } from "@/lib/apps";
import { useNavGroups, useSidebarCollapsed } from "./core/useSidebar";
import { NavGroup as KitNavGroup } from "./core/NavGroup";
import { navGroupId } from "./core/sidebarStore";
import { useHydrated } from "./core/useHydrated";
import { useAdminIdentity } from "./useAdminIdentity";
import { useReviewPendingCount } from "./useReviewPendingCount";
import { useNavLabel } from "./adminNav";
import { useT } from "./core/useI18n";
import { SHELL_MESSAGES } from "./shell.messages";
import { K, alpha, sans, serif } from "./core/tokens";

export function Sidebar({ onNavigate, drawerOpen = false }: { onNavigate?: () => void; drawerOpen?: boolean }) {
  const router = useRouter();
  const pathname = usePathname();
  const { role } = useAdminIdentity();
  const [collapsed, setCollapsed] = useSidebarCollapsed();
  // AA-834 — e2e hook: `data-hydrated="true"` appears only once the toggle handlers are live.
  const hydrated = useHydrated();
  const t = useT(SHELL_MESSAGES);

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

  // The 68px icon rail: desktop collapsed. The phone drawer always shows the full panel (globals.css),
  // whatever the stored desktop state, so folding works there as on the expanded desktop sidebar.
  const rail = collapsed && !drawerOpen;

  // The role's groups, and the group holding the current page (always open on arrival).
  const groups = visibleGroups(role);
  const current = activeGroup(pathname);
  const navGroups = useNavGroups("cis-admin", current ? navGroupId(current.label) : null, pathname);

  function go(href: string) {
    router.push(href);
    onNavigate?.();
  }

  // A nav entry: a CIS route through the router, another app's page (AA-1037 Trade) as a same-tab full load.
  function openEntry(entry: NavEntry) {
    if (!followExternal(entry)) router.push(entry.href);
    onNavigate?.();
  }

  return (
    <aside
      className="aa-admin-sidebar aa-shell-sidebar"
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
          title={t("logoTitle")}
          aria-label={t("logoAria")}
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
            <span style={{ display: "block", fontSize: 9.5, textTransform: "uppercase" as const, letterSpacing: "0.18em", color: K.accent, fontWeight: 600, marginTop: 1 }}>
              {t("adminSubtitle")}
            </span>
          </span>
        </button>
      </div>

      {/* Nav — AA-1039: the seven groups of lib/adminIa.ts */}
      <nav data-testid="admin-nav" style={{ flex: 1, display: "flex", flexDirection: "column" }}>
        {groups.map((group, i) => {
          const id = navGroupId(group.label);
          return (
            <NavGroupBlock key={group.label} group={group} pathname={pathname} pending={pending} go={openEntry}
              open={navGroups.isOpen(id)} onToggle={() => navGroups.toggle(id)} rail={rail}
              spaceAbove={groupSpaceAbove(group, groups[i - 1], rail)} />
          );
        })}
      </nav>

      {/* AA-883 — the other apps of the ecosystem (lib/apps.ts), each in a new tab. */}
      <div data-testid="sidebar-apps" style={{ display: "flex", flexDirection: "column", gap: 1 }}>
        <div className="aa-sidebar-group-title" style={groupTitleStyle}>{t("appsTitle")}</div>
        {APP_LINKS.map((a) => <AppLinkItem key={a.id} app={a} />)}
      </div>

      {/* Collapse toggle */}
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <button
          onClick={() => setCollapsed(!collapsed)}
          data-testid="sidebar-collapse"
          title={collapsed ? t("expandSidebarKey") : t("collapseSidebarKey")}
          aria-label={collapsed ? t("expandSidebar") : t("collapseSidebar")}
          style={{
            display: "flex", alignItems: "center", gap: 10, width: "100%",
            padding: "8px 10px", borderRadius: 7, border: "none",
            background: "transparent", color: "var(--aa-side-text2)", cursor: "pointer",
            fontSize: 12, fontFamily: sans,
          }}
        >
          {collapsed ? <ChevronsRight size={16} /> : <ChevronsLeft size={16} />}
          <span className="aa-sidebar-label">{t("collapse")}</span>
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
// Only two untitled groups in a row close up to a 6px nudge, so they read as rows (≈42px pitch), not as islands.
// Any group next to a TITLED group keeps the full 16px break, so it never reads as that group's child. The first
// group sits right under the brand block. In the icon rail the titles are hidden, so every group gets 16.
function groupSpaceAbove(group: NavGroup, prev: NavGroup | undefined, rail: boolean): number {
  if (!prev) return 0;
  if (rail) return 16;
  return groupHasTitle(prev) || groupHasTitle(group) ? 16 : 6;
}

function NavGroupBlock({ group, pathname, pending, go, spaceAbove, open, onToggle, rail }: {
  group: NavGroup; pathname: string; pending: number | null | undefined; go: (entry: NavEntry) => void;
  spaceAbove: number; open: boolean; onToggle: () => void; rail: boolean;
}) {
  const navLabel = useNavLabel();
  const badgeOf = (n: NavEntry) => (n.href === "/admin/review" && pending ? pending : undefined);
  const total = group.items.reduce((sum, n) => sum + (badgeOf(n) ?? 0), 0);
  return (
    <KitNavGroup
      id={navGroupId(group.label)}
      label={groupHasTitle(group) ? navLabel(group.label) : null}
      open={open}
      onToggle={onToggle}
      rail={rail}
      active={group.items.some((n) => isActiveEntry(n, pathname))}
      badge={total || null}
      style={{ marginTop: spaceAbove }}
    >
      {group.items.map((n) => (
        <NavItem
          key={n.href}
          entry={n}
          active={isActiveEntry(n, pathname)}
          badge={badgeOf(n)}
          onClick={() => go(n)}
        />
      ))}
    </KitNavGroup>
  );
}

const APP_ICON: Record<AppId, React.ReactNode> = {
  booking: <CalendarCheck size={15} />,
  tripplanner: <MapIcon size={15} />,
};

const APP_TEXT: Record<AppId, { label: "appBookingLabel" | "appTripplannerLabel"; hint: "appBookingHint" | "appTripplannerHint" }> = {
  booking: { label: "appBookingLabel", hint: "appBookingHint" },
  tripplanner: { label: "appTripplannerLabel", hint: "appTripplannerHint" },
};

function AppLinkItem({ app }: { app: AppLink }) {
  const t = useT(SHELL_MESSAGES);
  const text = APP_TEXT[app.id];
  const label = text ? t(text.label) : app.label;
  return (
    <a
      href={app.href}
      target={app.external ? "_blank" : undefined}
      rel={app.external ? "noopener noreferrer" : undefined}
      title={text ? t(text.hint) : app.hint}
      aria-label={t("opensNewTab", label)}
      data-testid={`sidebar-app-${app.id}`}
      style={{
        display: "flex", alignItems: "center", gap: 10, width: "100%",
        padding: "7px 10px", borderRadius: 7, textDecoration: "none",
        color: "var(--aa-side-text2)", fontSize: 12.5, fontWeight: 500, fontFamily: sans,
      }}
    >
      <span style={{ flexShrink: 0, opacity: 0.75, display: "flex" }}>{APP_ICON[app.id]}</span>
      <span className="aa-sidebar-label" style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        {label}
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
  const accent = K.accent;
  const label = useNavLabel()(entry.label);
  return (
    <button
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      data-external={entry.external ? "true" : undefined}
      data-href={entry.external ? entry.href : undefined}
      title={label}
      aria-label={label}
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
            background: K.accent, color: "var(--aa-on-accent)",
            borderRadius: 999, fontSize: 9, fontWeight: 700,
            minWidth: 15, height: 15, display: "none", placeItems: "center", padding: "0 3px",
          }}>{badge > 99 ? "99+" : badge}</span>
        )}
      </span>
      <span className="aa-sidebar-label" style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        {label}
      </span>
      {entry.external && (
        <span className="aa-sidebar-label" aria-hidden="true" style={{ flexShrink: 0, opacity: 0.6, display: "flex" }}>
          <ArrowUpRight size={12} />
        </span>
      )}
      {badge != null && (
        <span className="aa-sidebar-badge-pill" style={{
          background: K.accent, color: "var(--aa-on-accent)",
          borderRadius: 999, fontSize: 10, fontWeight: 700,
          minWidth: 18, height: 16, display: "grid", placeItems: "center",
          padding: "0 5px", flexShrink: 0,
        }}>{badge > 99 ? "99+" : badge}</span>
      )}
    </button>
  );
}
