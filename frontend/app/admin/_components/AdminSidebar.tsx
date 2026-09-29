"use client";
// app/admin/_components/AdminSidebar.tsx

import { useEffect, useState, useCallback } from "react";
import { useRouter, usePathname } from "next/navigation";
import { LayoutDashboard, Users, Upload, Wand2, ClipboardList, Library, LogOut, Bell, Settings, Wallet, Puzzle, ListChecks } from "lucide-react";
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

// AA-663 — one information architecture (review §5.5): Overview · Content · Intelligence · Tenants ·
// Operations · Settings. Replaces the AA-323 grouping ("ACP v2 — Setup & Approval" + "AA Internal
// Content"), which gave admins two "Dashboard" entries. Visibility still follows middleware.ts
// PROTECTED_ROUTES exactly (adminOnly items render for role "admin" only, so no dead links for
// reviewer/content); the "ADMIN" tag (AA-605) is gone — a role only ever sees what it can open.
interface NavEntry {
  href: string; icon: React.ReactNode; label: string; adminOnly?: boolean;
  also?: string[];  // other routes that belong to this entry (its sub-nav pages)
}

const NAV_GROUPS: { label: string; items: NavEntry[] }[] = [
  { label: "Overview", items: [
    { href: "/admin/dashboard", icon: <LayoutDashboard size={15} />, label: "Dashboard" },
  ] },
  { label: "Content", items: [
    { href: "/admin/upload",         icon: <Upload size={15} />,        label: "Upload (S0)" },
    { href: "/admin/s1-rewrite",     icon: <Wand2 size={15} />,         label: "Rewrite (S1)" },
    { href: "/admin/review",         icon: <ClipboardList size={15} />, label: "Review Queue" },
    { href: "/admin/master-content", icon: <Library size={15} />,       label: "Master Content" },
  ] },
  // Atoms · Segments · Scores · Routes & Hubs · Slate are tabs inside this one page (AA-553/554).
  { label: "Intelligence", items: [
    { href: "/admin/atom-curation", icon: <Puzzle size={15} />, label: "Social Content", adminOnly: true,
      also: ["/admin/tenant-activity", "/admin/platform-stats"] },
  ] },
  { label: "Tenants", items: [
    { href: "/admin/tenants", icon: <Users size={15} />, label: "Tenants", adminOnly: true },
  ] },
  { label: "Operations", items: [
    // AA-622 — LLM + DataForSEO spend and budgets; route kept as /admin/llm-usage.
    { href: "/admin/llm-usage", icon: <Wallet size={15} />,     label: "External Spend", adminOnly: true },
    { href: "/admin/jobs",      icon: <ListChecks size={15} />, label: "Jobs",           adminOnly: true },
  ] },
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

      <nav style={{ flex: 1, display: "flex", flexDirection: "column", gap: 22 }}>
        {NAV_GROUPS.map(group => {
          const items = group.items.filter(n => isAdmin || !n.adminOnly);
          if (!items.length) return null;
          return (
            <NavGroup key={group.label} label={group.label}>
              {items.map(n => (
                <NavItem key={n.href} active={[n.href, ...(n.also ?? [])].some(active)} icon={n.icon} label={n.label}
                  onClick={() => router.push(n.href)} />
              ))}
            </NavGroup>
          );
        })}
      </nav>

      {/* Settings — models per stage, AA brand identity (AA-663, was a top-level page), SEO config,
          pipeline gates. All staff roles, same as middleware.ts. */}
      <NavItem active={active("/admin/settings")}
        icon={<Settings size={15} />} label="Settings"
        onClick={() => router.push("/admin/settings")} />

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

function NavItem({ active, icon, label, onClick }: {
  active: boolean; icon: React.ReactNode; label: string; onClick: () => void;
}) {
  const accent = A.gold;
  return (
    <button onClick={onClick} aria-current={active ? "page" : undefined} style={{
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
      <span style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{label}</span>
    </button>
  );
}
