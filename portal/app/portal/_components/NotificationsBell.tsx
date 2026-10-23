"use client";
// AA-636 — the top-bar bell had no handler. It now opens a panel of recent tour activity
// (the same /v1/billing `activity` the shell already loads), with an unread dot driven by a
// per-browser "last seen" timestamp. No new backend: notifications are a view of activity.
// AA-759: lives in the PortalShell topbar; the panel is a disclosure region (not a dialog: it does
// not trap focus), so the kit rule against hand-rolled dialogs holds.
// AA-986 L3 (root ADR 0015, Q7): the bell also reads the tenant's REAL notifications, GET /v1/notifications
// (tour.discontinued / tour.reinstated, written by the tour lifecycle service when AA stops or resumes serving a
// tour the tenant uses). They come first, with a server-side unread count; opening the panel marks them read
// (POST /v1/notifications/read-all). The badges on My Tours / Content / Publish stay the source of truth.
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Bell, Check, X, RefreshCw, RotateCcw } from "lucide-react";
import { apiGet, apiSend } from "@/app/_kit";
import { T, sans, fmtDateTime } from "./ui";
import type { ShellActivityView } from "./PortalShellContext";

/** One row of GET /v1/notifications (api/routers/v1_notifications.py). */
export interface TenantNotification {
  id: number;
  event_type: string;
  title: string;
  message: string;
  tour_id: string | null;
  tour_name: string | null;
  reason: string | null;
  live_posts: number;
  is_read: boolean;
  created_at: string | null;
}

export const TENANT_NOTIFICATIONS_KEY = ["tenant-notifications"] as const;

type Activity = ShellActivityView;

const SEEN_KEY = "cis_portal_notif_seen_at";

function readSeen(): number {
  try { return Number(localStorage.getItem(SEEN_KEY) ?? 0) || 0; } catch { return 0; }
}
function writeSeen(ts: number) {
  try { localStorage.setItem(SEEN_KEY, String(ts)); } catch { /* storage blocked — dot just stays */ }
}

// AA-760 — the sentence follows the job state the shell joined in, never the row flags alone.
const describe = (a: Activity) => {
  switch (a.state) {
    case "approved": return "was approved";
    case "rejected": return "was rejected";
    case "failed": return "rewrite didn't finish — retry it in My Tours";
    case "queued": case "writing": case "checking": return "is being rewritten";
    case "done": return "rewrite is ready to review";
    case "edited": return "was edited";
    default: return "was updated";
  }
};

export default function NotificationsBell({ activity }: { activity: Activity[] }) {
  const [open, setOpen] = useState(false);
  const [seenAt, setSeenAt] = useState<number>(() => (typeof window === "undefined" ? 0 : readSeen()));
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);

  const qc = useQueryClient();
  const { data: notes } = useQuery({
    queryKey: TENANT_NOTIFICATIONS_KEY,
    queryFn: () => apiGet<{ items?: TenantNotification[]; unread?: number }>("/api/tenant/v1/notifications?limit=20"),
    refetchInterval: 120_000,
    staleTime: 60_000,
  });
  const markAllRead = useMutation({
    mutationFn: () => apiSend<{ cleared: number }>("/api/tenant/v1/notifications/read-all", { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: TENANT_NOTIFICATIONS_KEY }),
  });
  const tourNotes = notes?.items ?? [];
  const serverUnread = notes?.unread ?? 0;

  const newest = activity.reduce((m, a) => Math.max(m, Date.parse(a.created_at) || 0), 0);
  const unread = activity.filter(a => (Date.parse(a.created_at) || 0) > seenAt).length + serverUnread;

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && newest > seenAt) { writeSeen(newest); setSeenAt(newest); }
    if (next && serverUnread > 0 && !markAllRead.isPending) markAllRead.mutate();
  }

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button onClick={toggle} aria-label={unread ? `Notifications, ${unread} new` : "Notifications"} aria-expanded={open}
        style={{ width: 36, height: 36, borderRadius: 8, background: open ? T.goldTint : "transparent", border: "none", display: "grid", placeItems: "center", cursor: "pointer", color: T.ink3, position: "relative" }}>
        <Bell size={15} />
        {unread > 0 && (
          <span style={{ position: "absolute", top: 6, right: 7, width: 8, height: 8, borderRadius: "50%", background: T.gold, boxShadow: `0 0 0 2px ${T.card}` }} />
        )}
      </button>
      {open && (
        <div role="region" aria-label="Notifications" style={{
          position: "absolute", right: 0, top: 44, width: "min(360px, calc(100vw - 32px))", background: T.card,
          border: `1px solid ${T.line}`, borderRadius: 12, boxShadow: "0 12px 40px -8px rgba(31,41,51,0.25)", zIndex: 50, overflow: "hidden", fontFamily: sans,
        }}>
          <div style={{ padding: "12px 16px", borderBottom: `1px solid ${T.line2}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: T.ink }}>Notifications</span>
            <Link href="/portal/account/activity" onClick={() => setOpen(false)} style={{ fontSize: 12, color: T.ink3, textDecoration: "none" }}>View all →</Link>
          </div>
          {tourNotes.slice(0, 5).map(n => {
            const gone = n.event_type === "tour.discontinued";
            const Icon = gone ? AlertTriangle : RotateCcw;
            return (
              <Link key={`n-${n.id}`} href={n.live_posts > 0 ? "/portal/content/publish" : "/portal/tours/mine"}
                onClick={() => setOpen(false)} style={{
                  display: "grid", gridTemplateColumns: "30px minmax(0,1fr)", gap: 10, padding: "11px 16px",
                  textDecoration: "none", borderBottom: `1px solid ${T.line2}`,
                  background: n.is_read ? "transparent" : T.goldTint,
                }}>
                <span style={{ width: 30, height: 30, borderRadius: 8, background: gone ? T.redSoft : T.greenSoft, color: gone ? T.red : T.green, display: "grid", placeItems: "center" }}>
                  <Icon size={14} strokeWidth={2.2} />
                </span>
                <span style={{ minWidth: 0 }}>
                  <span style={{ display: "block", fontSize: 12.5, color: T.ink, lineHeight: 1.4 }}>
                    <strong style={{ fontWeight: 600 }}>{n.title}</strong> · {n.message}
                  </span>
                  <span style={{ display: "block", fontSize: 11, color: T.muted2, marginTop: 2 }}>{fmtDateTime(n.created_at)}</span>
                </span>
              </Link>
            );
          })}
          {activity.length === 0 && tourNotes.length === 0 ? (
            <div style={{ padding: "28px 16px", textAlign: "center", fontSize: 13, color: T.muted2 }}>You&rsquo;re all caught up.</div>
          ) : activity.slice(0, 6).map(a => {
            const bad = a.state === "rejected" || a.state === "failed";
            const Icon = a.state === "approved" ? Check : bad ? X : RefreshCw;
            const color = a.state === "approved" ? T.green : bad ? T.red : T.amber;
            const bg = a.state === "approved" ? T.greenSoft : bad ? T.redSoft : T.goldTint;
            return (
              <Link key={a.id} href="/portal/tours/mine" onClick={() => setOpen(false)} style={{
                display: "grid", gridTemplateColumns: "30px minmax(0,1fr)", gap: 10, padding: "11px 16px", textDecoration: "none",
                borderBottom: `1px solid ${T.line2}`,
              }}>
                <span style={{ width: 30, height: 30, borderRadius: 8, background: bg, color, display: "grid", placeItems: "center" }}><Icon size={14} strokeWidth={2.2} /></span>
                <span style={{ minWidth: 0 }}>
                  <span style={{ display: "block", fontSize: 12.5, color: T.ink, lineHeight: 1.4 }}>
                    <strong style={{ fontWeight: 600 }}>{a.tour_name || "A tour"}</strong> {describe(a)}
                  </span>
                  <span style={{ display: "block", fontSize: 11, color: T.muted2, marginTop: 2 }}>{fmtDateTime(a.created_at)}</span>
                </span>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}
