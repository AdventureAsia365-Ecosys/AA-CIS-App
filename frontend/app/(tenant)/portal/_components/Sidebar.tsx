"use client";
// app/(tenant)/portal/_components/Sidebar.tsx — v3 (AA-430)
//
// Was tab-state (onClick={() => setTab(id)}, active = tab === id). Portal is now real
// routes per T-stage (see portal/layout.tsx) — nav is <Link> + active state comes from
// usePathname(), not a prop passed down from a single page.tsx anymore.

import Link from "next/link";
import { usePathname } from "next/navigation";
import { LayoutDashboard, Globe2, BookOpen, Sparkles, Code2, Store, CalendarRange, Eye, Send, LogOut } from "lucide-react";
import { T, serif, sans } from "./ui";
import { LOGO_SRC } from "../../../_brand/tokens";

interface Props {
  poolCount: number;
  catalogCount: number;
  tenantName: string;
  planTier: string;
  // AA-430: old setTab({...}) => setGlobalSearch("") behavior on any nav click — Sidebar
  // no longer owns navigation itself (Link does), so the search-clear side effect is
  // surfaced as a plain onClick alongside the Link instead.
  onNavClick?: () => void;
}

const NAV1: { href: string; icon: React.ReactNode; label: string }[] = [
  { href: "/portal/dashboard",  icon: <LayoutDashboard size={15} />, label: "Dashboard" },
  { href: "/portal/t1-rewrite", icon: <Globe2 size={15} />,          label: "Browse Tours" }, // AA-576 Phần 3 (was "Browse Pool" — "Pool" read as internal jargon)
  { href: "/portal/t4-pool",    icon: <BookOpen size={15} />,        label: "My Catalog Tours" }, // AA-576 Phần 3 (was "My Catalog" — disambiguate from "My Content" below)
  { href: "/portal/t0-brand",   icon: <Sparkles size={15} />,        label: "Brand Identity" },
  // AA-526 — Atom Curation (T6) removed from the tenant portal entirely: atoms are backend-only
  // now (owner_scope='platform', curated by AA-admin — see AA-527), tenants never see them.
  { href: "/portal/t7-planning", icon: <CalendarRange size={15} />,  label: "Social Content" }, // AA-448, relabeled AA-519 Việc 3 (Slate replaced the old Quarter Plan UI), renamed again AA-564 4.1 (Tenant Portal only — Admin still calls this "Slate", AA-563/564 Group 2); kept as-is AA-576 Phần 3 (Nghiệp's call)
  // AA-576 Phần 3 — "Write Content" nav item removed (Nghiệp's call): AngleGateWizard is reached
  // inline from a Social Content Subject row since AA-564 4.2, so this standalone menu entry was
  // redundant. Route/page/AngleGateTab.tsx deliberately left untouched — still a real, valid
  // deep-link (e.g. a bookmarked resume link), not deleted, just no longer in the menu.
  { href: "/portal/t10-review", icon: <Eye size={15} />,             label: "My Content" }, // AA-576 Phần 3 (was "Review" — ambiguous next to "My Catalog Tours")
  { href: "/portal/t11-publish", icon: <Send size={15} />,           label: "Publish" }, // AA-457/AA-458 — WordPress credentials + real publish
  { href: "/portal/marketplace", icon: <Store size={15} />,          label: "Marketplace" }, // AA-444
  { href: "/portal/api",        icon: <Code2 size={15} />,           label: "API Access" },
];

const NAV2: { href: string; label: string }[] = [
  { href: "/portal/activity", label: "Activity Log" },
  { href: "/portal/billing",  label: "Billing" },
  { href: "/portal/settings", label: "Settings" },
];

export default function Sidebar({
  poolCount, catalogCount, tenantName, planTier, onNavClick,
}: Props) {
  const pathname = usePathname();
  const initials = tenantName.split(" ").map(w => w[0]).join("").slice(0, 2).toUpperCase();

  async function logout() {
    // AA-427: the tenant cookies are httpOnly now — JS can no longer clear
    // them by writing "<name>=; max-age=0" itself. Clear server-side instead.
    try {
      await fetch("/api/auth/tenant-logout", { method: "POST" });
    } catch {
      // ignore — redirect below either way; middleware re-verifies the JWT
      // on the next request regardless of whether the clear succeeded.
    }
    window.location.href = "/tenant-login";
  }

  const counts: Record<string, number> = {
    "/portal/t1-rewrite": poolCount,
    "/portal/t4-pool":    catalogCount,
  };

  return (
    <aside style={{
      width: 236, flexShrink: 0, background: T.ink, color: "#C9CFD8", // AA-605: 220 -> 236, same as admin (wider Poppins)
      padding: "22px 14px 24px", display: "flex", flexDirection: "column",
      gap: 28, position: "sticky", top: 0, height: "100vh", overflowY: "auto",
    }}>
      {/* Brand */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, paddingBottom: 18, borderBottom: "1px solid rgba(255,255,255,0.07)" }}>
        {/* AA-605 — real Adventure Asia mountain mark, replacing the gold gradient "A" tile. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={LOGO_SRC} alt="Adventure Asia" width={38} height={24}
          style={{ width: 38, height: "auto", flexShrink: 0, display: "block" }} />
        <div>
          <div style={{ fontFamily: serif, fontSize: 14, fontWeight: 500, color: "#F4F1EC", letterSpacing: "-0.01em", lineHeight: 1.15 }}>
            Adventure Asia
          </div>
          <div style={{ fontSize: 9.5, textTransform: "uppercase", letterSpacing: "0.18em", color: T.gold, fontWeight: 600, marginTop: 2 }}>
            CIS Platform
          </div>
        </div>
      </div>

      {/* Nav */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 26 }}>
        <NavGroup label="Workspace">
          {NAV1.map(n => (
            <NavItem key={n.href} href={n.href} active={pathname === n.href} icon={n.icon} label={n.label}
              count={counts[n.href]} onClick={onNavClick} />
          ))}
        </NavGroup>
        <NavGroup label="Account">
          {NAV2.map(n => (
            <NavItem key={n.href} href={n.href} active={pathname === n.href} icon={null} label={n.label}
              onClick={onNavClick} />
          ))}
        </NavGroup>
      </div>

      {/* Footer */}
      <div style={{ paddingTop: 14, borderTop: "1px solid rgba(255,255,255,0.07)" }}>
        <div style={{
          display: "flex", alignItems: "center", gap: 9,
          padding: 8, borderRadius: 8, background: "rgba(255,255,255,0.03)",
        }}>
          <div style={{
            width: 30, height: 30, borderRadius: 6, background: "#3A4453",
            display: "grid", placeItems: "center",
            color: T.goldSoft, fontWeight: 600, fontSize: 11, flexShrink: 0,
          }}>{initials}</div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ color: "#F4F1EC", fontSize: 12, fontWeight: 600, lineHeight: 1.2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {tenantName}
            </div>
            <div style={{ color: "#8A929D", fontSize: 10.5, lineHeight: 1.3 }}>
              {planTier.charAt(0).toUpperCase() + planTier.slice(1)} Plan
            </div>
          </div>
          <button onClick={logout} title="Sign out"
            style={{ background: "none", border: "none", cursor: "pointer", color: "#8A929D", padding: 2, display: "flex" }}>
            <LogOut size={12} />
          </button>
        </div>
      </div>
    </aside>
  );
}

function NavGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 9.5, textTransform: "uppercase", letterSpacing: "0.16em", color: "#6E7681", padding: "0 10px 8px", fontWeight: 600 }}>
        {label}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>{children}</div>
    </div>
  );
}

function NavItem({ href, active, icon, label, count, onClick }: {
  href: string; active: boolean; icon: React.ReactNode; label: string;
  count?: number; onClick?: () => void;
}) {
  return (
    <Link href={href} onClick={onClick} style={{
      display: "flex", alignItems: "center", gap: 10, width: "100%",
      padding: "8px 10px", borderRadius: 7, border: "none",
      background: active ? "rgba(219,150,40,0.12)" : "transparent",
      color: active ? "#fff" : "#C9CFD8",
      fontSize: 13, fontWeight: 500, cursor: "pointer",
      textAlign: "left", fontFamily: sans, position: "relative",
      transition: "background .15s, color .15s",
      textDecoration: "none",
    }}>
      {active && (
        <span style={{ position: "absolute", left: 0, top: 8, bottom: 8, width: 2, background: T.gold, borderRadius: "0 2px 2px 0" }} />
      )}
      {icon && <span style={{ flexShrink: 0, opacity: active ? 1 : 0.75 }}>{icon}</span>}
      <span style={{ flex: 1 }}>{label}</span>
      {count != null && count > 0 && (
        <span style={{ fontSize: 11, color: active ? T.gold : "#8A929D", fontVariantNumeric: "tabular-nums" }}>
          {count}
        </span>
      )}
    </Link>
  );
}
