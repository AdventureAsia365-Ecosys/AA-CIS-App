"use client";
// AA-636 — the top-bar bell had no handler. It now opens a panel of recent tour activity
// (the same /v1/billing `activity` the shell already loads), with an unread dot driven by a
// per-browser "last seen" timestamp. No new backend: notifications are a view of activity.
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Bell, Check, X, RefreshCw } from "lucide-react";
import { T, sans, fmtDateTime } from "./ui";

type Activity = { id: string; status: string; edit_source: string; tour_name: string; country: string | null; created_at: string };

const SEEN_KEY = "cis_portal_notif_seen_at";

function readSeen(): number {
  try { return Number(localStorage.getItem(SEEN_KEY) ?? 0) || 0; } catch { return 0; }
}
function writeSeen(ts: number) {
  try { localStorage.setItem(SEEN_KEY, String(ts)); } catch { /* storage blocked — dot just stays */ }
}

const describe = (a: Activity) =>
  a.status === "approved" ? "was approved" :
  a.status === "rejected" ? "was rejected" :
  a.edit_source === "ai_generated" ? "rewrite is ready to review" : "was edited";

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

  const newest = activity.reduce((m, a) => Math.max(m, Date.parse(a.created_at) || 0), 0);
  const unread = activity.filter(a => (Date.parse(a.created_at) || 0) > seenAt).length;

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && newest > seenAt) { writeSeen(newest); setSeenAt(newest); }
  }

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button onClick={toggle} aria-label={unread ? `Notifications, ${unread} new` : "Notifications"} aria-expanded={open}
        style={{ width: 36, height: 36, borderRadius: 8, background: "#fff", border: `1px solid ${open ? T.gold : T.line}`, display: "grid", placeItems: "center", cursor: "pointer", color: T.ink3, position: "relative" }}>
        <Bell size={15} />
        {unread > 0 && (
          <span style={{ position: "absolute", top: 6, right: 7, width: 8, height: 8, borderRadius: "50%", background: T.gold, boxShadow: "0 0 0 2px #fff" }} />
        )}
      </button>
      {open && (
        <div role="dialog" aria-label="Notifications" style={{
          position: "absolute", right: 0, top: 44, width: "min(360px, calc(100vw - 32px))", background: T.card,
          border: `1px solid ${T.line}`, borderRadius: 12, boxShadow: "0 12px 40px -8px rgba(31,41,51,0.25)", zIndex: 50, overflow: "hidden", fontFamily: sans,
        }}>
          <div style={{ padding: "12px 16px", borderBottom: `1px solid ${T.line2}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: T.ink }}>Notifications</span>
            <Link href="/portal/activity" onClick={() => setOpen(false)} style={{ fontSize: 12, color: T.ink3, textDecoration: "none" }}>View all →</Link>
          </div>
          {activity.length === 0 ? (
            <div style={{ padding: "28px 16px", textAlign: "center", fontSize: 13, color: T.muted2 }}>You&rsquo;re all caught up.</div>
          ) : activity.slice(0, 6).map(a => {
            const Icon = a.status === "approved" ? Check : a.status === "rejected" ? X : RefreshCw;
            const color = a.status === "approved" ? T.green : a.status === "rejected" ? T.red : T.amber;
            const bg = a.status === "approved" ? T.greenSoft : a.status === "rejected" ? T.redSoft : T.goldTint;
            return (
              <Link key={a.id} href="/portal/t4-pool" onClick={() => setOpen(false)} style={{
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
