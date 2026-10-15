"use client";
// app/(tenant)/portal/_components/AngleGateWizard.tsx — AA-449 (T8 Angle Gate) + AA-450 (T9 Write
// + T10-inline quality check), ONE continuous wizard, Goal -> Angle -> Write.
//
// AA-564 Nhóm 4.2 (2026-09-08) — extracted out of AngleGateTab.tsx (which is now a thin wrapper,
// see that file) so SlateTab.tsx can render this SAME wizard embedded inline under a Subject row
// (accordion, one row expanded at a time) instead of only as the standalone /portal/t8-angle-gate
// page. Two behavior differences from the original, both gated on `embedded`:
//   - `requestId` is a prop instead of read from useSearchParams() directly (the standalone page
//     wrapper still reads the query param — see AngleGateTab.tsx — and passes it down).
//   - `reset()` ("Start over") called `onReset?.()` to collapse the row instead of
//     `router.push("/portal/t8-angle-gate")` when embedded (AA-668 removed both: the Writer panel
//     has no "Start over", closing the panel is the way out).
// Everything else (the full state machine, every fetch call, every card) is unchanged from the
// pre-4.2 AngleGateTab.tsx — this is a mechanical extraction, not a rewrite.
//
// AA-668 (S237) — `embedded` now means "inside the Writer panel" (WriterPanel.tsx, a kit Drawer
// opened from the Slate) instead of the inline accordion under a Subject row. In that mode the
// panel owns the context header (so SubjectHeader is hidden), the close action (so there is no
// "Start over" button — closing the panel leaves the request and any running write alone) and
// the finished-piece view + actions (so the "Content ready" line and WriteDonePopup are not
// rendered; the panel reads the piece through `onPieceChange`). In both modes: Goal is a grid of
// cards, and each Angle card carries its own "Write with this angle" button — one click chooses
// the angle and the write starts at once (it replaces the AA-469 pick-then-confirm pair).
//
// Per ADR-2026-038 §0.2/§10.3 (tenant self-service — AA does not gate tenant content; the T8
// "gate" is the TENANT choosing, never AA): "Goal" is always the 8-value list (Bang 1), "Angle"
// is always the 3 LLM-generated options per request.
//
// Real back-navigation exists for exactly ONE step, because it's the only one the backend
// supports (AA-497's reopen_request(), approved -> reusable): from the Write card, "Change angle"
// reopens the SAME request and rewinds to the angle-choice card, no new LLM call. Channel has no
// dependency on which angle was picked (T9's write prompt applies channel style independently of
// the angle's own best_final_style) and is fixed at creation anyway (AA-522), so there's nothing
// to re-ask there.
//
// Full workflow:
//   1. Slate (SlateTab.tsx) picks a Subject -> creates the request (subject_id + channel already
//      set) -> either navigates to /portal/t8-angle-gate?resume_request_id= (standalone) or, as
//      of AA-564 4.2, expands this wizard inline under the Subject row (embedded).
//   2. Tenant picks a Goal from the 8-value list.
//   3. Backend auto-applies fixed brand audience + formula, generates 3 angles, recommends one.
//   4. Tenant picks one of the 3 (recommended or not) — the real gate. status -> approved.
//      AA-668: one button per angle card ("Write with this angle") — no separate confirm.
//   5. AUTOMATICALLY: the write-step effect below fires T9's write the instant an angle is
//      approved. The server fills a NULL request CTA from the brand's default CTA (AA-771); only
//      when the brand has none does write() return 422 and the inline CTA field appear (AA-668).
//   6. ONE loading state while T9 writes + T10 checks (up to 2 attempts, inline) -> final result.
//
// API (via /api/tenant proxy -> Authorization: Bearer <cis_tenant_token>, tenant_id always
// resolved from the JWT):
//   GET  /api/tenant/v1/angle-gate/goals
//   GET  /api/tenant/v1/angle-gate/requests/{id}
//   POST /api/tenant/v1/angle-gate/requests/{id}/goal              {goal}
//   POST /api/tenant/v1/angle-gate/requests/{id}/choose            {idx}
//   POST /api/tenant/v1/angle-gate/requests/{id}/reopen             {}     — AA-497, "Change angle"
//   POST /api/tenant/v1/content-writing/requests/{id}/write         {cta?}  — AA-450/466, 202 +
//                                                                     'processing' placeholder,
//                                                                     poll GET .../pieces/{id}
//   GET  /api/tenant/v1/content-writing/pieces/{id}                 — AA-466 poll target
//   GET  /api/tenant/v1/content-writing/requests/{id}/latest-piece  — AA-522, resume support: the
//                                                                     latest piece for THIS
//                                                                     request's currently-chosen
//                                                                     angle, or {piece: null}.

import { useState, useEffect, useCallback, useRef } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Sparkles, CheckCircle2, ChevronRight, RotateCcw, AlertTriangle, ExternalLink, Compass } from "lucide-react";
import { T, serif, sans, mono, Card, CardHead, Badge, Btn, LoadingScreen, EmptyState, Spinner } from "./ui";
import LiveWriter from "./LiveWriter";

const POLL_INTERVAL_MS = 3000;
const POLL_CEILING_MS = 180_000;

interface Goal {
  key: string;
  name: string;
  description: string;
  logic: string;
  marketing_term: string;
}

interface AngleOption {
  idx: number;
  name: string;
  why_it_works: string;
  formula_fit: string;
  best_final_style: string;
  recommended: boolean;
  chosen: boolean;
  // AA-512 — measurable-ranking evidence (services/acp_angle_gate/ranking.py). Both null
  // together = never ranked (a pre-AA-512 legacy row) — the badge row is simply omitted then.
  answers: string[] | null;
  violations: string[] | null;
}

interface AngleGateRequest {
  request_id: string;
  atom_id: string;
  channel: string | null; // AA-522 — always set from creation now (fixed by the Subject); a
  // legacy pre-AA-522 row is the only way this is ever null.
  goal: string | null;
  cta: string | null; // AA-450 migration 114 — realistically NULL for every real request today.
  // AA-668: NULL is fine — the server falls back to the brand's default_cta (AA-771).
  // AA-497 — "reusable" is an approved request "Change angle" just reopened
  // (services/acp_angle_gate/service.py::reopen_request()): same "pick 1 of 3 already-generated
  // angles" UI as "pending_choice" below, choose() is unchanged either way.
  status: "pending_goal" | "pending_choice" | "approved" | "reusable";
  // AA-764 — angles are generated by a background job: `generating` while it runs (status stays
  // pending_goal), `generation_failed` when it ended without angles (choose a goal again).
  generating?: boolean;
  generation_failed?: boolean;
  angles: AngleOption[];
  // AA-512 — the real PAA pool this request's angles were ranked against (AA-501 migration 127,
  // snapshotted at set_goal_and_generate() time) — only used here for the badge's denominator
  // ("answers X/Y"), Y = people_also_ask.length.
  dfs_paa_snapshot: { people_also_ask: string[] } | null;
  // AA-512 — fixed header (Subject + Channel, "không sửa được ở đây"). subject_id null = a
  // legacy pre-AA-522 row (the removed atom-picker path).
  subject_id: string | null;
  subject_score: number | null;
  subject_place: string | null;
  subject_action: string | null;
  subject_hub_name: string | null;
}

// AA-450/AA-613/AA-614 — mirrors the tenant-safe piece shape every tenant-facing read now
// returns (services/acp_content_writing/service.py::_tenant_safe_piece / the 202 write placeholder
// / fetch_piece / fetch_latest_piece_for_request). The backend deliberately no longer exposes the
// raw `status` / `held_reason` / `gate_ledger` to the tenant — those are internal/admin-only. The
// FE only ever needs `ready_state`:
//   - "in_progress": T9 is still writing (the 202 placeholder, or a resumed in-flight piece).
//   - "ready": there's real content to use — approved OR held. A held piece is fully delivered
//     to the tenant here (My Content shows it exactly like approved, AA-613 #7); only its PUBLISH
//     is gated server-side (v1_publish 422, #8). The tenant never sees WHY it was held.
//   - "not_ready": a hard failure produced no content (retry).
export interface ContentPiece {
  piece_id: string;
  angle_gate_request_id: string;
  channel?: string | null;
  content_text: string | null;
  ready_state: "in_progress" | "ready" | "not_ready";
}

// AA-522 — 3 steps only now (Atom and Channel are gone with Luồng B).
type Step = 1 | 2 | 3;
const STEP_LABELS: [Step, string][] = [[1, "Goal"], [2, "Angle"], [3, "Write"]];
const ANGLE_STEP: Step = 2;

function currentStep(req: AngleGateRequest | null): Step {
  if (!req) return 1;
  if (req.status === "pending_goal") return 1;
  if (req.status === "pending_choice" || req.status === "reusable") return 2;
  return 3; // approved
}

// AA-512 — fixed, non-editable header shown above the Stepper (Linear: "Header cố định hiện
// Subject + Channel đã chọn, không sửa được ở đây"). Renders nothing for a legacy pre-AA-522 row
// with no subject_id.
function SubjectHeader({ req }: { req: AngleGateRequest }) {
  const place = req.subject_place ?? req.subject_hub_name;
  const detail = req.subject_place && req.subject_action ? `${req.subject_place} — ${req.subject_action}` : place;
  return (
    <div style={{ padding: "10px 14px", background: T.bg, border: `1px solid ${T.line2}`, borderRadius: 8, fontFamily: sans }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
        <span style={{ fontSize: 12.5, fontWeight: 700, color: T.ink, textTransform: "capitalize" }}>
          Writing for: {req.channel}
        </span>
        {req.subject_score !== null && <Badge variant="default">Score {req.subject_score}</Badge>}
      </div>
      {detail && <div style={{ fontSize: 12, color: T.muted, marginTop: 4 }}>{detail}</div>}
    </div>
  );
}

// AA-512 — the 2 measurable badges on an angle card. Omitted entirely when this angle was never
// ranked (answers/violations both null — a pre-AA-512 legacy row).
function AngleRankingBadges({ angle, paaTotal }: { angle: AngleOption; paaTotal: number }) {
  if (angle.answers === null || angle.violations === null) return null;
  const n = angle.violations.length;
  return (
    <div style={{ display: "flex", gap: 8, marginTop: 8, paddingTop: 8, borderTop: `1px solid ${T.line2}` }}>
      <span
        title={angle.answers.length ? angle.answers.join("; ") : "No PAA data for this request"}
        style={{ fontSize: 11.5, color: T.muted, fontFamily: sans }}
      >
        {paaTotal > 0 ? `✓ answers ${angle.answers.length}/${paaTotal} PAA questions` : "no PAA data"}
      </span>
      <span
        title={angle.violations.join("; ") || "No avoid-list phrases matched"}
        style={{ fontSize: 11.5, fontFamily: sans, color: n > 0 ? "#8A5A16" : T.muted }}
      >
        {n > 0 ? `⚠ ${n} avoid-list hit${n === 1 ? "" : "s"}` : "0 avoid-list violations"}
      </span>
    </div>
  );
}

// Mirrors SlotPickerPanel.tsx's Breadcrumb — same visual language (chevron-separated, active
// crumb bold, past crumbs dim + checked), but only the "2 Angle" crumb is ever clickable, and
// only from step 3, because reopen_request() is the only backend endpoint that actually supports
// jumping back a step.
function Stepper({ step, canChangeAngle, onChangeAngle }: {
  step: Step; canChangeAngle: boolean; onChangeAngle: () => void;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", padding: "8px 0" }}>
      {STEP_LABELS.map(([n, label], i) => {
        const done = n < step;
        const active = n === step;
        const clickable = n === ANGLE_STEP && step > ANGLE_STEP && canChangeAngle;
        const crumbStyle: React.CSSProperties = {
          display: "inline-flex", alignItems: "center", gap: 4, fontFamily: sans, fontSize: 12.5,
          fontWeight: active ? 700 : 500, color: active ? T.ink : done ? T.muted : T.muted2,
          background: "none", border: "none", padding: 0, cursor: clickable ? "pointer" : "default",
        };
        const content = <>{done && <CheckCircle2 size={12} color={T.green} />} {n} · {label}</>;
        return (
          <span key={n} style={{ display: "flex", alignItems: "center", gap: 6 }}>
            {i > 0 && <ChevronRight size={13} color={T.muted2} />}
            {clickable
              ? <button onClick={onChangeAngle} style={crumbStyle} title="Change angle — picks from the same 3 already-generated options, no new content generated">{content}</button>
              : <span style={crumbStyle}>{content}</span>}
          </span>
        );
      })}
    </div>
  );
}

// AA-569 — replaces the old inline result card (which showed full content_text +
// attempt_number + raw held_reason, right here in the wizard). This popup confirms the write
// finished and links to My Content (ReviewList.tsx) — deliberately no content preview, no
// status-specific gate/technical wording, same regardless of approved/held, per Nghiệp's
// explicit build decision (AA-569).
function WriteDonePopup({ onClose, onOpenInMyContent }: {
  onClose: () => void; onOpenInMyContent: () => void;
}) {
  return (
    <div
      style={{ position: "fixed", inset: 0, background: "rgba(31,41,51,0.45)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000 }}
      onClick={onClose}
    >
      <div
        style={{ background: "#fff", borderRadius: 12, padding: "28px 26px", maxWidth: 360, width: "90%", boxShadow: "0 12px 40px rgba(0,0,0,0.25)", textAlign: "center", fontFamily: sans }}
        onClick={e => e.stopPropagation()}
      >
        <div style={{ display: "flex", justifyContent: "center", marginBottom: 12 }}>
          <CheckCircle2 size={36} color={T.green} />
        </div>
        <div style={{ fontFamily: serif, fontSize: 17, fontWeight: 600, color: T.ink, marginBottom: 6 }}>
          Your content is ready
        </div>
        <p style={{ fontSize: 12.5, color: T.muted, lineHeight: 1.5, margin: "0 0 20px" }}>
          Review, edit, or export it any time from My Content.
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <Btn variant="primary" onClick={onOpenInMyContent}><ExternalLink size={13} /> Open in My Content</Btn>
          <Btn variant="ghost" onClick={onClose}>Stay here</Btn>
        </div>
      </div>
    </div>
  );
}

export default function AngleGateWizard({ requestId, embedded = false, onPieceChange, onDetachedPiece }: {
  requestId: string | null;
  embedded?: boolean;
  // AA-668 — the Writer panel's view of the current piece (null = none yet / cleared). The panel
  // renders the finished piece + its actions, and keeps watching an in-progress piece after the
  // tenant closes it. Must be a stable callback (a useState setter or useCallback).
  onPieceChange?: (piece: ContentPiece | null) => void;
  // AA-668 — a write the tenant started (angle clicked) whose 202 placeholder (or in-progress
  // latest piece) arrives AFTER this wizard unmounted, i.e. the Writer panel was closed in the
  // ~1-2 s between the click and the write call answering. The job runs either way; this hands
  // the piece to the Slate's background watcher so the "is ready" toast still fires.
  onDetachedPiece?: (piece: ContentPiece) => void;
}) {
  const router = useRouter();

  const [goals, setGoals] = useState<Goal[]>([]);
  // AA-522 — only gates the initial paint while a requestId is actually being resolved; with
  // none given there's nothing to wait for, the empty state below renders immediately.
  const [initialLoading, setInitialLoading] = useState(!!requestId);

  const [selectedGoal, setSelectedGoal] = useState("");

  const [req, setReq] = useState<AngleGateRequest | null>(null);
  const [generating, setGenerating] = useState(false);
  const [choosing, setChoosing] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  // AA-497 — "Change angle" (step 3 -> back to step 2), see changeAngle() below.
  const [reopening, setReopening] = useState(false);
  const [reopenError, setReopenError] = useState<string | null>(null);

  // AA-450 — write + inline T10 check. AA-466: `writing` spans the whole 202+poll cycle.
  const [piece, setPiece] = useState<ContentPiece | null>(null);
  const [writing, setWriting] = useState(false);
  const [writeError, setWriteError] = useState<string | null>(null);
  const [needsCtaInput, setNeedsCtaInput] = useState(false);
  const [ctaInput, setCtaInput] = useState("");
  const [pollTimedOut, setPollTimedOut] = useState(false); // 180s poll ceiling hit, NOT a failure
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // AA-668 — false once this wizard unmounted (Writer panel closed). A fetch still in flight at
  // that moment must not start a poll interval nobody will ever clear (the unmount cleanup has
  // already run); a just-started write goes to onDetachedPiece instead.
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  // AA-569 — only true right after a write this session just finished (set inside pollPiece's
  // success branch below), never when resuming an already-finished piece via the latest-piece
  // effect further down — a tenant revisiting a done request shouldn't get a popup for nothing
  // that just happened.
  const [showDonePopup, setShowDonePopup] = useState(false);

  // AA-522 — tracks which request_id the write-step effect below has already resolved (fetched
  // latest-piece for, and either restored it or decided to auto-write/ask-for-CTA), so it runs
  // exactly once per real "arrival at the write step" rather than re-firing on every setReq() no-
  // op. Reset in changeAngle()'s success handler so a reopen+re-choose cycle gets a fresh check.
  const resolvedWriteStepFor = useRef<string | null>(null);

  useEffect(() => {
    fetch("/api/tenant/v1/angle-gate/goals")
      .then(r => (r.ok ? r.json() : { goals: [] }))
      .then(d => setGoals(d.goals ?? []))
      .catch(() => {});
  }, []);

  // AA-764 — POST .../goal returns 202 while a t8_angles job generates the angles; poll the
  // request until it leaves `generating` (same 3 s interval / 180 s ceiling as the piece poll).
  const anglePollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const stopAnglePoll = useCallback(() => {
    if (anglePollRef.current) { clearInterval(anglePollRef.current); anglePollRef.current = null; }
  }, []);
  const settleAngles = useCallback((d: AngleGateRequest) => {
    setReq(d);
    if (d.generating) return false;
    setGenerating(false);
    if (d.generation_failed) setError("Couldn't generate angles — please choose a goal and try again.");
    return true;
  }, []);
  const pollAngles = useCallback((requestId: string) => {
    if (!aliveRef.current) return; // unmounted — the t8_angles job finishes server-side anyway
    stopAnglePoll();
    setGenerating(true);
    const startTime = Date.now();
    anglePollRef.current = setInterval(async () => {
      if (Date.now() - startTime > POLL_CEILING_MS) {
        stopAnglePoll();
        setGenerating(false);
        setError("Angles are taking longer than usual — refresh this page in a minute.");
        return;
      }
      try {
        const r = await fetch(`/api/tenant/v1/angle-gate/requests/${requestId}`);
        if (!r.ok) return; // transient — the job keeps running either way
        if (settleAngles(await r.json())) stopAnglePoll();
      } catch { /* transient network error — keep polling */ }
    }, POLL_INTERVAL_MS);
  }, [stopAnglePoll, settleAngles]);
  useEffect(() => stopAnglePoll, [stopAnglePoll]); // cleanup on unmount

  // AA-497/AA-522 — load the resumed request. Loads straight into whatever card its real status
  // calls for (goal / angle-choice / write) — no separate "resume" branch needed, the per-status
  // cards below already cover every value the API can return.
  useEffect(() => {
    if (!requestId) return;
    fetch(`/api/tenant/v1/angle-gate/requests/${requestId}`)
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then((d: AngleGateRequest) => {
        setReq(d);
        // AA-764 — resumed while the angle job is still running: keep showing progress.
        if (d.generating) { if (d.goal) setSelectedGoal(d.goal); pollAngles(d.request_id); }
      })
      .catch(e => setError(e.detail ?? "Couldn't load that request — try again from Social Content."))
      .finally(() => setInitialLoading(false));
  }, [requestId, pollAngles]);

  const submitGoal = useCallback(() => {
    if (!req || !selectedGoal) return;
    setGenerating(true); setError(null);
    fetch(`/api/tenant/v1/angle-gate/requests/${req.request_id}/goal`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal: selectedGoal }),
    })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then((d: AngleGateRequest) => { if (!settleAngles(d)) pollAngles(d.request_id); })
      .catch(e => { setGenerating(false); setError(e.detail ?? "Couldn't generate angles — try again."); });
  }, [req, selectedGoal, settleAngles, pollAngles]);

  const stopPolling = useCallback(() => {
    if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
  }, []);

  // AA-466 — single-piece poll for the 202 placeholder.
  const pollPiece = useCallback((pieceId: string) => {
    if (!aliveRef.current) return; // unmounted — see aliveRef; the t9_write job keeps running
    stopPolling();
    const startTime = Date.now();
    pollingRef.current = setInterval(async () => {
      if (Date.now() - startTime > POLL_CEILING_MS) {
        stopPolling();
        setWriting(false);
        setPollTimedOut(true);
        return;
      }
      try {
        const r = await fetch(`/api/tenant/v1/content-writing/pieces/${pieceId}`);
        if (!r.ok) return; // transient — next tick may succeed, backend is still working either way
        const fresh: ContentPiece = await r.json();
        if (fresh.ready_state !== "in_progress") {
          stopPolling();
          setPiece(fresh);
          setWriting(false);
          // AA-569/AA-614 — the ONLY place this fires: a write that started THIS session just
          // reached a terminal outcome. "ready" (approved OR held — both have real content to
          // review) shows the done popup; "not_ready" (a hard failure) keeps its own inline retry
          // state below instead — nothing to "review in My Content" for that case.
          if (fresh.ready_state === "ready" && !embedded) setShowDonePopup(true);
        }
      } catch { /* transient network error — keep polling, don't surface as a failure */ }
    }, POLL_INTERVAL_MS);
  }, [stopPolling, embedded]);

  useEffect(() => stopPolling, [stopPolling]); // cleanup on unmount

  // AA-668 — keep the Writer panel's copy of the piece in step (finished view, background watch).
  useEffect(() => { onPieceChange?.(piece); }, [piece, onPieceChange]);

  // AA-450 — `cta` only ever overrides a NULL angle_gate_request.cta. AA-466: POST returns 202 +
  // a 'processing' placeholder immediately — the real result comes from polling.
  const writeContent = useCallback((requestId: string, cta?: string) => {
    setWriting(true); setWriteError(null); setNeedsCtaInput(false); setPollTimedOut(false);
    fetch(`/api/tenant/v1/content-writing/requests/${requestId}/write`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cta ? { cta } : {}),
    })
      .then(async r => {
        if (r.status === 422) {
          // MissingCTAError — the request has no CTA AND the brand has no default CTA (the
          // server tries both first, AA-771). AA-668: this is now the ONLY path that asks the
          // tenant for a CTA — "ask, don't fabricate", inline, not a separate step.
          setWriting(false);
          setNeedsCtaInput(true);
          return null;
        }
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          throw new Error(err.detail ?? "Couldn't write content — try again.");
        }
        return r.json();
      })
      .then((placeholder: ContentPiece | null) => {
        if (!placeholder) return; // 422 branch above already handled its own state
        if (!aliveRef.current) { onDetachedPiece?.(placeholder); return; }
        setPiece(placeholder);
        pollPiece(placeholder.piece_id);
      })
      .catch(e => {
        setWriting(false);
        setWriteError(e instanceof Error ? e.message : "Couldn't write content — try again.");
      });
  }, [pollPiece, onDetachedPiece]);

  // AA-522 — the ONE place that decides what the write step should show, whether arrived at via
  // a fresh choose(), a reload/resume, or a "Change angle" re-choice: ask the server for the
  // latest content_piece under the currently-chosen angle (GET .../latest-piece). This is the
  // actual fix for this issue's bug — the old code trusted local React state (`needsCtaInput`)
  // that a reload silently wiped, leaving the tenant stuck on an empty Write card with no CTA
  // form and no button. Runs once per request_id (resolvedWriteStepFor ref guard); changeAngle()
  // resets that ref so a re-choice gets a fresh check instead of showing the PREVIOUS angle's
  // stale piece (the backend query is itself also scoped to the current chosen option, as a
  // second layer of protection against that).
  useEffect(() => {
    if (!req || req.status !== "approved") return;
    if (resolvedWriteStepFor.current === req.request_id) return;
    resolvedWriteStepFor.current = req.request_id;

    fetch(`/api/tenant/v1/content-writing/requests/${req.request_id}/latest-piece`)
      .then(r => (r.ok ? r.json() : { piece: null }))
      .then(({ piece: latest }: { piece: ContentPiece | null }) => {
        if (latest) {
          if (!aliveRef.current) {
            if (latest.ready_state === "in_progress") onDetachedPiece?.(latest);
            return;
          }
          setPiece(latest);
          if (latest.ready_state === "in_progress") { setWriting(true); pollPiece(latest.piece_id); }
          return;
        }
        // No piece written yet under this angle: write immediately. AA-668 — this used to ask
        // for a CTA up front whenever req.cta was NULL (every real request), even though the
        // server fills it from the brand's default CTA (AA-771). writeContent() handles the one
        // real gap — a brand with no default CTA — through the server's 422.
        writeContent(req.request_id);
      })
      .catch(() => {
        // Best-effort — worst case the tenant sees a blank Write card and can press "Retry"/
        // re-enter a CTA manually; not worth surfacing as a hard error for a resume-convenience
        // lookup.
      });
  }, [req, writeContent, pollPiece, onDetachedPiece]);

  const choose = useCallback((idx: number) => {
    if (!req) return;
    setChoosing(idx); setError(null);
    fetch(`/api/tenant/v1/angle-gate/requests/${req.request_id}/choose`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idx }),
    })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(d => setReq(d)) // AA-522 — the write-step effect above now owns firing the write call
      .catch(e => setError(e.detail ?? "Couldn't save your choice — try again."))
      .finally(() => setChoosing(null));
  }, [req]);

  // AA-497 — "Change angle" (available from step 3, status 'approved'): reopens THIS SAME
  // request (approved -> reusable, no new LLM call) and rewinds the UI to the angle-choice card.
  const changeAngle = useCallback(() => {
    if (!req) return;
    setReopening(true); setReopenError(null);
    fetch(`/api/tenant/v1/angle-gate/requests/${req.request_id}/reopen`, { method: "POST" })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(d => {
        setReq(d);
        stopPolling();
        setPiece(null); setWriteError(null); setNeedsCtaInput(false); setPollTimedOut(false);
        setShowDonePopup(false);
        resolvedWriteStepFor.current = null; // AA-522 — force a fresh latest-piece check on re-choice
      })
      .catch(e => setReopenError(e.detail ?? "Couldn't reopen — try again."))
      .finally(() => setReopening(false));
  }, [req, stopPolling]);

  // "Start over" (standalone page only) — returns to the empty state (the tenant picks a new
  // Subject from the Slate to start again). The Writer panel (embedded, AA-668) has no such
  // button: closing the panel is the way out. Confirms before wiping real progress (a goal
  // already submitted means at least one real LLM call happened).
  const reset = useCallback(() => {
    if (req && !window.confirm("Start over? This clears your current goal, angle, and write progress.")) return;
    stopPolling();
    setReq(null); setSelectedGoal(""); setError(null);
    setPiece(null); setWriteError(null); setNeedsCtaInput(false); setCtaInput("");
    setPollTimedOut(false); setReopenError(null); setShowDonePopup(false);
    resolvedWriteStepFor.current = null;
    router.push("/portal/t8-angle-gate");
  }, [req, stopPolling, router]);

  if (initialLoading) return <LoadingScreen message="Loading…" />;

  const step = currentStep(req);
  const chosenAngle = req?.angles.find(a => a.chosen) ?? null;
  // AA-668 — inside the Writer panel the drawer is the frame, so the step cards drop their own
  // border/padding instead of nesting a card in a card.
  const cardStyle: React.CSSProperties | undefined = embedded
    ? { padding: 0, border: "none", background: "transparent" }
    : undefined;

  return (
    // AA-524 — was maxWidth:720, a real outlier vs t7/t10's full-width convention (measured live:
    // 720 of ~1148px available on the standalone page, ~63% used). Bumped to 880, not removed
    // entirely — Goal/Angle cards here are prose (why_it_works/formula_fit/best_final_style), a
    // single-column reading layout, not a grid; going edge-to-edge would stretch those lines
    // well past a comfortable reading width. 880 meaningfully closes the gap (~77% used) while
    // keeping line length reasonable.
    <div style={{ display: "flex", flexDirection: "column", gap: embedded ? 16 : 20, maxWidth: embedded ? undefined : 880 }}>
      <p style={{ fontSize: 12, color: T.muted, margin: 0, lineHeight: 1.5 }}>
        Choose a content goal, then pick 1 of the 3 angles the system generates. You always
        choose — Adventure Asia never approves or blocks this for you.
      </p>

      {/* AA-668 — the Writer panel's own header already names the Subject and Channel. */}
      {req?.subject_id && !embedded && <SubjectHeader req={req} />}

      {req && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap", borderBottom: `1px solid ${T.line2}` }}>
          <Stepper step={step} canChangeAngle={req.status === "approved"} onChangeAngle={changeAngle} />
          {!embedded && <Btn variant="ghost" size="sm" onClick={reset}><RotateCcw size={12} /> Start over</Btn>}
        </div>
      )}

      {error && (
        <div style={{ padding: "9px 12px", background: T.redSoft, border: `1px solid ${T.redBorder}`, borderRadius: 8, fontSize: 12, color: T.red }}>
          {error}
        </div>
      )}

      {/* AA-522 — no more raw atom picker: every request now starts from the Slate. */}
      {!req && (
        <Card>
          <CardHead title="Pick a Subject to start" />
          <EmptyState icon={<Compass size={32} strokeWidth={1.5} color={T.gold} />} title="Nothing to write yet"
            sub="Every new piece starts from a Subject you pick in Social Content now." // AA-576 (was "Write Content always starts from there now" — stale after that nav item was removed)
            action={<Btn variant="primary" onClick={() => router.push("/portal/t7-planning")}>Go to Social Content</Btn>} />
        </Card>
      )}

      {req && req.status === "pending_goal" && (
        <Card style={cardStyle}>
          <CardHead title="1 · Choose a Goal" />
          {/* AA-668 — goal cards in a grid (2 columns in the panel / on desktop, 1 on a phone) so
              all 8 fit without scrolling. Picking a goal only selects it; angles cost an LLM call,
              so they start from the button below. */}
          <div role="radiogroup" aria-label="Content goal" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 230px), 1fr))", gap: 8 }}>
            {goals.map(g => {
              const on = selectedGoal === g.key;
              return (
                <button key={g.key} role="radio" aria-checked={on} disabled={generating}
                  data-testid={`goal-${g.key}`}
                  onClick={() => setSelectedGoal(g.key)} style={{
                    textAlign: "left", padding: "10px 14px", borderRadius: 8,
                    cursor: generating ? "default" : "pointer", minWidth: 0,
                    border: `1px solid ${on ? T.gold : T.line}`, borderWidth: on ? 2 : 1,
                    background: on ? T.goldTint : "#fff", fontFamily: sans,
                  }}>
                  <div style={{ fontSize: 13, fontWeight: 700, color: T.ink }}>{g.name}</div>
                  <div style={{ fontSize: 11.5, color: T.muted, marginTop: 2, lineHeight: 1.45 }}>{g.description}</div>
                </button>
              );
            })}
          </div>
          <div style={{ marginTop: 12, display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <Btn variant="primary" disabled={!selectedGoal || generating} onClick={submitGoal}>
              {generating ? <>Generating 3 angles…</> : <><Sparkles size={13} /> Generate 3 angles</>}
            </Btn>
            {generating && (
              <span role="status" style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 12, color: T.muted }}>
                <Spinner size={14} /> Usually under a minute{embedded ? " — you can close this panel, the angles keep generating." : "."}
              </span>
            )}
          </div>
        </Card>
      )}

      {req && (req.status === "pending_choice" || req.status === "reusable") && (
        // This card ONLY shows while actively choosing (not once approved — see the Write card's
        // meta row below for the post-choice summary). AA-668: each card has its own "Write with
        // this angle" button — one click chooses it and the write starts (no confirm step).
        <Card style={cardStyle}>
          <CardHead title={`2 · ${req.status === "reusable" ? "Choose a Different Angle" : "Choose an Angle"}`} />
          <p style={{ fontSize: 12.5, color: T.muted, margin: "0 0 14px", lineHeight: 1.5 }}>
            {req.status === "reusable"
              ? "Pick a different one of the 3 angles below — writing starts as soon as you pick."
              : "Pick one of the 3 angles below — writing starts as soon as you pick."}
          </p>
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {req.angles.map(a => {
              const busy = choosing === a.idx;
              return (
                <div key={a.idx} data-testid={`angle-${a.idx}`}
                  style={{
                    textAlign: "left", width: "100%", boxSizing: "border-box",
                    padding: "14px 16px", borderRadius: 10, position: "relative", fontFamily: sans,
                    border: `1px solid ${a.chosen ? T.green : a.recommended ? T.goldSoft : T.line}`,
                    background: a.chosen ? T.greenSoft : a.recommended ? T.goldTint : "#fff",
                  }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6, flexWrap: "wrap" }}>
                    <span style={{ fontFamily: serif, fontSize: 15, fontWeight: 600, color: T.ink }}>{a.name}</span>
                    {a.recommended && <Badge variant="default">Recommended</Badge>}
                    {a.chosen && <Badge variant="default">Currently chosen</Badge>}
                  </div>
                  <div style={{ fontSize: 12.5, color: T.body, marginBottom: 4 }}>
                    <strong>Why it works:</strong> {a.why_it_works}
                  </div>
                  <div style={{ fontSize: 12.5, color: T.body, marginBottom: 4 }}>
                    <strong>Formula fit:</strong> <span style={{ fontFamily: mono }}>{a.formula_fit}</span>
                  </div>
                  <div style={{ fontSize: 12.5, color: T.body }}>
                    <strong>Best final style:</strong> {a.best_final_style}
                  </div>
                  <AngleRankingBadges angle={a} paaTotal={req.dfs_paa_snapshot?.people_also_ask.length ?? 0} />
                  {!a.chosen && (
                    <div style={{ marginTop: 10 }}>
                      <Btn variant={a.recommended ? "primary" : "secondary"} size="sm"
                        disabled={choosing !== null} onClick={() => choose(a.idx)}>
                        {busy ? "Starting…" : <><Sparkles size={12} /> Write with this angle</>}
                      </Btn>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </Card>
      )}

      {req && req.status === "approved" && (
        <Card style={cardStyle}>
          <CardHead title="3 · Write" />

          {chosenAngle && (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, flexWrap: "wrap", marginBottom: 14, padding: "10px 12px", background: T.bg, border: `1px solid ${T.line2}`, borderRadius: 8 }}>
              <div style={{ fontSize: 12.5, color: T.body }}>
                <strong>Angle:</strong> {chosenAngle.name}
                <span style={{ color: T.muted2 }}> · </span>
                <strong>Goal:</strong> {req.goal}
                <span style={{ color: T.muted2 }}> · </span>
                <strong>Channel:</strong> {req.channel}
              </div>
              {!writing && (
                <Btn size="sm" variant="secondary" disabled={reopening} onClick={changeAngle}>
                  {reopening ? "Reopening…" : "Change angle"}
                </Btn>
              )}
            </div>
          )}

          {reopenError && (
            <div style={{ padding: "9px 12px", background: T.redSoft, border: `1px solid ${T.redBorder}`, borderRadius: 8, fontSize: 12, color: T.red, marginBottom: 10 }}>
              {reopenError}
            </div>
          )}

          {/* AA-637 — live steps + text while T9 writes; falls back to the spinner line until the
              placeholder piece id is known. */}
          {writing && (piece?.piece_id ? (
            <div style={{ marginBottom: 12 }}>
              <LiveWriter kind="piece" jobId={piece.piece_id} title="Writing your post" compact={embedded} />
            </div>
          ) : (
            <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 4px", color: T.muted, fontSize: 13 }}>
              <Spinner size={16} /> Writing and checking your content — one moment…
            </div>
          ))}

          {pollTimedOut && !writing && piece && (
            <div style={{ display: "flex", flexDirection: "column", gap: 10, padding: "9px 12px", background: T.goldTint, border: `1px solid ${T.gold}`, borderRadius: 8, marginBottom: 10 }}>
              <div style={{ fontSize: 12.5, color: T.body, lineHeight: 1.5 }}>
                Still working — this is taking longer than usual. Your content is still being
                written in the background.
              </div>
              <div>
                <Btn size="sm" variant="secondary" onClick={() => { setWriting(true); setPollTimedOut(false); pollPiece(piece.piece_id); }}>
                  Check status
                </Btn>
              </div>
            </div>
          )}

          {needsCtaInput && !writing && (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div style={{ fontSize: 12.5, color: T.body, lineHeight: 1.5 }}>
                This piece needs a call to action before it can be written — what should the
                reader do next?{" "}
                <Link href="/portal/t0-brand" style={{ color: T.gold, fontWeight: 600 }}>
                  Set a default call to action in Brand Identity
                </Link>{" "}
                so you aren&rsquo;t asked again.
              </div>
              <input value={ctaInput} onChange={e => setCtaInput(e.target.value)}
                placeholder="e.g. Book a consultation, Read the full guide…"
                style={{ padding: "9px 12px", background: "#fff", border: `1px solid ${T.line}`, borderRadius: 8, color: T.body, fontSize: 13, fontFamily: sans }} />
              <div>
                <Btn variant="primary" disabled={!ctaInput.trim()}
                  onClick={() => writeContent(req.request_id, ctaInput.trim())}>
                  <Sparkles size={13} /> Write content
                </Btn>
              </div>
            </div>
          )}

          {writeError && !writing && (
            <div style={{ padding: "9px 12px", background: T.redSoft, border: `1px solid ${T.redBorder}`, borderRadius: 8, fontSize: 12, color: T.red, marginBottom: 10 }}>
              {writeError}
            </div>
          )}

          {piece && !writing && piece.ready_state === "not_ready" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11.5, fontWeight: 700, color: T.red }}>
                <AlertTriangle size={13} /> Something went wrong while writing this content
              </div>
              <div>
                <Btn size="sm" variant="primary" onClick={() => writeContent(req.request_id)}>
                  <Sparkles size={13} /> Retry
                </Btn>
              </div>
            </div>
          )}

          {/* AA-569 — no more inline result card here (used to show full content_text +
              attempt_number + raw held_reason). Just a minimal, non-technical confirmation +
              link — same wording regardless of approved/held, real content lives in My Content
              now. WriteDonePopup (rendered at the bottom of this component) covers the "just
              finished this session" moment; this line stays visible afterward (popup dismissed,
              or a resumed already-finished request) so the card never looks empty/broken. */}
          {/* AA-668 — the Writer panel shows the finished piece and its actions itself. */}
          {piece && !writing && piece.ready_state === "ready" && !embedded && (
            <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "10px 12px", background: T.greenSoft, border: `1px solid ${T.green}`, borderRadius: 8, fontSize: 12.5, color: T.body }}>
              <CheckCircle2 size={14} color={T.green} />
              Content ready.{" "}
              <a
                onClick={() => router.push(`/portal/t10-review?piece=${piece.piece_id}`)}
                style={{ color: T.gold, fontWeight: 600, cursor: "pointer", textDecoration: "underline" }}
              >
                View, edit, or export it in My Content
              </a>
            </div>
          )}
        </Card>
      )}

      {showDonePopup && piece && (
        <WriteDonePopup
          onClose={() => setShowDonePopup(false)}
          onOpenInMyContent={() => router.push(`/portal/t10-review?piece=${piece.piece_id}`)}
        />
      )}
    </div>
  );
}
