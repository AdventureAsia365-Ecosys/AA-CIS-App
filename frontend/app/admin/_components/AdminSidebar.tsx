"use client";
// app/admin/_components/AdminSidebar.tsx

import { useEffect, useState, useCallback } from "react";
import { useRouter, usePathname } from "next/navigation";
import { LayoutDashboard, Users, Upload, Wand2, ClipboardList, Palette, Library, LogOut, Bell, Settings, Wallet, Puzzle } from "lucide-react";
import { A, serif, sans, SIDEBAR_WIDTH } from "./adminUi";
import { LOGO_SRC } from "../../_brand/tokens";

interface Notif {
  id: number;
  event_type: string;
  title: string;
  message: string;
  entity_type: string;
  entity_id: string;
  payload: Record<string, unknown>;
  is_read: boolean;
  created_at: string;
}

// AA-323 round 6, Phần D — sidebar reorganized around the real ACP v2
// (N0-N8) business flow instead of historical grouping:
//   1. Real ACP v2 (N0-N8): Tenants(N1)/Marketplace(N1)/Quarter Plan Gate B(N5).
//   2. AA-internal's own content-authoring pipeline (Upload/S1 Rewrite/
//      Review/Brand/Master Content) — a different, older system for AA's
//      own tour copy, unrelated to the B2B tenant flow.
// AA-390 — the third group (legacy ACP v1: S2 Research/S3 Calendar/S4 Blog/
// S4 Social, admin_acp_proxy.py) was removed from this sidebar entirely
// (nobody needs ACPv1 access anymore, per Nghiep). The routes/pages and
// their backend are untouched and still reachable directly by URL.
// AA-475 — the "ACP v2 — Atoms" group (Atomize N2 + Atom Curation) was
// removed along with those pages; the non-admin Dashboard fallback that
// group used to carry moved into "AA Internal Content" below so
// reviewer/content roles keep a Dashboard entry point.
const CONTENT_AUTHORING_NAV = [
  { href: "/admin/upload",         icon: <Upload size={15} />,        label: "Upload (S0)" },
  { href: "/admin/s1-rewrite",      icon: <Wand2 size={15} />,         label: "S1 Rewrite" },
  { href: "/admin/review",         icon: <ClipboardList size={15} />, label: "Review Queue" },
  { href: "/admin/brand",          icon: <Palette size={15} />,       label: "Brand Identity" },
  { href: "/admin/master-content", icon: <Library size={15} />,       label: "Master Content" },
];

export default function AdminSidebar() {
  const router   = useRouter();
  const pathname = usePathname();
  const [role, setRole]         = useState("");
  const [userName, setUserName] = useState("");
  const [unread, setUnread]     = useState(0);
  const [showNotifs, setShowNotifs] = useState(false);
  const [notifs, setNotifs]     = useState<Notif[]>([]);

  const fetchCount = useCallback(() => {
    fetch("/api/admin/notifications/count")
      .then(r => r.ok ? r.json() : null)
      .then(d => d && setUnread(d.unread))
      .catch(() => {});
  }, []);

  useEffect(() => {
    fetchCount();
    const id = setInterval(fetchCount, 30000);
    return () => clearInterval(id);
  }, [fetchCount]);

  function openNotifs() {
    setShowNotifs(v => !v);
    if (!showNotifs) {
      fetch("/api/admin/notifications?unread_only=false&limit=10")
        .then(r => r.ok ? r.json() : null)
        .then(d => d && setNotifs(d.items))
        .catch(() => {});
    }
  }

  function markAllRead() {
    fetch("/api/admin/notifications/read-all", { method: "PUT" })
      .then(() => { setUnread(0); setNotifs(n => n.map(x => ({ ...x, is_read: true }))); })
      .catch(() => {});
  }

  useEffect(() => {
    const r = document.cookie.split(";").find(c => c.trim().startsWith("cis_role="))?.split("=")[1] ?? "";
    const n = document.cookie.split(";").find(c => c.trim().startsWith("cis_user="))?.split("=")[1] ?? "";
    setRole(r);
    setUserName(n ? decodeURIComponent(n) : r === "admin" ? "Admin" : "Content");
  }, []);

  const isAdmin = role === "admin";

  async function logout() {
    // AA-521: cis_admin_token is httpOnly (AA-232) — client JS can't clear
    // it, so the old document.cookie loop left the real session alive until
    // its natural 24h expiry. Clear server-side via /api/auth/admin-logout
    // (mirrors AA-427's /api/auth/tenant-logout), same request-then-redirect
    // shape as the tenant portal's Sidebar.tsx logout().
    try {
      await fetch("/api/auth/admin-logout", { method: "POST" });
    } catch {
      // ignore — redirect below either way; middleware re-verifies the JWT
      // on the next request regardless of whether the clear succeeded.
    }
    router.push("/login");
  }

  function active(href: string) {
    return pathname === href || pathname.startsWith(href + "/");
  }

  return (
    <aside style={{
      width: SIDEBAR_WIDTH, flexShrink: 0, background: A.ink, color: "#C9CFD8",
      padding: "22px 14px 24px", display: "flex", flexDirection: "column",
      gap: 28, position: "sticky", top: 0, height: "100vh", overflowY: "auto",
    }}>
      {/* Brand */}
      <div style={{ position: "relative" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, paddingBottom: 18, borderBottom: "1px solid rgba(255,255,255,0.07)" }}>
          {/* AA-605 — real Adventure Asia mountain mark (gold on the dark sidebar), replacing the
              red/gold "AA" tile. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={LOGO_SRC} alt="Adventure Asia" width={38} height={24}
            style={{ width: 38, height: "auto", flexShrink: 0, display: "block" }} />
          <div style={{ flex: 1 }}>
            <div style={{ fontFamily: serif, fontSize: 14, fontWeight: 500, color: "#F4F1EC", letterSpacing: "-0.01em", lineHeight: 1.2 }}>
              CIS Admin
            </div>
            <div style={{ fontSize: 9.5, textTransform: "uppercase" as const, letterSpacing: "0.18em", color: A.accent, fontWeight: 600, marginTop: 1 }}>
              {isAdmin ? "Administrator" : "Content Team"}
            </div>
          </div>
          {/* Notification bell */}
          <button onClick={openNotifs} title="Notifications" style={{
            position: "relative", background: "none", border: "none", cursor: "pointer",
            color: "#C9CFD8", display: "flex", padding: 4,
          }}>
            <Bell size={15} />
            {unread > 0 && (
              <span style={{
                position: "absolute", top: 0, right: 0,
                background: A.red, color: "#fff",
                borderRadius: 999, fontSize: 9, fontWeight: 700,
                minWidth: 14, height: 14, display: "grid", placeItems: "center",
                padding: "0 3px",
              }}>{unread > 99 ? "99+" : unread}</span>
            )}
          </button>
        </div>

        {/* Notification dropdown */}
        {showNotifs && (
          <div style={{
            position: "absolute", top: "100%", left: 0, right: 0, zIndex: 100,
            background: "#2A333E", border: "1px solid rgba(255,255,255,0.1)",
            borderRadius: 8, boxShadow: "0 8px 24px rgba(0,0,0,0.4)",
            maxHeight: 320, overflowY: "auto",
          }}>
            <div style={{
              display: "flex", alignItems: "center", justifyContent: "space-between",
              padding: "10px 12px 8px", borderBottom: "1px solid rgba(255,255,255,0.07)",
            }}>
              <span style={{ fontSize: 11, fontWeight: 600, color: "#F4F1EC" }}>Notifications</span>
              {unread > 0 && (
                <button onClick={markAllRead} style={{
                  background: "none", border: "none", cursor: "pointer",
                  fontSize: 10, color: A.gold, fontWeight: 600,
                }}>Mark all read</button>
              )}
            </div>
            {notifs.length === 0 ? (
              <div style={{ padding: "16px 12px", fontSize: 11, color: "#6E7681", textAlign: "center" }}>
                No notifications
              </div>
            ) : notifs.map(n => (
              <div key={n.id} style={{
                padding: "8px 12px",
                background: n.is_read ? "transparent" : "rgba(219,150,40,0.08)",
                borderBottom: "1px solid rgba(255,255,255,0.04)",
              }}>
                <div style={{ fontSize: 11, color: "#C9CFD8", fontWeight: n.is_read ? 400 : 600 }}>
                  {n.title || n.event_type}
                </div>
                {n.message && (
                  <div style={{ fontSize: 10, color: "#6E7681", marginTop: 2 }}>{n.message}</div>
                )}
                <div style={{ fontSize: 9.5, color: "#6E7681", marginTop: 2 }}>
                  {new Date(n.created_at).toLocaleString()}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Nav — AA-323 round 6, Phần D: real ACP v2 (N0-N8) flow first (N1
          setup/approval, admin-only, then N2 atoms, all roles), then the
          two OTHER pipelines clearly labeled as separate, then Settings.
          Every {isAdmin && ...} / unconditional-visibility boundary below
          is byte-for-byte the same boundary as before this round — only
          which group an item sits in, and each group's order/label,
          changed. */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 24 }}>
        {/* ACP v2 — N1 setup + N5 approval (admin-only, same as before) */}
        {isAdmin && (
          <NavGroup label="ACP v2 — Setup & Approval">
            <NavItem active={active("/admin/dashboard")} accent={A.accent} adminOnly
              icon={<LayoutDashboard size={15} />} label="Dashboard"
              onClick={() => router.push("/admin/dashboard")} />
            <NavItem active={active("/admin/tenants")} accent={A.accent} adminOnly
              icon={<Users size={15} />} label="Tenants"
              onClick={() => router.push("/admin/tenants")} />
            {/* AA-603 — "Run Health" NavItem removed with the deleted /admin/run-health page
                (it read the dead N7/N8 acp_v2_runs/slots tables, always rendered empty). */}
            {/* AA-437 [A4]'s "Cross-Tenant Oversight" NavItem (Eye icon) is retired here (AA-560)
                — real Playwright confirmed BOTH its replacements work in production first: "07 ·
                Platform Stats" (Review Log + Trust Ramp, reachable via SocialContentSubNav inside
                Social Content, same as "06 · Content Trace" — no top-level sidebar item for
                either) and Content Trace's new Force-unpublish button. The a4-oversight route +
                its middleware.ts entry are deleted in this same commit. */}
            {/* AA-505 — real per-call LLM cost/quality, Tenant->Model->Stage. Admin-only, same
                tier Cross-Tenant Oversight used to be (middleware.ts).
                AA-622 — expanded into the "External Spend" page (LLM + DataForSEO cost, 3 tabs);
                route kept as /admin/llm-usage, label + icon updated to match. */}
            <NavItem active={active("/admin/llm-usage")} accent={A.accent} adminOnly
              icon={<Wallet size={15} />} label="External Spend"
              onClick={() => router.push("/admin/llm-usage")} />
            {/* AA-553 — "Atom Curation" moved out of this group, down into "AA Internal Content"
                (right under Master Content) — it's Master Content pool data (Atom/Segment/Score/
                Route/Hub/Slate), not platform config, so it didn't belong alongside Tenants/Run
                Health/Cross-Tenant Oversight. Still admin-only (middleware.ts unchanged,
                PROTECTED_ROUTES roles: ["admin"]) — see that NavItem below for the isAdmin guard
                this move required now that it sits in an otherwise all-roles group. */}
            {/* AA-557 H.15 — "Tenant Activity" (was here, AA-551) removed as its OWN top-level
                entry: Nghiệp confirmed it's really Write/Gate→Review→Publish per-tenant CONTENT
                activity, genuinely part of the same Social Content flow (01-05) below, not general
                tenant account activity — now reachable as a "06-08" item inside Social Content's
                own inner tab-group instead (atom-curation/page.tsx). Route itself unchanged. */}
          </NavGroup>
        )}

        {/* AA-internal's own content-authoring pipeline — a different, older
            system for AA's own tour copy, not part of the B2B ACP v2 flow.
            Visible to all roles (same as before). AA-475: the non-admin
            Dashboard fallback (reviewer/content roles don't get the
            isAdmin-gated one above) moved here from the deleted
            "ACP v2 — Atoms" group. */}
        <NavGroup label="AA Internal Content">
          {!isAdmin && (
            <NavItem active={active("/admin/dashboard")} accent={A.gold}
              icon={<LayoutDashboard size={15} />} label="Dashboard"
              onClick={() => router.push("/admin/dashboard")} />
          )}
          {CONTENT_AUTHORING_NAV.map(n => (
            <NavItem key={n.href} active={active(n.href)} accent={A.gold}
              icon={n.icon} label={n.label} onClick={() => router.push(n.href)} />
          ))}
          {/* AA-553 — moved here from "ACP v2 — Setup & Approval" (was miscategorized: this is
              Master Content pool data — Atom/Segment/Score/Route/Hub/Slate — not platform config,
              so it belongs right under Master Content, not next to Tenants/Run Health). Kept
              isAdmin-gated (adminOnly tag, AA-605; was a red accent vs its gold neighbors) because
              this group itself is NOT role-gated (renders for reviewer/content too) but
              middleware.ts's PROTECTED_ROUTES still restricts /admin/atom-curation to
              roles: ["admin"] — left that restriction untouched (out of this issue's scope), so a
              non-admin NavItem here would be a dead link. AA-554 A.2 — label renamed "Social
              Content" (Nghiệp's decision from AA-553's name options), route
              (`/admin/atom-curation`) unchanged. */}
          {isAdmin && (
            <NavItem active={active("/admin/atom-curation")} accent={A.accent} adminOnly
              icon={<Puzzle size={15} />} label="Social Content"
              onClick={() => router.push("/admin/atom-curation")} />
          )}
        </NavGroup>

        {/* AA-390 hid this Legacy B2B pipeline (ACP v1) sidebar entry, keeping the pages
            reachable by direct URL "if ever needed again" — but AA-477/AA-439 later deleted
            their backend routers entirely without anyone checking that promise. AA-633
            (23/09/2026) found the pages 404ing live and removed them (admin/pipeline/s1, s2,
            s3, s4-blog, s4-social) — there is no longer anything at those URLs to reach. */}
      </div>

      {/* Settings — admin only */}
      {isAdmin && (
        <NavItem active={active("/admin/settings")} accent={A.accent} adminOnly
          icon={<Settings size={15} />} label="Settings"
          onClick={() => router.push("/admin/settings")} />
      )}

      {/* Footer */}
      <div style={{ paddingTop: 14, borderTop: "1px solid rgba(255,255,255,0.07)" }}>
        <div style={{
          display: "flex", alignItems: "center", gap: 9,
          padding: 8, borderRadius: 8, background: "rgba(255,255,255,0.03)",
        }}>
          <div style={{
            width: 30, height: 30, borderRadius: 6,
            background: A.accent,
            display: "grid", placeItems: "center",
            color: "#fff", fontWeight: 700, fontSize: 12, flexShrink: 0,
          }}>
            {userName.charAt(0).toUpperCase()}
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ color: "#F4F1EC", fontSize: 12, fontWeight: 600 }}>{userName}</div>
            <div style={{ color: "#8A929D", fontSize: 10.5 }}>{isAdmin ? "Admin" : "Content"}</div>
          </div>
          <button onClick={logout} title="Sign out"
            style={{ background: "none", border: "none", cursor: "pointer", color: "#8A929D", display: "flex" }}>
            <LogOut size={13} />
          </button>
        </div>
      </div>
    </aside>
  );
}

function NavGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 9.5, textTransform: "uppercase" as const, letterSpacing: "0.16em", color: "#6E7681", padding: "0 10px 8px", fontWeight: 600 }}>
        {label}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>{children}</div>
    </div>
  );
}

function NavItem({ active, icon, label, accent, onClick, adminOnly = false }: {
  active: boolean; icon: React.ReactNode; label: string;
  accent: string; onClick: () => void;
  // AA-605 — admin-only items used to be told apart by a red accent; now every item shares the
  // brand gold and admin-only ones carry a small "ADMIN" tag instead.
  adminOnly?: boolean;
}) {
  return (
    <button onClick={onClick} style={{
      display: "flex", alignItems: "center", gap: 10, width: "100%",
      padding: "8px 10px", borderRadius: 7, border: "none",
      background: active ? `${accent}18` : "transparent",
      color: active ? "#fff" : "#C9CFD8",
      fontSize: 13, fontWeight: 500, cursor: "pointer",
      textAlign: "left" as const, fontFamily: sans, position: "relative",
      transition: "background .15s, color .15s",
    }}>
      {active && (
        <span style={{ position: "absolute", left: 0, top: 8, bottom: 8, width: 2, background: accent, borderRadius: "0 2px 2px 0" }} />
      )}
      <span style={{ flexShrink: 0, opacity: active ? 1 : 0.75 }}>{icon}</span>
      <span style={{ flex: 1 }}>{label}</span>
      {adminOnly && (
        <span style={{
          fontSize: 8.5, fontWeight: 600, letterSpacing: "0.12em", color: "#8A929D",
          border: "1px solid #3A4453", borderRadius: 3, padding: "0 4px", lineHeight: "14px",
        }}>ADMIN</span>
      )}
    </button>
  );
}
