"use client";
// app/(tenant)/portal/_components/PlanningTab.tsx — T7 (tenant-facing), page title "Slate".
//
// AA-519 Việc 3 — this page used to open with a Quarter Plan preview/trip-table/finalize/history
// UI (AA-448, extended AA-469 Việc 2) ABOVE <SlateTab/>; Nghiệp's explicit call (AA-519 issue
// body) is that this was fully superseded by the Slate (AA-511) and should come out, not stay as
// dead/hidden code — removed here. See git history on this file (pre-AA-519) for that design's
// full record (API routes it called, the "edit after finalize" decision, etc.) if ever needed
// again. ReallocationSection below predates AA-511 too but was kept (scope decision posted to
// the AA-519 Linear comment before building) — not "Quarter Plan UI" in the narrow sense that
// changed here; confirmed still live (suggest_trip_reallocation() computes fresh each call, not
// dependent on the removed Preview/Finalize UI ever having run).
//
// AA-519 follow-up (same day, real-data audit posted to the Linear comment) — FeedbackSection
// (the "round 6" manual metric-entry + atom-weight rollup UI that used to render here) was
// REMOVED, not kept: confirmed genuinely dead via a real HTTP 404
// (`POST /v1/planning/metrics` against a real T9 `content_piece.piece_id` — the only kind of
// piece_id a real tenant ever has). Root cause has nothing to do with Slate/quarter — `services/
// acp_shared/content_metrics.py::record_metric_snapshot()`/`rollup_atom_weights()` query
// `acp_deliver.pieces` (N7/N8's OLD table, migration 094/096), never `acp_shared.content_piece`
// (T9's real table, migration 115) — a gap that predates AA-511 by weeks, from when AA-448's
// round-6 feedback loop was built against the wrong/older piece table and never reconciled when
// T9 shipped its own. Nghiệp's explicit call: remove the dead UI now rather than leave a feature
// tenants can see but can never successfully use; `content_metrics.py` itself is untouched — a
// real feedback loop, if built again, gets designed fresh against T9's actual schema, not patched
// onto the old one.
//
// API this file still calls (via /api/tenant proxy -> Authorization: Bearer <cis_tenant_token>,
// api/routers/v1_planning.py — tenant_id always resolved from the JWT):
//   GET  /api/tenant/v1/planning/trip-reallocation/suggest?year=&quarter=
//   POST /api/tenant/v1/planning/trip-reallocation/confirm {year, quarter, accept}
// (<SlateTab/> itself calls GET /v1/slate + POST /v1/subjects/{id}/pick — see that file.)

import { useState, useCallback } from "react";
import { T, serif, mono, Card, CardHead, Badge, Btn } from "./ui";
// AA-511 — the Slate replaces SlotPickerPanel.tsx (Weekly Slots) on this render path. The old
// component/file is deliberately left in place, unused, per the epic's own "giữ code cũ, chỉ
// ngưng dùng, không xoá" rule — see docs/claude_audit/AA-511-step0-slate-investigation.md.
import SlateTab from "./SlateTab";

// AA-519 Việc 3 — the old Quarter Plan preview/trip-table/finalize/history UI that used to render
// ABOVE <SlateTab/> on this page is REMOVED here (not hidden — Nghiệp's explicit call, "đã thay
// thế hoàn toàn bởi Slate", AA-519 issue body). That UI's own types/components
// (YearQuarterPicker/PreviewPanel/HistorySection/ScoreCell/StatBlock/TripScore/QuarterPlanPayload/
// WeekLock/PreviewResponse/HistoryVersion*) are deleted along with it, not left as dead code — see
// git history (this file, pre-AA-519) to recover if ever needed. ReallocationSection below is
// KEPT (scope decision, posted to the AA-519 Linear comment before building this): not "Quarter
// Plan UI" in the narrow sense (no trip-selection table/preview/finalize), has its own card
// heading and stands independently — and confirmed still live via a real HTTP call (see this
// file's own top-of-file comment). FeedbackSection (was here too at first) was REMOVED in the
// AA-519 follow-up round — confirmed genuinely dead (real 404 against a real tenant piece_id),
// see top-of-file comment for the full root cause.

interface TripScore {
  trip_id: string;
  name: string;
  destination: string | null;
  score: number;
  runway_fit: number;
  richness: number;
  dfs_relevance_score: number;
  engagement_adjustment_score: number;
  forced: boolean;
  selected: boolean;
  reason: string;
}

interface QuarterPlanPayload {
  trip_ids: string[];
  forced_specials: string[];
  destination_shares: Record<string, number>;
  capacity_note: string | null;
  trip_scores: TripScore[];
}

interface ReallocationSuggestion {
  plan: QuarterPlanPayload;
  has_existing_plan: boolean;
  added: string[];
  removed: string[];
  unchanged: string[];
}

const now = new Date();
const DEFAULT_YEAR = now.getFullYear();
const DEFAULT_QUARTER = Math.floor(now.getMonth() / 3) + 1;

export default function PlanningTab() {
  return (
    <div>
      <div style={{ marginBottom: 22 }}>
        {/* AA-564 4.1 — renamed "Slate" -> "Social Content" here (Tenant Portal only; Admin's
            /admin/atom-curation page still calls this section "05 · Slate", AA-563/564 Group 2). */}
        <h2 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: T.ink, margin: "0 0 6px", letterSpacing: "-0.01em" }}>
          Social Content
        </h2>
        <p style={{ fontSize: 13, color: T.muted, lineHeight: 1.6, margin: 0 }}>
          Channel-scoped content picks, ranked by score and cleared against each channel&rsquo;s
          bar — pick one below to start writing.
        </p>
      </div>

      <SlateTab />

      <ReallocationSection defaultYear={DEFAULT_YEAR} defaultQuarter={DEFAULT_QUARTER} />
    </div>
  );
}

// ---------------------------------------------------------------- trip reallocation (round 6)

function ReallocationSection({ defaultYear, defaultQuarter }: { defaultYear: number; defaultQuarter: number }) {
  const nextQuarter = defaultQuarter === 4 ? 1 : defaultQuarter + 1;
  const nextYear = defaultQuarter === 4 ? defaultYear + 1 : defaultYear;
  const [suggestion, setSuggestion] = useState<ReallocationSuggestion | null>(null);
  const [loading, setLoading] = useState(false);
  const [confirmStatus, setConfirmStatus] = useState<string | null>(null);

  const loadSuggestion = useCallback(() => {
    setLoading(true);
    setConfirmStatus(null);
    fetch(`/api/tenant/v1/planning/trip-reallocation/suggest?year=${nextYear}&quarter=${nextQuarter}`)
      .then(r => (r.ok ? r.json() : Promise.reject()))
      .then(d => setSuggestion(d))
      .catch(() => setSuggestion(null))
      .finally(() => setLoading(false));
  }, [nextYear, nextQuarter]);

  const confirm = useCallback((accept: boolean) => {
    setConfirmStatus(accept ? "Applying…" : "Recording…");
    fetch("/api/tenant/v1/planning/trip-reallocation/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ year: nextYear, quarter: nextQuarter, accept }),
    })
      .then(r => (r.ok ? r.json() : Promise.reject()))
      .then(() => setConfirmStatus(accept ? "Applied — plan updated for next quarter." : "Rejected — no change made."))
      .catch(() => setConfirmStatus("Couldn't record your decision — try again."));
  }, [nextYear, nextQuarter]);

  return (
    <Card style={{ padding: "16px 18px" }}>
      <CardHead title={`Reallocation suggestion — Q${nextQuarter} ${nextYear}`}
        action={<Btn variant="secondary" size="sm" onClick={loadSuggestion} disabled={loading}>
          {loading ? "Checking…" : "Check for a suggestion"}
        </Btn>} />
      <p style={{ fontSize: 12.5, color: T.muted, lineHeight: 1.6, margin: "0 0 14px" }}>
        Based on real feedback so far, here&rsquo;s what would change if you re-planned next quarter now —
        nothing changes until you approve it.
      </p>
      {suggestion && (
        <div>
          {suggestion.added.length === 0 && suggestion.removed.length === 0 ? (
            <div style={{ fontSize: 12.5, color: T.muted }}>No change suggested — the plan stays the same.</div>
          ) : (
            <>
              {suggestion.added.length > 0 && (
                <div style={{ marginBottom: 8, fontSize: 12.5 }}>
                  <Badge variant="success">+{suggestion.added.length} add</Badge>
                  <span style={{ marginLeft: 8, color: T.muted, fontFamily: mono, fontSize: 11 }}>
                    {suggestion.added.join(", ")}
                  </span>
                </div>
              )}
              {suggestion.removed.length > 0 && (
                <div style={{ marginBottom: 12, fontSize: 12.5 }}>
                  <Badge variant="error">−{suggestion.removed.length} remove</Badge>
                  <span style={{ marginLeft: 8, color: T.muted, fontFamily: mono, fontSize: 11 }}>
                    {suggestion.removed.join(", ")}
                  </span>
                </div>
              )}
              <div style={{ display: "flex", gap: 8 }}>
                <Btn variant="primary" size="sm" onClick={() => confirm(true)}>Apply this</Btn>
                <Btn variant="ghost" size="sm" onClick={() => confirm(false)}>Reject</Btn>
              </div>
            </>
          )}
        </div>
      )}
      {confirmStatus && <div style={{ marginTop: 10, fontSize: 12, color: T.muted }}>{confirmStatus}</div>}
    </Card>
  );
}
