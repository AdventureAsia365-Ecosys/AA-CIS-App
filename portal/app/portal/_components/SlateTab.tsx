"use client";
// portal/app/portal/_components/SlateTab.tsx — AA-511, the Slate (Weekly Slots' replacement).
//
// Replaces SlotPickerPanel.tsx on PlanningTab.tsx's own render path (not deleted — the epic's
// own "giữ code cũ, chỉ ngưng dùng ở đường Slate chính, không xoá" rule; see docs/claude_audit/
// AA-511-step0-slate-investigation.md for the full evidence trail behind this build).
//
// 8 tabs, one per Channel: 5 weekly-rhythm (Blog/LinkedIn/Facebook/Instagram/TikTok) + 3
// on-demand (Email/Landing Page/Ads, STEP0 Q3 — the origin's own `derive_posting_rhythm()`
// never produces a Subject list for these at all; this build runs the same Bar against them at
// their default-zero threshold, so an on-demand tab just lists "everything eligible", no
// nhịp/tuần line). Each row shows why it cleared the Bar (`cleared_bar_reason`, verbatim from
// `services/acp_shared/slate.py::_clears_bar()`) and a "Chọn viết" button that posts the pick.
//
// AA-564 Nhóm 4.2 (2026-09-08) — pick() used to navigate to the standalone T8 route
// (`/portal/create/write?resume_request_id=`). It now expands `AngleGateWizard` INLINE under
// the same Subject row instead (accordion — an `expanded` state held at most one
// {subjectId, requestId} at a time, shared by both `proposed.map()` and `DecidedList` via the
// same `SubjectRow` so the wizard stays open across the row moving from one list to the other
// when `onPicked()`'s refetch flips `state` proposed -> picked). The standalone route/page still
// exists unchanged (a valid bookmark/deep-link), just no longer the only way in — its own
// `AngleGateWizard`'s `requestId` load path (built for AA-497's "Change angle" reopen) already
// handled loading an existing request at whatever step it's actually at, with zero changes
// needed there for this embedded use.
//
// AA-556 (2026-09-10) — "Cut" button added next to "Chọn viết" in SubjectRow's `state ===
// "proposed"` block. `cut_subject()` (services/acp_shared/slate.py) already existed backend-only
// since AA-554 H.2, always re-SELECTs the real DB state before allowing proposed->cut (never
// trusts a client-supplied value) — confirmed via direct read + posted on AA-556 before building
// this, since AA-564 Nhóm 5 flagged a possible race between this same row's `pick()` succeeding
// (isExpanded flips true synchronously) and the Slate's own refetch landing (which is what
// actually updates `subject.state` away from "proposed"). Backend is safe either way (a
// same-row Cut mid-race just 409s, same as an already-picked row would), so disabling Cut during
// `picking || isExpanded` here is purely UX polish (avoid a pointless 409/flicker), not a data
// safety fix.
//
// AA-668 (S237) — the inline accordion above is replaced by the Writer panel (WriterPanel.tsx, a
// right-side kit Drawer; full-screen on a phone). "Pick to write" opens it on the new request;
// a Subject picked earlier in this visit gets a "Continue" button that reopens it (the Slate API
// does not return a picked Subject's request_id, so a reload loses that shortcut — the piece is in
// My Content either way). Closing the panel while a piece is still being written hands it to
// useWriteWatcher, which raises a toast when the background job finishes.
//
// API: GET /api/tenant/v1/slate, POST /api/tenant/v1/subjects/{id}/pick,
// POST /api/tenant/v1/subjects/{id}/cut (api/routers/v1_planning.py's `slate_router`, AA-511/556).
//
// AA-511 FE audit fix (2026-09-02, before Done) — 5 of the 6 reported gaps closed here (#4 was
// explicitly "no fix needed, skip"), each cited to the finding it closes:
//  #1 per-tab skeleton (SkeletonPanel) instead of one full-panel spinner covering everything.
//  #2 the important one — pick()'s error handler used to call onPicked() (refresh the whole
//     Slate) even when the POST itself failed, which could show a stale pickError message right
//     next to a row that had actually flipped to "picked" server-side if the client just lost the
//     response (a real timeout, not the request itself failing). onPicked()/setPickError(null)
//     now only run on a CONFIRMED 200 — a failed request leaves the Slate exactly as it was, and
//     the tenant gets an explicit "Refresh" link to check the real state on their own terms
//     instead of an automatic, possibly-misleading refresh.
//  #3 a Channel with decided (picked/used/cut) history but 0 currently-eligible Subjects now gets
//     its own message, distinct from "never had any Subject at all".
//  #5 a thin divider + a group icon (Calendar/Zap) on the tab strip, so the 3 on-demand tabs read
//     as a different group from the 5 weekly ones without opening any of them.
//  #6 the tab strip scrolls horizontally (hidden scrollbar) instead of wrapping onto extra lines
//     on a narrow screen.

import { useCallback, useEffect, useRef, useState } from "react";
import { Calendar, ChevronRight, Sparkles, X, Zap, AlertTriangle, ClipboardList, CheckCircle2 } from "lucide-react";
import { T, serif, sans, mono, Card, CardHead, Badge, Btn, EmptyState, StickyBar } from "./ui";
import { ToastProvider } from "@/app/_kit";
import type { ContentPiece } from "./AngleGateWizard";
import { WriterPanel, useWriteWatcher, watchPiece, type WriterTarget } from "./WriterPanel";

interface ClearedBarReason {
  channel: string;
  on_demand: boolean;
  needs_demand: number; demand: number | null; demand_ok: boolean;
  needs_questions: number; questions: number; questions_ok: boolean;
  needs_said: number; said: number; said_ok: boolean;
}

interface SlateSubject {
  subject_id: string;
  channel: string;
  state: "proposed" | "picked" | "used" | "cut";
  score: number | null;
  cleared_bar_reason: ClearedBarReason;
  segment_id: string | null;
  route_id: string | null;
  place: string | null;
  action: string | null;
  hub_name: string | null;
  created_at: string | null;
}

interface ChannelSlate {
  channel: string;
  on_demand: boolean;
  eligible_count: number;
  subjects: SlateSubject[];
}

interface SlateResponse {
  channels: Record<string, ChannelSlate>;
  posts_per_week: number;
  // AA-629 — non-empty ONLY when this tenant declared real target_market.countries the platform
  // doesn't support yet (a tenant who declared nothing at all never sees this — that case's US
  // default is a legitimate product choice, not something to warn about).
  unmatched_markets?: string[];
  // AA-764 — the Slate is proposed by a background job; `refreshing` while it runs.
  refresh?: { status: string; refreshing: boolean; finished_at: string | null };
}

const SLATE_REFRESH_POLL_MS = 15_000;

// Order: 5 weekly-rhythm tabs first, then the 3 on-demand ones — matches the build prompt's own
// "5 tab nhịp tuần + 3 tab theo yêu cầu" grouping. `group` drives the tab-strip divider + icon
// (FE audit #5) — the tab list itself is unchanged.
const CHANNEL_TABS: { key: string; label: string; group: "weekly" | "on_demand" }[] = [
  { key: "blog", label: "Blog", group: "weekly" },
  { key: "linkedin", label: "LinkedIn", group: "weekly" },
  { key: "facebook", label: "Facebook", group: "weekly" },
  { key: "instagram", label: "Instagram", group: "weekly" },
  { key: "tiktok", label: "TikTok", group: "weekly" },
  { key: "email", label: "Email", group: "on_demand" },
  { key: "landing_page", label: "Landing Page", group: "on_demand" },
  { key: "ads", label: "Ads", group: "on_demand" },
];

// AA-668 — the Writer panel's state, shared by every row of every Channel tab: at most one panel
// open (`panel`), plus the requests picked during this visit (`known`, keyed by subject_id) so a
// picked row can reopen its panel after it moved from `proposed` to `decided`.
interface WriterControls {
  openSubjectId: string | null;
  known: Record<string, WriterTarget>;
  open: (t: WriterTarget) => void;
}

/** AA-759 — the Channels a Create page shows: Articles = blog, Social = every other Channel. */
export const ARTICLE_CHANNELS: readonly string[] = ["blog"];
export const SOCIAL_CHANNELS: readonly string[] = CHANNEL_TABS.map(c => c.key).filter(k => !ARTICLE_CHANNELS.includes(k));

// The kit toast needs a provider; the Slate is the only toast user on this page.
export default function SlateTab({ channels, title = "Social Content" }: {
  /** AA-759 — the Channel tabs to show (default: all 8). */
  channels?: readonly string[];
  title?: string;
}) {
  return (
    <ToastProvider>
      <SlateTabInner channels={channels} title={title} />
    </ToastProvider>
  );
}

function SlateTabInner({ channels, title }: { channels?: readonly string[]; title: string }) {
  const tabs = channels ? CHANNEL_TABS.filter(c => channels.includes(c.key)) : CHANNEL_TABS;
  const [data, setData] = useState<SlateResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeChannel, setActiveChannel] = useState(() => tabs[0]?.key ?? "blog");
  const [panel, setPanel] = useState<WriterTarget | null>(null);
  const [known, setKnown] = useState<Record<string, WriterTarget>>({});
  const { watch, unwatch } = useWriteWatcher();

  const openWriter = useCallback((t: WriterTarget) => {
    unwatch(t.requestId);
    setKnown(prev => ({ ...prev, [t.subjectId]: t }));
    setPanel(t);
  }, [unwatch]);

  const closeWriter = useCallback((lastPiece: ContentPiece | null) => {
    if (panel && lastPiece?.ready_state === "in_progress") {
      watch(watchPiece(lastPiece.piece_id, panel.requestId, panel.title));
    }
    setPanel(null);
  }, [panel, watch]);

  // A write whose 202 arrived after the panel closed (closed in the click → write-call window).
  // Skipped when that request's panel is open again by then: the reopened wizard resumes it itself.
  const panelRef = useRef<WriterTarget | null>(null);
  useEffect(() => { panelRef.current = panel; }, [panel]);
  const watchDetached = useCallback((p: ContentPiece, t: WriterTarget) => {
    if (p.ready_state !== "in_progress" || panelRef.current?.requestId === t.requestId) return;
    watch(watchPiece(p.piece_id, t.requestId, t.title));
  }, [watch]);

  const writer: WriterControls = { openSubjectId: panel?.subjectId ?? null, known, open: openWriter };

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    fetch("/api/tenant/v1/slate")
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then((d: SlateResponse) => setData(d))
      .catch(e => setError(e.detail ?? "Couldn't load the Slate — try again."))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  // AA-764 — GET /v1/slate only reads now; while a background proposal runs, re-read quietly
  // (no skeleton) until it finishes so new topics appear on their own.
  const refreshing = !!data?.refresh?.refreshing;
  useEffect(() => {
    if (!refreshing) return;
    const id = setInterval(() => {
      fetch("/api/tenant/v1/slate")
        .then(r => (r.ok ? r.json() : null))
        .then((d: SlateResponse | null) => { if (d) setData(d); })
        .catch(() => { /* transient — next tick */ });
    }, SLATE_REFRESH_POLL_MS);
    return () => clearInterval(id);
  }, [refreshing]);

  const [refreshError, setRefreshError] = useState<string | null>(null);
  const requestRefresh = useCallback(() => {
    setRefreshError(null);
    fetch("/api/tenant/v1/slate/refresh", { method: "POST" })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then((d: { refresh: SlateResponse["refresh"] }) => setData(prev => (prev ? { ...prev, refresh: d.refresh } : prev)))
      .catch(e => setRefreshError(e.detail ?? "Couldn't start a refresh — try again."));
  }, []);

  const active = data?.channels[activeChannel];

  return (
    <Card style={{ padding: "16px 18px", marginBottom: 18 }}>
      {/* Shimmer keyframes (FE audit #1) + hidden-scrollbar class for the tab strip (#6) — same
          plain-<style>-tag pattern CatalogTab.tsx already uses in this same portal. */}
      <style>{`
        @keyframes aa511SlateShimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
        .aa511-slate-tabstrip { scrollbar-width: none; -ms-overflow-style: none; }
        .aa511-slate-tabstrip::-webkit-scrollbar { display: none; }
      `}</style>

      {/* AA-524 — sticky, bleeding out to this Card's own custom padding (16px/18px) so it reads
          as one continuous bar rather than floating detached from the card's border once a
          Channel's Subject list scrolls underneath it. */}
      <StickyBar
        background={T.card}
        style={{
          marginTop: -16, marginLeft: -18, marginRight: -18,
          paddingTop: 16, paddingLeft: 18, paddingRight: 18,
          borderRadius: "12px 12px 0 0",
        }}
      >
        {/* AA-564 4.1 — Tenant-facing label renamed "Slate" -> "Social Content"; the component/file
            name (SlateTab.tsx, internal-only) is unchanged, matching CONTEXT.md's own Subject/Slate
            terminology used everywhere else in the codebase. */}
        <CardHead title={title} />
        <p style={{ fontSize: 12.5, color: T.muted, lineHeight: 1.6, margin: "0 0 14px" }}>
          Every moment that has cleared its Channel&rsquo;s bar — search-led Channels need
          measured demand and questions to answer; attention-led Channels need enough of the
          journey actually described. Sorted strongest first. Pick one to start writing.
        </p>

        {/* AA-764 — proposal state: topics are re-ranked in the background, never on page load. */}
        {data && (
          <div style={{
            marginBottom: 14, display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
            fontSize: 12, color: T.muted,
          }}>
            {refreshing ? (
              <span role="status">Updating topics in the background — new ones appear here when it finishes (a few minutes).</span>
            ) : (
              <span>
                {data.refresh?.finished_at
                  ? <>Topics last updated {new Date(data.refresh.finished_at).toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" })}.</>
                  : <>Topics haven&rsquo;t been ranked yet.</>}
                {data.refresh?.status === "failed" && <> The last update didn&rsquo;t finish.</>}
              </span>
            )}
            <Btn variant="secondary" disabled={refreshing} onClick={requestRefresh}>
              {refreshing ? "Updating…" : "Update topics"}
            </Btn>
            {refreshError && <span style={{ color: T.red }}>{refreshError}</span>}
          </div>
        )}

        {/* AA-629 — explicit notice instead of silently showing US-market ranking data for a
            tenant whose declared markets aren't supported yet. */}
        {data && data.unmatched_markets && data.unmatched_markets.length > 0 && (
          <div style={{
            marginBottom: 14, padding: "10px 12px", background: T.goldTint,
            border: `1px solid ${T.gold}`, borderRadius: 8,
            fontSize: 12, color: T.ink, display: "flex", alignItems: "flex-start", gap: 8,
          }}>
            <span style={{ fontSize: 14, flexShrink: 0 }}>⚠</span>
            <span>
              Your configured market{data.unmatched_markets.length > 1 ? "s" : ""} (
              <strong>{data.unmatched_markets.join(", ")}</strong>) {data.unmatched_markets.length > 1 ? "aren't" : "isn't"} supported
              for SEO research yet — the ranking data below is not specific to{" "}
              {data.unmatched_markets.length > 1 ? "those markets" : "that market"}. We&rsquo;ve
              recorded your request; reach out if you need this prioritized.
            </span>
          </div>
        )}

        <div className="aa511-slate-tabstrip" style={{
          display: "flex", gap: 4, alignItems: "center", overflowX: "auto", flexWrap: "nowrap",
          borderBottom: `1px solid ${T.line2}`, paddingBottom: 2,
        }}>
          {tabs.map(({ key, label, group }, i) => {
            const showDivider = i > 0 && tabs[i - 1].group !== group;
            const count = data?.channels[key]?.eligible_count ?? null;
            const isActive = activeChannel === key;
            const Icon = group === "weekly" ? Calendar : Zap;
            return (
              <div key={key} style={{ display: "flex", alignItems: "center", flexShrink: 0 }}>
                {showDivider && (
                  <div aria-hidden style={{ width: 1, height: 20, background: T.line2, margin: "0 6px", flexShrink: 0 }} />
                )}
                <button
                  onClick={() => setActiveChannel(key)}
                  title={group === "weekly" ? "Weekly rhythm" : "On-demand"}
                  style={{
                    display: "flex", alignItems: "center", gap: 5, whiteSpace: "nowrap", flexShrink: 0,
                    padding: "7px 12px", borderRadius: "8px 8px 0 0", cursor: "pointer",
                    fontFamily: sans, fontSize: 12.5, fontWeight: isActive ? 700 : 500,
                    color: isActive ? T.ink : T.muted,
                    background: isActive ? T.bg : "transparent",
                    border: "none", borderBottom: isActive ? `2px solid ${T.gold}` : "2px solid transparent",
                  }}
                >
                  <Icon size={11} style={{ opacity: 0.55, flexShrink: 0 }} />
                  {label}{count != null && <span style={{ marginLeft: 2, fontFamily: mono, fontSize: 10.5, color: T.muted2 }}>{count}</span>}
                </button>
              </div>
            );
          })}
        </div>
      </StickyBar>

      <div style={{ marginTop: 16 }}>
      {loading && <SkeletonPanel />}

      {error && !loading && (
        <EmptyState icon={<AlertTriangle size={32} strokeWidth={1.5} color={T.gold} />} title="Couldn't load Social Content" sub={error}
          action={<Btn variant="secondary" onClick={load}>Try again</Btn>} />
      )}

      {!loading && !error && active && (
        <ChannelPanel
          channel={active} postsPerWeek={data!.posts_per_week} onPicked={load} writer={writer}
        />
      )}
      </div>

      <WriterPanel key={panel?.requestId ?? "closed"} target={panel} onClose={closeWriter} onBackgroundPiece={watchDetached} />
    </Card>
  );
}

// ── Skeleton (FE audit #1) ──────────────────────────────────────────────────
// Shaped like the real content (a counts line + a few SubjectRow-shaped cards), shown in the
// active panel's own place — the tab strip above it is never hidden or replaced.

function SkeletonBlock({ width, height = 12 }: { width: string; height?: number }) {
  return (
    <div style={{
      width, height, borderRadius: 5, flexShrink: 0,
      background: `linear-gradient(90deg, ${T.line} 25%, ${T.line2} 50%, ${T.line} 75%)`,
      backgroundSize: "200% 100%", animation: "aa511SlateShimmer 1.5s infinite",
    }} />
  );
}

function SkeletonRow() {
  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
      padding: "12px 14px", borderRadius: 10, border: `1px solid ${T.line}`, background: T.card,
    }}>
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 8, minWidth: 0 }}>
        <SkeletonBlock width="55%" height={14} />
        <SkeletonBlock width="75%" height={11} />
      </div>
      <SkeletonBlock width="96px" height={28} />
    </div>
  );
}

function SkeletonPanel() {
  return (
    <div>
      <SkeletonBlock width="220px" height={12} />
      <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 14 }}>
        <SkeletonRow />
        <SkeletonRow />
        <SkeletonRow />
      </div>
    </div>
  );
}

function ChannelPanel({ channel, postsPerWeek, onPicked, writer }: {
  channel: ChannelSlate; postsPerWeek: number; onPicked: () => void; writer: WriterControls;
}) {
  const proposed = channel.subjects.filter(s => s.state === "proposed");
  const decided = channel.subjects.filter(s => s.state !== "proposed");

  return (
    <div>
      <div style={{ fontSize: 12, color: T.muted, marginBottom: 14 }}>
        <strong style={{ color: T.ink, fontFamily: mono }}>{channel.eligible_count}</strong> eligible subject{channel.eligible_count === 1 ? "" : "s"}
        {!channel.on_demand && (
          <> · target rhythm <strong style={{ color: T.ink, fontFamily: mono }}>{postsPerWeek}</strong> posts/week</>
        )}
        {channel.on_demand && <> · written on demand</>}
      </div>

      {channel.subjects.length === 0 ? (
        // Never had any Subject at all — the original "atomize a tour first" guidance.
        <EmptyState icon={<ClipboardList size={32} strokeWidth={1.5} color={T.gold} />} title="Nothing here yet"
          sub="No Segment or Route on this tenant has cleared this Channel's bar yet — atomize and rank a tour first." />
      ) : proposed.length === 0 ? (
        // FE audit #3 — DIFFERENT message: this Channel has decided history, just nothing NEW
        // to pick right now. Conflating this with the "never had any Subject" case above was the
        // reported gap — a tenant seeing a bare gap above "Already decided" with no explanation.
        <>
          <EmptyState icon={<CheckCircle2 size={32} strokeWidth={1.5} color={T.gold} />} title="No new eligible Subjects right now"
            sub="Review the Subjects already decided below, or rewrite/atomize more tours for new proposals." />
          <DecidedList subjects={decided} onPicked={onPicked} writer={writer} />
        </>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {proposed.map(s => (
            <SubjectRow key={s.subject_id} subject={s} onPicked={onPicked} writer={writer} />
          ))}
          {decided.length > 0 && <DecidedList subjects={decided} onPicked={onPicked} writer={writer} />}
        </div>
      )}
    </div>
  );
}

function DecidedList({ subjects, onPicked, writer }: {
  subjects: SlateSubject[]; onPicked: () => void; writer: WriterControls;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 10 }}>
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", color: T.muted2, margin: "10px 0 2px" }}>
        Already decided
      </div>
      {subjects.map(s => (
        <SubjectRow key={s.subject_id} subject={s} onPicked={onPicked} writer={writer} />
      ))}
    </div>
  );
}

function barReasonText(reason: ClearedBarReason): string {
  if (reason.on_demand) return "On demand — no threshold applies";
  const parts: string[] = [];
  if (reason.needs_demand > 0) {
    parts.push(`Demand ${reason.demand?.toLocaleString() ?? 0} ≥ ${reason.needs_demand.toLocaleString()}`);
  }
  if (reason.needs_questions > 0) {
    parts.push(`Questions ${reason.questions} ≥ ${reason.needs_questions}`);
  }
  if (reason.needs_said > 0) {
    parts.push(`Said ${reason.said} chars ≥ ${reason.needs_said}`);
  }
  return parts.length > 0 ? parts.join(" · ") : "No threshold applies";
}

function SubjectRow({ subject, onPicked, writer }: {
  subject: SlateSubject; onPicked: () => void; writer: WriterControls;
}) {
  const [picking, setPicking] = useState(false);
  const [pickError, setPickError] = useState<string | null>(null);
  const [cutting, setCutting] = useState(false);
  const [cutError, setCutError] = useState<string | null>(null);

  const title = subject.route_id
    ? (subject.hub_name ?? "Untitled journey")
    : [subject.place, subject.action].filter(Boolean).join(" — ") || "Untitled moment";

  const kindLabel = subject.route_id ? "Route" : "Segment";
  // AA-668 — kept the AA-556 name: "this row's writer is open".
  const isExpanded = writer.openSubjectId === subject.subject_id;
  const knownTarget = writer.known[subject.subject_id] ?? null;
  const { open: openWriter } = writer;

  const pick = useCallback(() => {
    setPicking(true);
    setPickError(null);
    fetch(`/api/tenant/v1/subjects/${subject.subject_id}/pick`, { method: "POST" })
      .then(async r => {
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw body;
        return body as { request_id: string };
      })
      .then(d => {
        // FE audit #2 — only a CONFIRMED 200 clears the error and refreshes the Slate. Reaching
        // here means the backend really did flip this Subject to 'picked', so both are safe.
        // AA-668 — opens the Writer panel on the new request (was: AA-564 4.2's inline
        // accordion; before that, navigation to the standalone T8 route).
        setPickError(null);
        setPicking(false);
        onPicked();
        openWriter({
          subjectId: subject.subject_id, requestId: d.request_id, title,
          channel: subject.channel, kind: kindLabel,
        });
      })
      .catch(e => {
        // FE audit #2 (the important fix) — a failed request (4xx/5xx, or the browser giving up
        // on a real network timeout) must NOT call onPicked(). The old code refreshed here
        // unconditionally, which could show this exact error message sitting right next to a
        // "picked" badge if the backend had actually succeeded before the client gave up waiting
        // on the response. Leaving the Slate untouched means the row keeps showing "Pick to write"
        // (its last KNOWN state) alongside the error — never a state the tenant never asked for.
        setPickError(e?.detail ?? "Couldn't pick this Subject — try again.");
        setPicking(false);
      });
  }, [subject.subject_id, subject.channel, title, kindLabel, onPicked, openWriter]);

  // AA-556 — one-way action (no "un-cut"), so a confirm dialog gates every call. Disabled while
  // `picking` or `isExpanded` (see file header) — purely to avoid a same-row race that would just
  // 409 harmlessly, not a data-safety guard (the backend already re-checks live DB state).
  const cut = useCallback(() => {
    if (!window.confirm("Cut this proposal? This cannot be undone.")) return;
    setCutting(true);
    setCutError(null);
    fetch(`/api/tenant/v1/subjects/${subject.subject_id}/cut`, { method: "POST" })
      .then(async r => {
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw body;
        return body;
      })
      .then(() => {
        setCutError(null);
        onPicked();
      })
      .catch(e => {
        setCutError(e?.detail ?? "Couldn't cut this Subject — try again.");
        setCutting(false);
      });
  }, [subject.subject_id, onPicked]);

  const stateVariant = subject.state === "proposed" ? "gold"
    : subject.state === "picked" ? "info"
    : subject.state === "used" ? "success" : "default";

  return (
    <div style={{
      borderRadius: 10, border: `1px solid ${isExpanded ? T.gold : T.line}`, background: T.card,
      overflow: "hidden",
    }}>
      <div style={{
        display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12,
        padding: "12px 14px",
      }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4, flexWrap: "wrap" }}>
            <span style={{ fontFamily: serif, fontSize: 14.5, fontWeight: 500, color: T.ink }}>{title}</span>
            <Badge variant="default">{kindLabel}</Badge>
            {subject.state !== "proposed" && <Badge variant={stateVariant}>{subject.state}</Badge>}
            {subject.score != null && (
              <span style={{ fontFamily: mono, fontSize: 10.5, color: T.muted2 }} title="Rank-sum — lower is better">
                rank {subject.score}
              </span>
            )}
          </div>
          <div style={{ fontSize: 11.5, color: T.muted, lineHeight: 1.5 }}>
            {barReasonText(subject.cleared_bar_reason)}
          </div>
          {pickError && (
            <div style={{
              marginTop: 6, padding: "6px 8px", background: T.redSoft, border: `1px solid ${T.redBorder}`,
              borderRadius: 6, fontSize: 11, color: T.red, display: "flex", alignItems: "center", gap: 8,
            }}>
              <span>{pickError}</span>
              {/* Manual, explicit refresh — never automatic (FE audit #2) — so the tenant can
                  check the real state on their own terms instead of an auto-refresh that could
                  have shown this same error next to a row that actually succeeded. */}
              <button onClick={onPicked} style={{
                background: "none", border: "none", padding: 0, cursor: "pointer",
                color: T.red, fontFamily: sans, fontSize: 11, fontWeight: 700, textDecoration: "underline",
                flexShrink: 0,
              }}>
                Refresh
              </button>
            </div>
          )}
          {/* AA-556 — same shape as pickError above, own Refresh (cut() failing never calls
              onPicked() itself, same FE-audit-#2 "only a confirmed success refetches" rule). */}
          {cutError && (
            <div style={{
              marginTop: 6, padding: "6px 8px", background: T.redSoft, border: `1px solid ${T.redBorder}`,
              borderRadius: 6, fontSize: 11, color: T.red, display: "flex", alignItems: "center", gap: 8,
            }}>
              <span>{cutError}</span>
              <button onClick={onPicked} style={{
                background: "none", border: "none", padding: 0, cursor: "pointer",
                color: T.red, fontFamily: sans, fontSize: 11, fontWeight: 700, textDecoration: "underline",
                flexShrink: 0,
              }}>
                Refresh
              </button>
            </div>
          )}
        </div>
        {subject.state === "proposed" && (
          <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
            <Btn variant="primary" size="sm" disabled={picking} onClick={pick}>
              {picking ? "Picking…" : <><Sparkles size={12} /> Pick to write <ChevronRight size={12} /></>}
            </Btn>
            {/* AA-556 — disabled while picking/isExpanded/cutting: UX polish only (see file
                header) — the backend already 409s a stale/racing cut safely on its own. */}
            <Btn variant="danger" size="sm" disabled={picking || isExpanded || cutting} onClick={cut}>
              {cutting ? "Cutting…" : <><X size={12} /> Cut</>}
            </Btn>
          </div>
        )}
        {/* AA-668 — a Subject picked earlier in this visit reopens its Writer panel. */}
        {subject.state !== "proposed" && knownTarget && (
          <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
            <Btn variant="secondary" size="sm" disabled={isExpanded} onClick={() => openWriter(knownTarget)}>
              <Sparkles size={12} /> Continue <ChevronRight size={12} />
            </Btn>
          </div>
        )}
      </div>
    </div>
  );
}
