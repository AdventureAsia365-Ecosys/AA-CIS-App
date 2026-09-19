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
//   - `reset()` ("Start over") calls `onReset?.()` to collapse the row instead of
//     `router.push("/portal/t8-angle-gate")` when embedded — SlateTab is not a route the tenant
//     should be navigated away from just to abandon an in-progress write.
// Everything else (the full state machine, every fetch call, every card) is unchanged from the
// pre-4.2 AngleGateTab.tsx — this is a mechanical extraction, not a rewrite.
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
//   5. AUTOMATICALLY: the write-step effect below fires T9's write the instant an angle is
//      approved, UNLESS the request's CTA is still NULL, in which case it asks for one first.
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
import { useRouter } from "next/navigation";
import { Sparkles, CheckCircle2, ChevronRight, RotateCcw, AlertTriangle, ExternalLink } from "lucide-react";
import { T, serif, sans, mono, Card, CardHead, Badge, Btn, LoadingScreen, EmptyState, Spinner } from "./ui";

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
  cta: string | null; // AA-450 migration 114 — realistically NULL for every real request today
  // (see this file's own header comment) — the write-step effect below asks for one when NULL.
  // AA-497 — "reusable" is an approved request "Change angle" just reopened
  // (services/acp_angle_gate/service.py::reopen_request()): same "pick 1 of 3 already-generated
  // angles" UI as "pending_choice" below, choose() is unchanged either way.
  status: "pending_goal" | "pending_choice" | "approved" | "reusable";
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
interface ContentPiece {
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

export default function AngleGateWizard({ requestId, embedded = false, onReset }: {
  requestId: string | null;
  embedded?: boolean;
  onReset?: () => void;
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

  // AA-469 Việc 4 — pick-then-confirm for the angle step: clicking a card only highlights it via
  // this local state; choose() itself isn't called until "Confirm this angle" is pressed.
  const [pendingAngleIdx, setPendingAngleIdx] = useState<number | null>(null);

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

  // AA-497/AA-522 — load the resumed request. Loads straight into whatever card its real status
  // calls for (goal / angle-choice / write) — no separate "resume" branch needed, the per-status
  // cards below already cover every value the API can return.
  useEffect(() => {
    if (!requestId) return;
    fetch(`/api/tenant/v1/angle-gate/requests/${requestId}`)
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(d => { setReq(d); setPendingAngleIdx(null); })
      .catch(e => setError(e.detail ?? "Couldn't load that request — try again from Social Content."))
      .finally(() => setInitialLoading(false));
  }, [requestId]);

  const submitGoal = useCallback(() => {
    if (!req || !selectedGoal) return;
    setGenerating(true); setError(null);
    fetch(`/api/tenant/v1/angle-gate/requests/${req.request_id}/goal`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal: selectedGoal }),
    })
      .then(async r => (r.ok ? r.json() : Promise.reject(await r.json().catch(() => ({})))))
      .then(d => { setReq(d); setPendingAngleIdx(null); })
      .catch(e => setError(e.detail ?? "Couldn't generate angles — try again."))
      .finally(() => setGenerating(false));
  }, [req, selectedGoal]);

  const stopPolling = useCallback(() => {
    if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
  }, []);

  // AA-466 — single-piece poll for the 202 placeholder.
  const pollPiece = useCallback((pieceId: string) => {
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
          if (fresh.ready_state === "ready") setShowDonePopup(true);
        }
      } catch { /* transient network error — keep polling, don't surface as a failure */ }
    }, POLL_INTERVAL_MS);
  }, [stopPolling]);

  useEffect(() => stopPolling, [stopPolling]); // cleanup on unmount

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
          // MissingCTAError — kept as a defense-in-depth fallback (the write-step effect below
          // already checks req.cta BEFORE calling write, so this shouldn't normally trigger, but
          // a stale req from a race is still possible) — same "ask, don't fabricate" behavior.
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
        setPiece(placeholder);
        pollPiece(placeholder.piece_id);
      })
      .catch(e => {
        setWriting(false);
        setWriteError(e instanceof Error ? e.message : "Couldn't write content — try again.");
      });
  }, [pollPiece]);

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
          setPiece(latest);
          if (latest.ready_state === "in_progress") { setWriting(true); pollPiece(latest.piece_id); }
          return;
        }
        // No piece written yet under this angle. If a CTA is already known, write immediately
        // (matches the old "no extra click" behavior) — otherwise ask for one up front instead
        // of waiting for write() to come back 422.
        if (req.cta) writeContent(req.request_id);
        else setNeedsCtaInput(true);
      })
      .catch(() => {
        // Best-effort — worst case the tenant sees a blank Write card and can press "Retry"/
        // re-enter a CTA manually; not worth surfacing as a hard error for a resume-convenience
        // lookup.
      });
  }, [req, writeContent, pollPiece]);

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
        setPendingAngleIdx(null); setShowDonePopup(false);
        resolvedWriteStepFor.current = null; // AA-522 — force a fresh latest-piece check on re-choice
      })
      .catch(e => setReopenError(e.detail ?? "Couldn't reopen — try again."))
      .finally(() => setReopening(false));
  }, [req, stopPolling]);

  // "Start over" — discards this request entirely. Standalone: returns to the empty state (the
  // tenant picks a new Subject from the Slate to start again). Embedded (AA-564 4.2): collapses
  // this row back closed via onReset() instead of navigating away from the Slate. Confirms before
  // wiping real progress (a goal already submitted means at least one real LLM call happened).
  const reset = useCallback(() => {
    if (req && !window.confirm("Start over? This clears your current goal, angle, and write progress.")) return;
    stopPolling();
    setReq(null); setSelectedGoal(""); setError(null);
    setPiece(null); setWriteError(null); setNeedsCtaInput(false); setCtaInput("");
    setPollTimedOut(false); setReopenError(null); setPendingAngleIdx(null); setShowDonePopup(false);
    resolvedWriteStepFor.current = null;
    if (embedded) onReset?.();
    else router.push("/portal/t8-angle-gate");
  }, [req, stopPolling, router, embedded, onReset]);

  if (initialLoading) return <LoadingScreen message="Loading…" />;

  const step = currentStep(req);
  const chosenAngle = req?.angles.find(a => a.chosen) ?? null;

  return (
    // AA-524 — was maxWidth:720, a real outlier vs t7/t10's full-width convention (measured live:
    // 720 of ~1148px available on the standalone page, ~63% used). Bumped to 880, not removed
    // entirely — Goal/Angle cards here are prose (why_it_works/formula_fit/best_final_style), a
    // single-column reading layout, not a grid; going edge-to-edge would stretch those lines
    // well past a comfortable reading width. 880 meaningfully closes the gap (~77% used) while
    // keeping line length reasonable.
    <div style={{ display: "flex", flexDirection: "column", gap: 20, maxWidth: 880 }}>
      <p style={{ fontSize: 12, color: T.muted, margin: 0, lineHeight: 1.5 }}>
        Choose a content goal, then pick 1 of the 3 angles the system generates. You always
        choose — Adventure Asia never approves or blocks this for you.
      </p>

      {req?.subject_id && <SubjectHeader req={req} />}

      {req && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap", borderBottom: `1px solid ${T.line2}` }}>
          <Stepper step={step} canChangeAngle={req.status === "approved"} onChangeAngle={changeAngle} />
          <Btn variant="ghost" size="sm" onClick={reset}><RotateCcw size={12} /> Start over</Btn>
        </div>
      )}

      {error && (
        <div style={{ padding: "9px 12px", background: T.redSoft, border: "1px solid #F5C6C6", borderRadius: 8, fontSize: 12, color: T.red }}>
          {error}
        </div>
      )}

      {/* AA-522 — no more raw atom picker: every request now starts from the Slate. */}
      {!req && (
        <Card>
          <CardHead title="Pick a Subject to start" />
          <EmptyState icon="🧭" title="Nothing to write yet"
            sub="Every new piece starts from a Subject you pick in Social Content now." // AA-576 (was "Write Content always starts from there now" — stale after that nav item was removed)
            action={<Btn variant="primary" onClick={() => router.push("/portal/t7-planning")}>Go to Social Content</Btn>} />
        </Card>
      )}

      {req && req.status === "pending_goal" && (
        <Card>
          <CardHead title="1 · Choose a Goal" />
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {goals.map(g => (
              <button key={g.key} onClick={() => setSelectedGoal(g.key)} style={{
                textAlign: "left", padding: "10px 14px", borderRadius: 8, cursor: "pointer",
                border: `1px solid ${selectedGoal === g.key ? T.gold : T.line}`,
                background: selectedGoal === g.key ? T.goldTint : "#fff",
              }}>
                <div style={{ fontSize: 13, fontWeight: 700, color: T.ink, fontFamily: sans }}>{g.name}</div>
                <div style={{ fontSize: 11.5, color: T.muted, marginTop: 2 }}>{g.description}</div>
              </button>
            ))}
            <div style={{ marginTop: 6 }}>
              <Btn variant="primary" disabled={!selectedGoal || generating} onClick={submitGoal}>
                {generating ? <>Generating 3 angles…</> : <><Sparkles size={13} /> Generate 3 angles</>}
              </Btn>
            </div>
          </div>
        </Card>
      )}

      {req && (req.status === "pending_choice" || req.status === "reusable") && (
        // This card ONLY shows while actively choosing (not once approved — see the Write card's
        // meta row below for the post-choice summary). Pick-then-confirm: clicking a card only
        // sets pendingAngleIdx (highlight); "Confirm this angle" calls choose().
        <Card>
          <CardHead title={`2 · ${req.status === "reusable" ? "Choose a Different Angle" : "Choose an Angle"}`} />
          <p style={{ fontSize: 12.5, color: T.muted, margin: "0 0 14px", lineHeight: 1.5 }}>
            {req.status === "reusable"
              ? "Pick a different one of the 3 angles below, then confirm — no new content is generated until you do."
              : "Pick one of the 3 angles below, then confirm."}
          </p>
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {req.angles.map(a => {
              const picked = pendingAngleIdx === a.idx;
              const clickable = !a.chosen;
              return (
                <button key={a.idx} disabled={!clickable}
                  onClick={() => clickable && setPendingAngleIdx(a.idx)}
                  style={{
                    textAlign: "left", width: "100%", cursor: clickable ? "pointer" : "default",
                    padding: "14px 16px", borderRadius: 10, position: "relative", fontFamily: sans,
                    border: `1px solid ${a.chosen ? T.green : picked ? T.gold : a.recommended ? T.goldSoft : T.line}`,
                    borderWidth: picked ? 2 : 1,
                    background: a.chosen ? T.greenSoft : picked ? T.goldTint : a.recommended ? T.goldTint : "#fff",
                  }}>
                  {picked && !a.chosen && <CheckCircle2 size={16} color={T.gold} style={{ position: "absolute", top: 12, right: 12 }} />}
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
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
                </button>
              );
            })}
          </div>
          <div style={{ marginTop: 14 }}>
            <Btn variant="primary" disabled={pendingAngleIdx === null || choosing !== null}
              onClick={() => pendingAngleIdx !== null && choose(pendingAngleIdx)}>
              {choosing !== null ? "Saving…" : "Confirm this angle"}
            </Btn>
          </div>
        </Card>
      )}

      {req && req.status === "approved" && (
        <Card>
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
            <div style={{ padding: "9px 12px", background: T.redSoft, border: "1px solid #F5C6C6", borderRadius: 8, fontSize: 12, color: T.red, marginBottom: 10 }}>
              {reopenError}
            </div>
          )}

          {writing && (
            <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 4px", color: T.muted, fontSize: 13 }}>
              <Spinner size={16} /> Writing and checking your content — one moment…
            </div>
          )}

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
                reader do next?
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
            <div style={{ padding: "9px 12px", background: T.redSoft, border: "1px solid #F5C6C6", borderRadius: 8, fontSize: 12, color: T.red, marginBottom: 10 }}>
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
          {piece && !writing && piece.ready_state === "ready" && (
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
