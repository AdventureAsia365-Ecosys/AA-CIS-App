"use client";
// app/admin/atom-curation/page.tsx — AA-527 original build, rebuilt AA-551 (07/09/2026).
//
// AA-551 STEP0 (docs/investigation/AA-550-admin-ui-real-audit.md): the original 8-section page
// (AA-527 "bổ sung") required a single selected Tour for sections 02-08, which meant Segment/
// Score/Route/Hub could never show anything in "All tours" mode — directly contradicting AA-545's
// own platform-wide redesign of exactly those 3 (dropped `tenant_id` from the schema entirely,
// same day, but the API layer here stayed hard-scoped to one Tour regardless). This page is now
// SPLIT in two, per Nghiệp's decision (AA-551, "Phương án B"):
//   - THIS page (`/admin/atom-curation`, same URL) = 01 Atomize + 02 Segment + 03 Score +
//     04 Route/Hub + 05 Slate — genuinely platform-wide Master Content monitoring. A common
//     Tour+Market filter applies to 01-04 at once (05 Slate is the one deliberate exception, see
//     below); each of 02-04 also has its own extra filter row. A header stat bar (Tour/Atom/
//     Segment/Score-row/Route/Hub counts) re-filters live with the same common filter.
//   - `/admin/tenant-activity` (new page) = 06 Write/Gate + 07 Review + 08 Publish — a specific
//     TENANT's activity on a specific Tour, a different subject entirely (AA-550 point C.8: the
//     original single page "lẫn lộn nội dung của tầng admin và tenant").
//
// 05 Slate is NOT part of the platform-wide fix (AA-550 A.3, confirmed real schema:
// `acp_shared.subject.tenant_id NOT NULL`) — it keeps requiring one selected Tour, unchanged
// behavior, only its prompt copy is now English.
//
// Backend (api/routers/admin_dashboard.py, AA-551): `tour_id` is now OPTIONAL on
// `segments`/`score`/`routes` (each also gained a `market` filter + a section-specific filter +
// pagination + `tour_id`/`tour_name` on every row); `slate` is unchanged. New
// `GET /admin/dashboard/summary` feeds the header stat bar.
//
// AA-554 (07/09/2026, this build) — page title/sidebar label renamed "Social Content" (route
// unchanged); Tours sidebar (Atomize) is now sticky + taller + "Load more" paginated, no longer a
// fixed 640px box; bulk-select + "Star selected" added to AtomCard; Segment rows now grouped by
// segment_id with numbering + a clickable Route badge (client-side cross-filter into Route/Hub);
// Score section gained a formula explainer, Demand tooltip, row numbering, and a link into
// Segment (client-side `segment_id` cross-filter); a shared MARKET_NAMES legend covers Segment +
// Score + (now) Route/Hub. All 8 empty-state DB-table/file/issue-number leaks AA-552 found are
// rewritten in plain English (see this task's Linear comment for the grep before/after).
//
// AA-554 mục G/H (07/09/2026, same-day follow-up after Nghiệp's decision) — Route/Hub is now a
// real split: Route (1 tour's own journey, unchanged behavior) + a new Hub table (2+ tours' shared
// journey, backed by a real `GET /api/admin/dashboard/hubs`) with a genuine empty-state ("No Hub
// yet — needs 2+ tours sharing a route segment") rather than being folded into Route's own
// `hub_name` column. Slate's `used`/`cut` states: `used` is now wired for real (auto-set the
// instant a content_piece is created from that Slate proposal, services/acp_content_writing/
// service.py); `cut` stays backend/API-only (no tenant UI button yet — POST /v1/subjects/{id}/cut
// exists, unreachable from any control here, see the AA-554 child issue for building that
// button) — its header badge is shown for real (not hidden) with a "coming soon" note.
import { Suspense, useState, useEffect, useCallback } from "react";
import { useSearchParams } from "next/navigation";
import {
  Trash2, ChevronDown, ChevronRight, Layers, Milestone,
} from "lucide-react";
import AdminSidebar from "../_components/AdminSidebar";
import SocialContentSubNav, { type SectionKey } from "../_components/SocialContentSubNav";
import { A, serif, mono, sans, Card, Badge, Btn, LoadingScreen, TH, TD } from "../_components/adminUi";
import { fetchJson, EmptyState, ErrorState, AuditTable, Col } from "../_components/auditPanels";

const MARKETS = ["US", "UK", "AU", "DE", "FR", "NL"];

// AA-554 E.13 — shared market-code legend, used by Segment + Score (Route/Hub's own copy of this
// is part of the deferred G redesign, not added here).
const MARKET_NAMES: Record<string, string> = {
  US: "United States", UK: "United Kingdom", AU: "Australia",
  DE: "Germany", FR: "France", NL: "Netherlands",
};

function MarketLegend() {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 10, fontSize: 11, color: A.muted2, marginBottom: 10 }}>
      {Object.entries(MARKET_NAMES).map(([code, name]) => (
        <span key={code}><strong style={{ color: A.muted }}>{code}</strong> = {name}</span>
      ))}
    </div>
  );
}

function marketTitle(code: string | null): string | undefined {
  return code ? MARKET_NAMES[code] : undefined;
}

// ── Shared types ─────────────────────────────────────────────────────────────

interface TourSummary {
  tour_id: string;
  tour_name: string;
  atom_count: number;
  is_thin: boolean;
  unreviewed_count: number;
  used_atom_count: number;
  lifecycle_stage: "active" | "phasing_out" | "retired";
  atomized_at: string | null;
  owner_scopes: string[];
}

interface Summary {
  distinctiveness_breakdown: { HIGH: number; MED: number; LOW: number };
  total_count: number;
  reviewed_count: number;
  by_tour: TourSummary[];
}

interface DashboardSummary {
  tour_count: number; atom_count: number; segment_count: number;
  score_count: number; route_count: number; hub_count: number;
}

// AA-575 — guards the `?section=` deep-link param (see AtomCurationDashboard below): an
// unrecognized/typo'd value falls back to "atomize" instead of matching none of the
// `activeSection === "..."` checks and rendering a blank content column.
const VALID_SECTIONS: Set<string> = new Set(["atomize", "segment", "score", "route_hub", "slate"]);

const LIFECYCLE_COLOR: Record<string, "green" | "amber" | "gray"> = {
  active: "green", phasing_out: "amber", retired: "gray",
};

const selectStyle: React.CSSProperties = {
  padding: "8px 12px", background: A.card, border: `1px solid ${A.line}`, borderRadius: 8,
  fontSize: 13, fontFamily: sans, color: A.body, cursor: "pointer",
};

const inputStyle: React.CSSProperties = {
  padding: "7px 10px", background: A.card, border: `1px solid ${A.line}`, borderRadius: 8,
  fontSize: 12.5, fontFamily: sans, color: A.body, width: 140,
};

// ══════════════════════════════════════════════════════════════════════════
// Section 01 — Atomize (PR #311's original build, unchanged by AA-551)
// ══════════════════════════════════════════════════════════════════════════

interface Atom {
  atom_id: string;
  tour_id: string;
  tour_name: string;
  text: string;
  activity_type: string | null;
  distinctiveness: "HIGH" | "MED" | "LOW";
  deleted: boolean;
  unreviewed: boolean;
  segment_id: string | null;
  canonical_place: string | null;
  canonical_action: string | null;
  segment_score: number | null;
  route_id: string | null;
  route_hub_name: string | null;
  owner_scope: string;
  recurrence: number | null;
  usage_count: number;
  lifecycle_stage: "active" | "phasing_out" | "retired";
}

const DIST_COLOR: Record<string, "green" | "amber" | "gray"> = { HIGH: "green", MED: "amber", LOW: "gray" };
const PAGE_SIZE = 50;
const TOURS_PAGE_SIZE = 30; // AA-554 B.6 — Tours sidebar "Load more" window size

function isLegacyScope(scope: string): boolean { return scope !== "platform"; }

function OwnerBadge({ scope }: { scope: string }) {
  return isLegacyScope(scope) ? <Badge color="amber">Legacy tenant-owned</Badge> : <Badge color="gold">Platform</Badge>;
}

function AtomizeSection({ summary, summaryLoading, selectedTour, onTourChange, onSummaryChange }: {
  summary: Summary | null; summaryLoading: boolean;
  selectedTour: string | null; onTourChange: (t: string | null) => void;
  onSummaryChange: () => void;
}) {
  const [atoms, setAtoms] = useState<Atom[]>([]);
  const [total, setTotal] = useState(0);
  const [distinctiveness, setDistinctiveness] = useState("");
  const [unreviewedOnly, setUnreviewedOnly] = useState(false);
  const [lifecycleFilter, setLifecycleFilter] = useState("");
  const [atomsLoading, setAtomsLoading] = useState(true);
  const [atomsError, setAtomsError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [collapsedSegments, setCollapsedSegments] = useState<Set<string>>(new Set());
  // AA-554 B.6 — Tours sidebar "Load more" window (client-side; `summary.by_tour` already
  // arrives whole from GET /admin/atoms/summary, no backend pagination to wire).
  const [toursShown, setToursShown] = useState(TOURS_PAGE_SIZE);
  // AA-564 3.2 — manual atomize backfill (decision 1, AA-563: atomize has exactly ONE automatic
  // trigger, a fresh publish — any tour that entered Master Content another way, or before that
  // trigger existed, never gets atomized on its own).
  const [unatomizedTours, setUnatomizedTours] = useState<{ tour_id: string; tour_name: string }[]>([]);
  const [atomizeRunning, setAtomizeRunning] = useState<string | null>(null); // status line while polling
  const [atomizeTriggering, setAtomizeTriggering] = useState(false);

  const loadUnatomized = useCallback(() => {
    fetchJson<{ total: number; tours: { tour_id: string; tour_name: string }[] }>("/api/admin/atoms/unatomized-tours")
      .then(d => setUnatomizedTours(d.tours))
      .catch(() => {});
  }, []);
  useEffect(() => { loadUnatomized(); }, [loadUnatomized]);

  async function triggerAtomize(target: { tour_id: string } | { all: true }) {
    setAtomizeTriggering(true);
    try {
      const res = await fetch("/api/admin/atoms/atomize", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(target),
      });
      if (!res.ok) { setAtomizeTriggering(false); return; }
      const body: { tour_ids: string[] } = await res.json();
      setAtomizeRunning(
        "all" in target ? `Atomizing ${body.tour_ids.length} tours…` : "Atomizing…",
      );
      // Poll until every accepted tour has dropped off the unatomized list (or ~10 min elapses —
      // run_t5_atomize() can take a while, up to one Bedrock call per itinerary day, per tour).
      const pending = new Set(body.tour_ids);
      let attempts = 0;
      const poll = setInterval(async () => {
        attempts += 1;
        const d = await fetchJson<{ tours: { tour_id: string; tour_name: string }[] }>("/api/admin/atoms/unatomized-tours").catch(() => null);
        if (d) {
          const stillPending = new Set(d.tours.map(t => t.tour_id));
          for (const id of Array.from(pending)) if (!stillPending.has(id)) pending.delete(id);
          setUnatomizedTours(d.tours);
        }
        if (pending.size === 0 || attempts >= 120) {
          clearInterval(poll);
          setAtomizeRunning(null);
          onSummaryChange();
        }
      }, 5000);
    } finally {
      setAtomizeTriggering(false);
    }
  }

  const loadAtoms = useCallback((offset: number, append: boolean) => {
    if (append) setLoadingMore(true); else setAtomsLoading(true);
    setAtomsError(null);
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
    if (distinctiveness) params.set("distinctiveness", distinctiveness);
    if (unreviewedOnly) params.set("unreviewed_only", "true");
    if (selectedTour) params.set("tour_id", selectedTour);
    if (lifecycleFilter) params.set("lifecycle_stage", lifecycleFilter);
    fetchJson<{ atoms: Atom[]; total: number }>(`/api/admin/atoms?${params}`)
      .then(d => {
        setAtoms(prev => (append ? [...prev, ...d.atoms] : d.atoms));
        setTotal(d.total ?? 0);
      })
      .catch(e => setAtomsError(String(e.message || e)))
      .finally(() => { setAtomsLoading(false); setLoadingMore(false); });
  }, [distinctiveness, unreviewedOnly, selectedTour, lifecycleFilter]);

  useEffect(() => { loadAtoms(0, false); }, [loadAtoms]);

  async function deleteAtom(atom: Atom) {
    if (!confirm("Remove this atom from the curated pool? It will no longer be used for any tenant's content going forward.")) return;
    setAtoms(prev => prev.filter(a => a.atom_id !== atom.atom_id));
    await fetch(`/api/admin/atoms/${atom.atom_id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deleted: true }),
    });
    onSummaryChange();
  }

  const breakdown = summary?.distinctiveness_breakdown ?? { HIGH: 0, MED: 0, LOW: 0 };
  // AA-564 1.3 — this block used to always read the whole-dataset `summary.total_count`/
  // `reviewed_count`, even when a single Tour was selected (the header stat bar above it does
  // filter correctly, which is why the two used to visibly disagree). `summary.by_tour` already
  // carries per-tour `atom_count`/`unreviewed_count` (GET /admin/atoms/summary, unchanged) — just
  // read that when a Tour is selected instead of the platform-wide totals. High/Medium/Low stays
  // platform-wide on purpose (decision 3, AA-563/564: backend's distinctiveness breakdown has no
  // per-tour grouping, out of scope here) — labeled below so it doesn't read as another bug.
  const selectedTourMeta = selectedTour ? (summary?.by_tour ?? []).find(t => t.tour_id === selectedTour) ?? null : null;
  const totalAtoms = selectedTourMeta ? selectedTourMeta.atom_count : (summary?.total_count ?? 0);
  const reviewedAtoms = selectedTourMeta
    ? selectedTourMeta.atom_count - selectedTourMeta.unreviewed_count
    : (summary?.reviewed_count ?? 0);

  return (
    <>
      {summaryLoading ? <LoadingScreen msg="Loading curation dashboard…" /> : (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 14, marginBottom: 6 }}>
            {[
              ["Total atoms", totalAtoms, A.gold],
              ["Reviewed", reviewedAtoms, A.green],
              ["High distinctiveness", breakdown.HIGH, A.green],
              ["Medium", breakdown.MED, A.amber],
              ["Low", breakdown.LOW, A.muted2],
            ].map(([label, value, accent]) => (
              <Card key={label as string} style={{ padding: "14px 16px" }}>
                <div style={{ fontSize: 11.5, color: A.muted, marginBottom: 6 }}>{label}</div>
                <div style={{ fontFamily: serif, fontSize: 24, fontWeight: 500, color: accent as string }}>{value}</div>
              </Card>
            ))}
          </div>
          <div style={{ fontSize: 10.5, color: A.muted2, marginBottom: 14 }}>
            {selectedTourMeta
              ? "Total atoms/Reviewed are for the selected Tour. High/Medium/Low distinctiveness stays platform-wide (all tours)."
              : "All 5 figures are platform-wide (all tours)."}
          </div>

          {/* AA-564 3.2 — manual atomize backfill banner. Only 1 automatic trigger exists (a
              fresh publish, AA-526) — this is the only way an OLDER Master Content tour ever
              gets atomized. */}
          {unatomizedTours.length > 0 && (
            <Card style={{ padding: "14px 16px", marginBottom: 16, borderColor: A.gold }}>
              <div style={{ fontSize: 13, color: A.body, marginBottom: 10 }}>
                ⚠ <strong>{unatomizedTours.length} tour{unatomizedTours.length === 1 ? "" : "s"}</strong> in
                Master Content {unatomizedTours.length === 1 ? "has" : "have"} never been atomized — atomize
                only runs automatically the moment a tour is freshly published (AA-526); anything published
                before that, or another way, needs a manual run.
              </div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <Btn
                  variant="secondary" size="sm"
                  disabled={atomizeTriggering || !!atomizeRunning || !selectedTour || !unatomizedTours.some(t => t.tour_id === selectedTour)}
                  onClick={() => selectedTour && triggerAtomize({ tour_id: selectedTour })}
                >
                  Atomize selected Tour
                </Btn>
                <Btn
                  variant="secondary" size="sm"
                  disabled={atomizeTriggering || !!atomizeRunning}
                  onClick={() => triggerAtomize({ all: true })}
                >
                  Atomize all {unatomizedTours.length} tours (runs in background)
                </Btn>
                {atomizeRunning && <span style={{ fontSize: 12, color: A.muted }}>{atomizeRunning}</span>}
              </div>
            </Card>
          )}

          <div style={{ display: "grid", gridTemplateColumns: "280px 1fr", gap: 18, alignItems: "start" }}>
            {/* AA-554 B.4/B.5 — sticky (same `position: sticky, top: 0` pattern AA-551 already
                proved works for the 01-05 section-nav, within this same page's outer scroll
                container — see that inner-nav below for the identical mechanism) + taller
                viewport-relative maxHeight (was a fixed 640px, too small for 74+ tours) with its
                own internal scroll for the list itself. */}
            <Card style={{ padding: 0, overflow: "hidden", position: "sticky", top: 0 }}>
              <div style={{ padding: "12px 16px", borderBottom: `1px solid ${A.line}`, fontSize: 12, fontWeight: 600, color: A.ink3, textTransform: "uppercase", letterSpacing: "0.06em" }}>
                Tours ({summary?.by_tour.length ?? 0})
              </div>
              <div style={{ maxHeight: "calc(100vh - 260px)", overflowY: "auto" }}>
                <button onClick={() => onTourChange(null)} style={{
                  display: "block", width: "100%", textAlign: "left", padding: "10px 16px",
                  background: selectedTour === null ? A.bg : "transparent", border: "none",
                  borderBottom: `1px solid ${A.line2}`, cursor: "pointer", fontFamily: sans,
                  fontSize: 12.5, fontWeight: selectedTour === null ? 700 : 500, color: A.ink,
                }}>
                  All tours
                </button>
                {(summary?.by_tour ?? []).slice(0, toursShown).map(t => (
                  <button key={t.tour_id} onClick={() => onTourChange(t.tour_id)} style={{
                    display: "block", width: "100%", textAlign: "left", padding: "10px 16px",
                    background: selectedTour === t.tour_id ? A.bg : "transparent", border: "none",
                    borderBottom: `1px solid ${A.line2}`, cursor: "pointer", fontFamily: sans,
                  }}>
                    <div style={{ fontSize: 12.5, fontWeight: selectedTour === t.tour_id ? 700 : 500, color: A.ink, marginBottom: 3 }}>
                      {t.tour_name}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                      <span style={{ fontSize: 11, color: A.muted }}>{t.atom_count} atoms</span>
                      {t.is_thin && <Badge color="red">Thin</Badge>}
                      {t.unreviewed_count > 0 && <Badge color="blue">{t.unreviewed_count} new</Badge>}
                      {t.lifecycle_stage !== "active" && <Badge color={LIFECYCLE_COLOR[t.lifecycle_stage]}>{t.lifecycle_stage}</Badge>}
                      {t.owner_scopes.map(s => <OwnerBadge key={s} scope={s} />)}
                    </div>
                    <div style={{ fontSize: 10.5, color: A.muted2, marginTop: 3 }}>
                      {t.used_atom_count} / {t.atom_count} atoms used in written content
                    </div>
                  </button>
                ))}
                {(summary?.by_tour.length ?? 0) > toursShown && (
                  <button onClick={() => setToursShown(n => n + TOURS_PAGE_SIZE)} style={{
                    display: "block", width: "100%", textAlign: "center", padding: "10px 16px",
                    background: "none", border: "none", borderTop: `1px solid ${A.line2}`,
                    cursor: "pointer", fontFamily: sans, fontSize: 12, fontWeight: 600, color: A.gold,
                  }}>
                    Load more ({Math.min(toursShown, summary?.by_tour.length ?? 0)} / {summary?.by_tour.length})
                  </button>
                )}
              </div>
            </Card>

            <div>
              <div style={{ display: "flex", gap: 10, marginBottom: 14, alignItems: "center", flexWrap: "wrap" }}>
                <select value={distinctiveness} onChange={e => setDistinctiveness(e.target.value)}
                  style={selectStyle}>
                  <option value="">All distinctiveness</option>
                  <option value="HIGH">High</option>
                  <option value="MED">Medium</option>
                  <option value="LOW">Low</option>
                </select>
                <select value={lifecycleFilter} onChange={e => setLifecycleFilter(e.target.value)} style={selectStyle}>
                  <option value="">All lifecycle stages</option>
                  <option value="active">Active</option>
                  <option value="phasing_out">Phasing out</option>
                  <option value="retired">Retired</option>
                </select>
                <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, color: A.body, cursor: "pointer" }}>
                  <input type="checkbox" checked={unreviewedOnly} onChange={e => setUnreviewedOnly(e.target.checked)} />
                  Unreviewed only
                </label>
              </div>
              {/* AA-554 C.8 — schema supports phasing_out/retired but every tour currently loaded
                  is "active" — this note is computed from the real, currently-loaded Tours list
                  (not hardcoded), so it disappears on its own the day a non-active tour exists. */}
              {(summary?.by_tour.length ?? 0) > 0 && (summary?.by_tour ?? []).every(t => t.lifecycle_stage === "active") && (
                <div style={{ fontSize: 11, color: A.muted2, marginTop: -8, marginBottom: 14 }}>
                  All {summary?.by_tour.length} tours are currently Active — Phasing out/Retired not yet in use.
                </div>
              )}

              {atomsError ? <ErrorState message={atomsError} onRetry={() => loadAtoms(0, false)} /> :
                atomsLoading ? <LoadingScreen msg="Loading atoms…" /> : atoms.length === 0 ? (
                <EmptyState title="No atoms match this filter"
                  body="Atoms are extracted automatically once a tour is approved into Master Content — nothing to trigger here." />
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {groupBySegment(atoms).map(row =>
                    row.kind === "atom" ? (
                      <AtomCard key={row.atom.atom_id} atom={row.atom} showTour={!selectedTour} onDelete={deleteAtom} />
                    ) : (
                      <SegmentGroup key={row.segmentId} place={row.place} action={row.action} atoms={row.atoms}
                        score={row.score} routeHubName={row.routeHubName} showTour={!selectedTour}
                        collapsed={collapsedSegments.has(row.segmentId)}
                        onToggle={() => setCollapsedSegments(prev => {
                          const next = new Set(prev);
                          next.has(row.segmentId) ? next.delete(row.segmentId) : next.add(row.segmentId);
                          return next;
                        })}
                        onDelete={deleteAtom} />
                    )
                  )}
                </div>
              )}

              {atoms.length < total && !atomsError && (
                <div style={{ textAlign: "center", marginTop: 16 }}>
                  <Btn variant="secondary" disabled={loadingMore} onClick={() => loadAtoms(atoms.length, true)}>
                    {loadingMore ? "Loading…" : `Load more (${atoms.length} / ${total})`}
                  </Btn>
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </>
  );
}

type AtomRow =
  | { kind: "atom"; atom: Atom }
  | { kind: "segment"; segmentId: string; place: string; action: string; atoms: Atom[]; score: number | null; routeHubName: string | null };

function groupBySegment(atoms: Atom[]): AtomRow[] {
  const bySegment = new Map<string, Atom[]>();
  for (const atom of atoms) {
    if (!atom.segment_id) continue;
    const list = bySegment.get(atom.segment_id) ?? [];
    list.push(atom);
    bySegment.set(atom.segment_id, list);
  }
  const rows: AtomRow[] = [];
  const emitted = new Set<string>();
  for (const atom of atoms) {
    const members = atom.segment_id ? bySegment.get(atom.segment_id) : undefined;
    if (members && members.length > 1) {
      if (emitted.has(atom.segment_id!)) continue;
      emitted.add(atom.segment_id!);
      rows.push({
        kind: "segment", segmentId: atom.segment_id!,
        place: members[0].canonical_place ?? "", action: members[0].canonical_action ?? "",
        atoms: members, score: members.find(m => m.segment_score != null)?.segment_score ?? null,
        routeHubName: members.find(m => m.route_hub_name != null)?.route_hub_name ?? null,
      });
    } else {
      rows.push({ kind: "atom", atom });
    }
  }
  return rows;
}

function AtomCard({ atom, showTour, onDelete }: {
  atom: Atom; showTour: boolean; onDelete: (a: Atom) => void;
}) {
  return (
    <Card style={{ padding: "14px 18px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          {showTour && <div style={{ fontSize: 11, color: A.muted2, marginBottom: 4, fontFamily: mono }}>{atom.tour_name}</div>}
          <div style={{ fontSize: 13.5, color: A.body, lineHeight: 1.5 }}>{atom.text}</div>
          <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap", alignItems: "center" }}>
            <Badge color={DIST_COLOR[atom.distinctiveness] ?? "gray"}>{atom.distinctiveness}</Badge>
            {atom.activity_type && <Badge color="gray">{atom.activity_type}</Badge>}
            {atom.unreviewed && <Badge color="blue">New</Badge>}
            <OwnerBadge scope={atom.owner_scope} />
            {atom.lifecycle_stage !== "active" && <Badge color={LIFECYCLE_COLOR[atom.lifecycle_stage]}>{atom.lifecycle_stage}</Badge>}
            {atom.recurrence != null && atom.recurrence > 0 && (
              <span style={{ fontSize: 10.5, fontFamily: mono, color: A.muted }}>↻ {atom.recurrence} itineraries</span>
            )}
            {/* AA-554 D.10 — tooltip explaining what "used" counts. */}
            <span style={{ fontSize: 10.5, fontFamily: mono, color: atom.usage_count > 0 ? A.ink3 : A.muted2 }}
              title="Number of times a tenant has selected this atom's Segment to write about (via T8 Angle Gate)">
              {atom.usage_count > 0 ? `✎ used ${atom.usage_count}×` : "not yet used"}
            </span>
          </div>
        </div>
        <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
          <button onClick={() => onDelete(atom)} title="Remove"
            style={{ background: "none", border: `1px solid ${A.line}`, borderRadius: 6, padding: 6, cursor: "pointer", color: A.red, display: "flex" }}>
            <Trash2 size={14} />
          </button>
        </div>
      </div>
    </Card>
  );
}

function SegmentGroup({ place, action, atoms, score, routeHubName, showTour, collapsed, onToggle, onDelete }: {
  place: string; action: string; atoms: Atom[]; score: number | null; routeHubName: string | null;
  showTour: boolean; collapsed: boolean; onToggle: () => void; onDelete: (a: Atom) => void;
}) {
  return (
    <div style={{ border: `1px solid ${A.line}`, borderRadius: 10, overflow: "hidden" }}>
      <button onClick={onToggle} style={{
        width: "100%", display: "flex", alignItems: "center", gap: 8, padding: "10px 14px",
        background: A.goldTint, border: "none", cursor: "pointer", textAlign: "left", flexWrap: "wrap",
      }}>
        {collapsed ? <ChevronRight size={14} color={A.muted} /> : <ChevronDown size={14} color={A.muted} />}
        <Layers size={13} color={A.gold} />
        <span style={{ fontSize: 13, fontWeight: 600, color: A.body, fontFamily: sans }}>
          {place}{action ? ` — ${action}` : ""}
        </span>
        {score != null && (
          <span style={{ fontFamily: mono, fontSize: 11, color: A.ink3, background: A.card, border: `1px solid ${A.line}`, borderRadius: 6, padding: "2px 7px" }} title="Rank-sum — lower is better">
            Score {score}
          </span>
        )}
        {routeHubName && (
          <Badge color="gold"><Milestone size={11} style={{ verticalAlign: -2, marginRight: 3 }} />Part of Route: {routeHubName}</Badge>
        )}
        <span style={{ fontSize: 11.5, color: A.muted2, marginLeft: "auto" }}>{atoms.length} atoms, same moment</span>
      </button>
      {!collapsed && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: 8, background: A.card }}>
          {atoms.map(atom => <AtomCard key={atom.atom_id} atom={atom} showTour={showTour} onDelete={onDelete} />)}
        </div>
      )}
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════
// Sections 02-04 — Segment / Score / Route-Hub — AA-551: platform-wide, paginated,
// common Tour+Market filter + own extra filter row each.
// ══════════════════════════════════════════════════════════════════════════

// `enabled` (default true) guards the actual fetch — Segment/Score/Route pass no guard (they
// support `tour_id` omitted, "All tours" mode, AA-551's whole point); Slate passes
// `enabled: !!tourId` since its backend endpoint still hard-requires `tour_id` (AA-550 A.3, real
// per-tenant exception, not touched by this task) — without this guard, deselecting the Tour
// filter would fire a request Slate's own API 422s on (found live during this task's own
// Playwright verify, fixed before merge).
function usePlatformFetch<T>(
  endpoint: string, params: Record<string, string | number | undefined>, enabled = true,
) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Stable dep key — `params` is a fresh object every render otherwise.
  const key = JSON.stringify(params);

  const load = useCallback(() => {
    if (!enabled) { setData(null); setLoading(false); setError(null); return; }
    setLoading(true); setError(null);
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== "") qs.set(k, String(v)); });
    fetchJson<T>(`${endpoint}?${qs}`)
      .then(setData)
      .catch(e => setError(String(e.message || e)))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [endpoint, key, enabled]);

  useEffect(() => { load(); }, [load]);
  return { data, loading, error, reload: load };
}

const filterBarStyle: React.CSSProperties = {
  display: "flex", gap: 8, marginBottom: 14, alignItems: "center", flexWrap: "wrap",
};

// AA-557 D.3/E.7/F.9 — live Playwright confirmed the previous sticky filter row (plain
// `position:sticky, background:A.bg`, no border) visually merges with whatever content scrolls
// up beneath it once stuck — the next Segment/Route group's own header sits flush against it with
// no visible seam (tests/e2e/results/aa557/D-00-segment-after-scroll-PREFIX.png). A bottom
// border + drop shadow gives it a real visual edge (the standard "elevated sticky bar" pattern)
// so scrolled content reads as passing UNDER it, not merging into it; extra paddingBottom widens
// the gap before the next card.
const stickyFilterBarStyle: React.CSSProperties = {
  ...filterBarStyle, position: "sticky", top: 0, background: A.bg, zIndex: 5,
  paddingTop: 8, paddingBottom: 14, marginBottom: 10,
  borderBottom: `1px solid ${A.line}`, boxShadow: "0 4px 10px -6px rgba(0,0,0,0.18)",
};

interface SegmentRow {
  tour_id: string; tour_name: string | null;
  segment_id: string; canonical_place: string; canonical_action: string;
  member_count: number; market: string | null; total_rank: number | null; recurrence: number | null;
  excluded_reason: string | null; route_id: string | null; route_hub_name: string | null;
}

// AA-554 E.11 — one card per distinct segment_id (was one flat AuditTable row per
// (Segment, Tour, Market)); numbered in display order within the current page.
interface SegmentGroupDisplay {
  segmentId: string; place: string; action: string; rows: SegmentRow[];
  routeId: string | null; routeHubName: string | null;
}

function groupSegmentRows(rows: SegmentRow[]): SegmentGroupDisplay[] {
  const order: string[] = [];
  const map = new Map<string, SegmentRow[]>();
  for (const r of rows) {
    if (!map.has(r.segment_id)) { map.set(r.segment_id, []); order.push(r.segment_id); }
    map.get(r.segment_id)!.push(r);
  }
  return order.map(id => {
    const members = map.get(id)!;
    const withRoute = members.find(m => m.route_hub_name != null);
    return {
      segmentId: id, place: members[0].canonical_place, action: members[0].canonical_action,
      rows: members, routeId: withRoute?.route_id ?? null, routeHubName: withRoute?.route_hub_name ?? null,
    };
  });
}

function SegmentSection({ tourId, market, focusRouteId, focusSegmentId, onClearFocus, onNavigateToRoute }: {
  tourId: string | null; market: string; focusRouteId: string | null; focusSegmentId: string | null;
  onClearFocus: () => void; onNavigateToRoute: (routeId: string) => void;
}) {
  const [placeSearch, setPlaceSearch] = useState("");
  const [minRecurrence, setMinRecurrence] = useState("");
  const [offset, setOffset] = useState(0);
  useEffect(() => { setOffset(0); }, [tourId, market, placeSearch, minRecurrence, focusRouteId, focusSegmentId]);

  // AA-554 — a cross-filter (focusRouteId/focusSegmentId) narrows AFTER fetch, client-side (see
  // below); real data has up to 138 raw Segment rows, well past PAGE_SIZE=50, so the target row
  // of a cross-nav click can land past the first page and show a false "not found" empty state —
  // found live during this task's own Playwright verify (Score → Segment cross-nav), not
  // theoretical. Fixed by fetching a much larger page whenever a focus filter is active, and
  // hiding the Previous/Next footer in that mode (it isn't really "browsing pages" anymore).
  const hasFocus = !!(focusRouteId || focusSegmentId);
  const { data, loading, error, reload } = usePlatformFetch<{ data: SegmentRow[]; total: number }>(
    "/api/admin/dashboard/segments",
    { tour_id: tourId ?? undefined, market: market || undefined, place_search: placeSearch || undefined,
      min_recurrence: minRecurrence || undefined, limit: hasFocus ? 200 : PAGE_SIZE, offset: hasFocus ? 0 : offset },
  );

  // AA-554 E.12 — client-side narrow to a single Route/Segment when arriving via a cross-section
  // link (Route badge below, or Score's row link) — the backend endpoint gained no new query
  // param for this, it only narrows whatever page is already loaded.
  const filteredRows = (data?.data ?? []).filter(r =>
    (!focusRouteId || r.route_id === focusRouteId) && (!focusSegmentId || r.segment_id === focusSegmentId));
  const groups = groupSegmentRows(filteredRows);

  return (
    <>
      {/* AA-557 D.3 — sticky filter row, now with a real visual edge (see stickyFilterBarStyle). */}
      <div style={stickyFilterBarStyle}>
        <input style={inputStyle} placeholder="Search place/verb…" value={placeSearch}
          onChange={e => setPlaceSearch(e.target.value)} />
        <input style={{ ...inputStyle, width: 110 }} type="number" min={0} placeholder="Min recurrence"
          value={minRecurrence} onChange={e => setMinRecurrence(e.target.value)} />
        {(focusRouteId || focusSegmentId) && (
          <Btn variant="ghost" size="sm" onClick={onClearFocus}>Clear cross-filter</Btn>
        )}
      </div>
      {/* AA-554 E.13 — shared market legend. */}
      <MarketLegend />
      {/* AA-561 1d — investigated (segments.py origin + ported segment_matching.py + real DB
          query): a Segment is grouped by place+verb ACROSS THE WHOLE PLATFORM (AA-545, migration
          146 — no tour_id/tenant_id folded into segment_id at all), so it genuinely CAN span
          multiple tours (and multiple tenants' rewrites) once 2 tours describe the same
          real-world moment — confirmed live in admin_dashboard.py's own list_segments query and
          docstring ("a Segment can span multiple tours"). Real data today (23 Segments, 7 tours,
          none yet overlapping) shows 0 multi-tour Segments — a fact about this catalog's current
          size, not the design. So this table renders ONE single table (header once), with
          Segment/Route-Hub cells merged (rowSpan) across a Segment's rows, but Tour is its OWN
          per-row column, never merged into the Segment cell — the moment 2 tours share a
          Segment, each becomes its own row here rather than being hidden by a merge that assumes
          1 Segment = 1 Tour. See docs/implementation-notes/AA-561.md for the full trace. */}
      {error ? <ErrorState message={error} onRetry={reload} /> :
        loading ? <LoadingScreen msg="Loading Segments…" /> :
        (!data || data.total === 0 || groups.length === 0) ? (
          <EmptyState title="No Segments match this filter" body="No Segment detected yet for this filter." />
        ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <div style={{ overflowX: "auto", border: `1px solid ${A.line}`, borderRadius: 10 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: sans }}>
              <thead>
                <tr>
                  {["#", "Segment", "Route/Hub", "Tour", "Market", "Atoms", "Total rank", "Recurrence"].map(label => (
                    <th key={label} style={TH}>{label}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {groups.map((g, i) => g.rows.map((r, j) => (
                  <tr key={`${g.segmentId}-${r.tour_id}-${r.market ?? "none"}`}
                    style={{ borderTop: j === 0 ? `1px solid ${A.line}` : "none" }}>
                    {j === 0 && (
                      <>
                        <td style={{ ...TD, verticalAlign: "top" }} rowSpan={g.rows.length}>
                          <span style={{ fontFamily: mono, fontSize: 11, color: A.muted2 }}>{offset + i + 1}</span>
                        </td>
                        <td style={{ ...TD, verticalAlign: "top" }} rowSpan={g.rows.length}>
                          <span style={{ fontSize: 13, fontWeight: 600, color: A.body }}>
                            {g.place}{g.action ? ` — ${g.action}` : ""}
                          </span>
                          <div style={{ fontSize: 10.5, color: A.muted2, marginTop: 3 }}>
                            {g.rows.length} market/tour row{g.rows.length !== 1 ? "s" : ""}
                          </div>
                        </td>
                        <td style={{ ...TD, verticalAlign: "top" }} rowSpan={g.rows.length}>
                          {g.routeHubName && g.routeId ? (
                            <button onClick={() => onNavigateToRoute(g.routeId!)}
                              style={{ background: "none", border: "none", padding: 0, cursor: "pointer" }}
                              title="View this Route in Route/Hub">
                              <Badge color="gold"><Milestone size={11} style={{ verticalAlign: -2, marginRight: 3 }} />{g.routeHubName}</Badge>
                            </button>
                          ) : "—"}
                        </td>
                      </>
                    )}
                    <td style={TD}>{r.tour_name ?? "—"}</td>
                    <td style={TD}>{r.market ? <span title={marketTitle(r.market)}>{r.market}</span> : "—"}</td>
                    <td style={TD}>{r.member_count}</td>
                    <td style={TD}>{r.excluded_reason ? <Badge color="gray">{r.excluded_reason}</Badge> : (r.total_rank ?? "—")}</td>
                    <td style={TD}>{r.recurrence ?? "—"}</td>
                  </tr>
                )))}
              </tbody>
            </table>
          </div>
          {!hasFocus && <PageFooter total={data.total} offset={offset} pageSize={PAGE_SIZE} onOffset={setOffset} />}
        </div>
      )}
    </>
  );
}

interface ScoreRow {
  tour_id: string; tour_name: string | null;
  market: string | null; segment_id: string; canonical_place: string | null; canonical_action: string | null;
  demand_rank: number | null; recurrence_rank: number | null; questions_rank: number | null; said_rank: number | null;
  total_rank: number | null; demand_market: string | null; demand_volume: number | null;
  recurrence: number; questions: number; said: number; excluded_reason: string | null;
}

function ScoreSection({ tourId, market, onNavigateToSegment }: {
  tourId: string | null; market: string; onNavigateToSegment: (segmentId: string) => void;
}) {
  const [minRank, setMinRank] = useState("");
  const [maxRank, setMaxRank] = useState("");
  const [offset, setOffset] = useState(0);
  useEffect(() => { setOffset(0); }, [tourId, market, minRank, maxRank]);

  const { data, loading, error, reload } = usePlatformFetch<{ data: ScoreRow[]; total: number }>(
    "/api/admin/dashboard/score",
    { tour_id: tourId ?? undefined, market: market || undefined,
      min_total_rank: minRank || undefined, max_total_rank: maxRank || undefined, limit: PAGE_SIZE, offset },
  );

  // AA-554 F.18 — row numbering, stable across pages (offset-relative, not just index-in-page).
  const numbered = (data?.data ?? []).map((r, i) => ({ ...r, __num: offset + i + 1 }));

  return (
    <>
      {/* AA-554 F.15 — formula explainer. Issue text said "see ADR-0014"; the repo's highest ADR
          is 0003 (docs/adr/0003-segment-score-route-hub-will-become-platform-wide.md, the actual
          doc covering this design) — pointing at a real doc instead of a nonexistent number. */}
      <div style={{ fontSize: 12, color: A.muted, marginBottom: 10 }}>
        Score ranks Segments (not individual atoms) — see ADR-0003.{" "}
        <span
          title="total_rank = demand_rank + recurrence_rank + questions_rank + said_rank (rank-sum, lower is better)"
          style={{ borderBottom: `1px dotted ${A.muted2}`, cursor: "help" }}
        >
          What does total rank mean?
        </span>
      </div>
      {/* AA-557 E.7 — sticky filter row, now with a real visual edge. */}
      <div style={stickyFilterBarStyle}>
        <input style={{ ...inputStyle, width: 110 }} type="number" placeholder="Min total rank" value={minRank} onChange={e => setMinRank(e.target.value)} />
        <input style={{ ...inputStyle, width: 110 }} type="number" placeholder="Max total rank" value={maxRank} onChange={e => setMaxRank(e.target.value)} />
      </div>
      <MarketLegend />
      {error ? <ErrorState message={error} onRetry={reload} /> :
        loading ? <LoadingScreen msg="Loading Score…" /> :
        (!data || data.total === 0) ? <EmptyState title="No ranked Segments match this filter" body="Score runs automatically as part of Route detection." /> : (
        <>
          {/* AA-557 E.8 — real table: sortable + per-column filter. */}
          <AuditTable sortable rows={numbered} rowKey={r => `${r.tour_id}-${r.segment_id}-${r.market ?? "none"}`} columns={[
            { key: "num", label: "#", render: r => r.__num, sortValue: r => r.__num },
            { key: "tour", label: "Tour", render: r => r.tour_name ?? "—",
              sortValue: r => r.tour_name ?? "", filterValue: r => r.tour_name ?? "" },
            {
              key: "place", label: "Segment", render: r => r.canonical_place ? (
                // AA-554 F.17 — link into Segment, filtered to this segment_id.
                <button onClick={() => onNavigateToSegment(r.segment_id)}
                  style={{ background: "none", border: "none", padding: 0, cursor: "pointer", color: A.gold, textAlign: "left", font: "inherit" }}
                  title="View this Segment">
                  {r.canonical_place} — {r.canonical_action} ↗
                </button>
              ) : "—",
              sortValue: r => r.canonical_place ?? "",
              filterValue: r => `${r.canonical_place ?? ""} ${r.canonical_action ?? ""}`,
            },
            { key: "market", label: "Market", render: r => r.market ? <span title={marketTitle(r.market)}>{r.market}</span> : "—",
              sortValue: r => r.market ?? "", filterValue: r => r.market ?? "" },
            { key: "total", label: "Total rank", render: r => r.excluded_reason ? <Badge color="gray">{r.excluded_reason}</Badge> : (r.total_rank ?? "—"),
              sortValue: r => r.total_rank ?? (r.excluded_reason ? Number.MAX_SAFE_INTEGER : null) },
            {
              key: "demand", label: "Demand", render: r => r.demand_rank != null ? (
                // AA-554 F.16 — tooltip explaining the 3-part "#rank (volume · market)" format.
                <span title="Rank among Segments by search demand · raw monthly search volume · the market that volume was measured in">
                  #{r.demand_rank} ({r.demand_volume ?? "—"} · {r.demand_market ?? "—"})
                </span>
              ) : "—",
              sortValue: r => r.demand_rank,
            },
            { key: "recurrence", label: "Recurrence", render: r => r.recurrence_rank != null ? `#${r.recurrence_rank} (${r.recurrence})` : "—",
              sortValue: r => r.recurrence_rank },
            { key: "questions", label: "Questions", render: r => r.questions_rank != null ? `#${r.questions_rank} (${r.questions})` : "—",
              sortValue: r => r.questions_rank },
            { key: "said", label: "Said", render: r => r.said_rank != null ? `#${r.said_rank} (${r.said})` : "—",
              sortValue: r => r.said_rank },
          ] as Col<ScoreRow & { __num: number }>[]} />
          <PageFooter total={data.total} offset={offset} pageSize={PAGE_SIZE} onOffset={setOffset} />
        </>
      )}
    </>
  );
}

interface RouteRow {
  route_id: string; tour_id: string; tour_name: string | null; hub_name: string; market: string | null;
  ordered_segment_ids: string[]; first_day: number; last_day: number; score: number | null; created_at: string;
  version: number; superseded_at: string | null;
}

interface HubRow {
  hub_id: string; hub_name: string; tour_names: string[] | null; route_count: number;
  created_at: string; updated_at: string;
}

// AA-554 mục G — Route/Hub tách 2 bảng riêng (Nghiệp's confirmed decision, hướng 1: split now,
// Hub shows a real, empty table rather than being hidden — data fills in naturally as the tour
// catalog grows). Route (1 tour's own journey) and Hub (2+ tours' shared journey, grouped by
// route_detection.py's families()) are genuinely different things — folding Hub into Route's own
// `hub_name` column (the pre-existing behavior) hid that distinction entirely.
function RouteHubSection({ tourId, market, focusRouteId, onClearFocus, onNavigateToSegment }: {
  tourId: string | null; market: string; focusRouteId: string | null; onClearFocus: () => void;
  onNavigateToSegment: (routeId: string) => void;
}) {
  const [minDays, setMinDays] = useState("");
  const [maxDays, setMaxDays] = useState("");
  const [hubSearch, setHubSearch] = useState("");
  const [offset, setOffset] = useState(0);
  useEffect(() => { setOffset(0); }, [tourId, market, minDays, maxDays, hubSearch]);

  // Same fix as Segment's identical bug (found live via this task's own Playwright verify): fetch
  // a much larger page when a focus filter is active, so the cross-nav target isn't missed just
  // because it falls past PAGE_SIZE on the default, unfiltered query.
  const hasFocus = !!focusRouteId;
  const { data, loading, error, reload } = usePlatformFetch<{ data: RouteRow[]; total: number }>(
    "/api/admin/dashboard/routes",
    { tour_id: tourId ?? undefined, market: market || undefined, min_days: minDays || undefined,
      max_days: maxDays || undefined, hub_name_search: hubSearch || undefined,
      limit: hasFocus ? 200 : PAGE_SIZE, offset: hasFocus ? 0 : offset },
  );
  const [hubOffset, setHubOffset] = useState(0);
  useEffect(() => { setHubOffset(0); }, [tourId, market]);
  const hubFetch = usePlatformFetch<{ data: HubRow[]; total: number }>(
    "/api/admin/dashboard/hubs",
    { tour_id: tourId ?? undefined, market: market || undefined, limit: PAGE_SIZE, offset: hubOffset },
  );

  const filteredRows = focusRouteId ? (data?.data ?? []).filter(r => r.route_id === focusRouteId) : (data?.data ?? []);
  const [expandedRouteId, setExpandedRouteId] = useState<string | null>(null);

  return (
    <>
      <div style={{ fontSize: 13, fontWeight: 600, color: A.body, marginBottom: 6 }}>Route</div>
      <div style={{ fontSize: 11.5, color: A.muted2, marginBottom: 10 }}>
        One tour's own journey — a consecutive-day span of that tour's ranked Segments.
      </div>
      {/* AA-557 F.11 — Nghiệp confirmed this reading directly; noted so admins don't need to ask. */}
      <div style={{ fontSize: 11.5, color: A.muted, marginBottom: 10, fontStyle: "italic" }}>
        Each row = one Tour&apos;s ranked-Segment journey for one Market (same Route content,
        market-specific Score).
      </div>
      {/* AA-557 F.9 — sticky filter row, now with a real visual edge. */}
      <div style={stickyFilterBarStyle}>
        <input style={{ ...inputStyle, width: 100 }} type="number" min={1} placeholder="Min days" value={minDays} onChange={e => setMinDays(e.target.value)} />
        <input style={{ ...inputStyle, width: 100 }} type="number" min={1} placeholder="Max days" value={maxDays} onChange={e => setMaxDays(e.target.value)} />
        <input style={inputStyle} placeholder="Search Route name…" value={hubSearch} onChange={e => setHubSearch(e.target.value)} />
        {focusRouteId && <Btn variant="ghost" size="sm" onClick={onClearFocus}>Clear filter (from Segment)</Btn>}
        {!tourId && (
          <span style={{ fontSize: 11.5, color: A.muted2, fontStyle: "italic" }}>
            Showing current Routes only — pick a Tour to see superseded versions too.
          </span>
        )}
      </div>
      {/* AA-554 G.21 — shared market legend (same one Segment/Score already render). */}
      <MarketLegend />
      {error ? <ErrorState message={error} onRetry={reload} /> :
        loading ? <LoadingScreen msg="Loading Routes…" /> :
        (!data || data.total === 0 || (focusRouteId != null && filteredRows.length === 0)) ? (
          <EmptyState title="No Routes match this filter" body="Route detection hasn't run for this Tour/Market yet, or found no consecutive-day span of ranked Segments." />
        ) : (
        <>
          {/* AA-557 F.10 — real table: sortable + per-column filter. F.12 — click a row (or its
              own "Days" cell) to expand a real per-Day breakdown below it. */}
          <AuditTable sortable rows={filteredRows} rowKey={r => `${r.route_id}-${r.market ?? "none"}`} columns={[
            { key: "status", label: "Status", render: r => r.superseded_at
              // AA-564 1.4 — SUPERSEDED had no explanation for anyone not reading route_detection.py.
              ? <span title="This Route's identity (Tour + day-span) was recomputed with a different shape or no longer qualifies — the old version is kept, never deleted, so any Slate proposal still pointing at it keeps resolving instead of breaking.">
                  <Badge color="gray">superseded v{r.version}</Badge>
                </span>
              : <Badge color="green">current{r.version > 1 ? ` v${r.version}` : ""}</Badge>,
              sortValue: r => r.superseded_at ? 1 : 0 },
            { key: "tour", label: "Tour", render: r => r.tour_name ?? "—",
              sortValue: r => r.tour_name ?? "", filterValue: r => r.tour_name ?? "" },
            // AA-557 F.9 — was mislabeled "Hub Name"; this column is the Route's OWN name
            // (Hub — a genuinely different concept — has its own table + column below).
            { key: "routeName", label: "Route Name", render: r => r.hub_name,
              sortValue: r => r.hub_name ?? "", filterValue: r => r.hub_name ?? "" },
            { key: "market", label: "Market", render: r => r.market ? <span title={marketTitle(r.market)}>{r.market}</span> : "—",
              sortValue: r => r.market ?? "", filterValue: r => r.market ?? "" },
            {
              key: "days", label: "Days", render: r => (
                <button onClick={() => setExpandedRouteId(id => id === r.route_id ? null : r.route_id)}
                  style={{ background: "none", border: "none", padding: 0, cursor: "pointer", color: A.gold, font: "inherit" }}
                  title="View this Route's day-by-day breakdown">
                  {r.first_day}–{r.last_day} {expandedRouteId === r.route_id ? "▲" : "▼"}
                </button>
              ),
              sortValue: r => r.first_day,
            },
            {
              key: "segments", label: "Segments", render: r => {
                const n = (r.ordered_segment_ids || []).length;
                // AA-554 G.22 — link into Segment, filtered to this Route's own ordered_segment_ids
                // (via the same focusRouteId cross-filter SegmentSection's route_id column already
                // narrows by — reused, not a new filter mechanism).
                return n > 0 ? (
                  <button onClick={() => onNavigateToSegment(r.route_id)}
                    style={{ background: "none", border: "none", padding: 0, cursor: "pointer", color: A.gold, textAlign: "left", font: "inherit" }}
                    title="View this Route's member Segments">
                    {n} segment{n !== 1 ? "s" : ""} ↗
                  </button>
                ) : 0;
              },
              sortValue: r => (r.ordered_segment_ids || []).length,
            },
            { key: "score", label: "Score", render: r => r.score ?? "—", sortValue: r => r.score },
            { key: "created", label: "Created", render: r => new Date(r.created_at).toLocaleString(), sortValue: r => r.created_at },
          ] as Col<RouteRow>[]} />
          {expandedRouteId && <RouteDayBreakdown routeId={expandedRouteId} />}
          {!hasFocus && <PageFooter total={data.total} offset={offset} pageSize={PAGE_SIZE} onOffset={setOffset} />}
        </>
      )}

      <div style={{ fontSize: 13, fontWeight: 600, color: A.body, marginTop: 28, marginBottom: 6 }}>Hub</div>
      <div style={{ fontSize: 11.5, color: A.muted2, marginBottom: 10 }}>
        2+ tours' shared journey — grouped automatically once route detection finds enough
        overlapping Segments between them.
      </div>
      {hubFetch.error ? <ErrorState message={hubFetch.error} onRetry={hubFetch.reload} /> :
        hubFetch.loading ? <LoadingScreen msg="Loading Hubs…" /> :
        (!hubFetch.data || hubFetch.data.total === 0) ? (
          <EmptyState title="No Hub yet" body="No Hub yet — needs 2+ tours sharing a route segment. Route detection groups tours into a Hub automatically once that happens; nothing to trigger here." />
        ) : (
        <>
          <AuditTable rows={hubFetch.data.data} rowKey={r => r.hub_id} columns={[
            { key: "hub", label: "Hub name", render: r => r.hub_name },
            { key: "tours", label: "Tours", render: r => (r.tour_names && r.tour_names.length > 0) ? r.tour_names.join(", ") : "—" },
            { key: "routes", label: "Routes", render: r => r.route_count },
            { key: "created", label: "Created", render: r => new Date(r.created_at).toLocaleString() },
            { key: "updated", label: "Updated", render: r => new Date(r.updated_at).toLocaleString() },
          ] as Col<HubRow>[]} />
          <PageFooter total={hubFetch.data.total} offset={hubOffset} pageSize={PAGE_SIZE} onOffset={setHubOffset} />
        </>
      )}
    </>
  );
}

interface RouteDayRow { day: number | null; segments: { segment_id: string; canonical_place: string | null; canonical_action: string | null }[]; }

// AA-557 F.12 — real per-Day breakdown, backed by GET /admin/dashboard/routes/{route_id}/days
// (see that endpoint's own docstring for why day-per-segment isn't on the route row itself and
// has to be re-derived from acp_contract.tour_atoms.itinerary_day).
function RouteDayBreakdown({ routeId }: { routeId: string }) {
  const [data, setData] = useState<{ days: RouteDayRow[]; found: boolean } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true); setError(null); setData(null);
    fetchJson<{ days: RouteDayRow[]; found: boolean }>(`/api/admin/dashboard/routes/${encodeURIComponent(routeId)}/days`)
      .then(setData)
      .catch(e => setError(String(e.message || e)))
      .finally(() => setLoading(false));
  }, [routeId]);

  return (
    <Card style={{ marginTop: -1, borderTop: "none", borderTopLeftRadius: 0, borderTopRightRadius: 0, padding: "14px 18px" }}>
      {loading ? <div style={{ fontSize: 12, color: A.muted2 }}>Loading day breakdown…</div> :
        error ? <div style={{ fontSize: 12, color: A.red }}>Could not load day breakdown: {error}</div> :
        (!data || !data.found || data.days.length === 0) ? (
          <div style={{ fontSize: 12, color: A.muted2 }}>No day-level data for this Route.</div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {data.days.map((d, i) => (
              <div key={i} style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
                <div style={{
                  flexShrink: 0, width: 62, fontSize: 11, fontFamily: mono, fontWeight: 600,
                  color: A.gold, background: A.goldTint, borderRadius: 6, padding: "4px 8px", textAlign: "center",
                }}>
                  {d.day != null ? `Day ${d.day}` : "Day —"}
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {d.segments.map(s => (
                    <span key={s.segment_id} style={{
                      fontSize: 12, color: A.body, background: A.card, border: `1px solid ${A.line}`,
                      borderRadius: 6, padding: "3px 9px",
                    }}>
                      {s.canonical_place ?? "—"}{s.canonical_action ? ` — ${s.canonical_action}` : ""}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
    </Card>
  );
}

function PageFooter({ total, offset, pageSize, onOffset }: {
  total: number; offset: number; pageSize: number; onOffset: (o: number) => void;
}) {
  if (total <= pageSize) return null;
  const page = Math.floor(offset / pageSize) + 1;
  const pages = Math.ceil(total / pageSize);
  return (
    <div style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: 12, marginTop: 16 }}>
      <Btn variant="secondary" size="sm" disabled={offset === 0} onClick={() => onOffset(Math.max(0, offset - pageSize))}>Previous</Btn>
      <span style={{ fontSize: 12, color: A.muted, fontFamily: mono }}>Page {page} / {pages} ({total} total)</span>
      <Btn variant="secondary" size="sm" disabled={offset + pageSize >= total} onClick={() => onOffset(offset + pageSize)}>Next</Btn>
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════
// Section 05 — Slate: UNCHANGED, still per-Tour (AA-550 A.3 — real per-tenant exception)
// ══════════════════════════════════════════════════════════════════════════

interface SlateRow {
  subject_id: string; tenant_name: string | null; channel: string; state: string; score: number | null;
  segment_id: string | null; route_id: string | null; created_at: string;
  // AA-564 2.1 — real topic name, joined server-side the same way the Tenant Portal already does
  // (services/acp_shared/slate.py::fetch_slate()) instead of a raw segment_id/route_id.
  canonical_place: string | null; canonical_action: string | null; hub_name: string | null;
  tour_name: string | null; segment_count: number | null;
}

interface TenantOption { tenant_id: string; name: string; }

const SLATE_STATE_COLOR: Record<string, "gray" | "blue" | "green" | "red"> = {
  proposed: "gray", picked: "blue", used: "green", cut: "red",
};

// AA-554 H.24 — explains all 4 states via a tooltip on each stat-bar badge. `used` text reflects
// this build's own H.1 wiring (no longer "not yet active" — it's real now); `cut` still says so,
// since H.2 only prepared the backend, no tenant UI button exists yet (see the AA-554 child issue).
const SLATE_STATE_TOOLTIP: Record<string, string> = {
  proposed: "System-proposed — this Segment/Route cleared the channel's Bar; the tenant hasn't acted on it yet.",
  picked: "The tenant chose to write this proposal (created its T8 Angle Gate request).",
  used: "A content_piece was successfully created from this proposal — it genuinely became content.",
  cut: "The tenant declined this proposal via the \"Cut\" button in their own Slate (AA-556).",
};

const SLATE_STATE_ORDER = ["proposed", "picked", "used", "cut"] as const;

// AA-561 1e — Nghiệp confirmed the AA-557 G.14 one-liner + column-header tooltips (below) were
// still too technical for a non-code reader ("Kind: Segment" with no explanation). Replaced with
// a plain-language intro block (what Slate IS, in one paragraph) + a full legend for the 4 states
// and 2 Kind values, always visible above the table — not a hover-only tooltip, so it reads
// without the reader needing to know to hover. Keeps the AA-557 G.14 tenant-portal pointer too.
function SlateExplainerNote() {
  return (
    <Card style={{ padding: "14px 18px", marginBottom: 14, background: A.card }}>
      <div style={{ fontSize: 13, color: A.body, lineHeight: 1.55, marginBottom: 10 }}>
        <strong>What is Slate?</strong> It&apos;s the list of topic ideas the pipeline has proposed
        for a tenant to write about — each one already scored and checked against that Channel&apos;s
        minimum bar. This page lets Admin view any one tenant's proposals (pick a Tenant below);
        a tenant only ever sees and acts on their own, in their portal's Social Content page
        (<code style={{ fontFamily: mono }}>/portal/t7-planning</code>).
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "6px 18px", fontSize: 11.5, color: A.muted }}>
        <span><strong>proposed</strong> = the system suggested it, tenant hasn&apos;t acted yet</span>
        <span><strong>picked</strong> = tenant chose to write it</span>
        <span><strong>used</strong> = it actually became a written piece</span>
        <span><strong>cut</strong> = tenant declined it</span>
        <span style={{ borderLeft: `1px solid ${A.line}`, paddingLeft: 18 }}>
          <strong>Kind — Segment</strong> = idea about one specific place/moment
        </span>
        <span><strong>Kind — Route</strong> = idea about a whole multi-day journey</span>
      </div>
    </Card>
  );
}

// AA-564 2.1 — same title logic as the Tenant Portal's SlateTab.tsx:295-297, so Admin and Tenant
// read identically instead of Admin showing a generic "Segment"/"Route" label.
function slateTopicTitle(r: SlateRow): string {
  if (r.route_id) return r.hub_name ?? "Untitled journey";
  return [r.canonical_place, r.canonical_action].filter(Boolean).join(" — ") || "Untitled moment";
}

const CHANNELS = ["blog", "linkedin", "facebook", "instagram", "tiktok", "email", "landing_page", "ads"];

function SlateSection() {
  // AA-564 2.2 — Slate is genuinely per-tenant (ADR-0003); this section now picks a Tenant, not a
  // Tour, and no longer depends on the page's shared Tour/Market filter at all.
  const [selectedTenant, setSelectedTenant] = useState<string | null>(null);
  const [selectedChannel, setSelectedChannel] = useState("");
  const { data: tenantsData } = usePlatformFetch<{ tenants: TenantOption[] }>("/api/admin/tenants", {}, true);
  const tenants = tenantsData?.tenants ?? [];

  const { data, loading, error, reload } = usePlatformFetch<{ data: SlateRow[]; total: number; by_state: Record<string, number> }>(
    "/api/admin/dashboard/slate", { tenant_id: selectedTenant ?? undefined, channel: selectedChannel || undefined }, !!selectedTenant,
  );

  const tenantPicker = (
    <div style={{ display: "flex", gap: 10, marginBottom: 14, alignItems: "center", flexWrap: "wrap" }}>
      <span style={{ fontSize: 12, color: A.muted }}>Tenant:</span>
      <select value={selectedTenant ?? ""} onChange={e => setSelectedTenant(e.target.value || null)} style={{ ...selectStyle, minWidth: 200, fontWeight: 600 }}>
        <option value="">Choose a tenant…</option>
        {tenants.map(t => <option key={t.tenant_id} value={t.tenant_id}>{t.name}</option>)}
      </select>
      <span style={{ fontSize: 12, color: A.muted }}>Channel:</span>
      <select value={selectedChannel} onChange={e => setSelectedChannel(e.target.value)} style={selectStyle}>
        <option value="">All channels</option>
        {CHANNELS.map(c => <option key={c} value={c}>{c}</option>)}
      </select>
    </div>
  );

  if (!selectedTenant) {
    return <><SlateExplainerNote />{tenantPicker}<EmptyState title="Select a Tenant" body="Slate is tenant-specific — pick a Tenant above to see their proposals. Each row shows its own originating Tour/Segment/Route, so no Tour needs to be chosen first." /></>;
  }
  if (error) return <><SlateExplainerNote />{tenantPicker}<ErrorState message={error} onRetry={reload} /></>;
  if (loading) return <><SlateExplainerNote />{tenantPicker}<LoadingScreen msg="Loading Slate…" /></>;
  if (!data || data.total === 0) {
    return <><SlateExplainerNote />{tenantPicker}<EmptyState title="No Slate proposals yet" body="No proposals yet for this tenant — Slate proposes a Subject once a Segment/Route clears a Channel's Bar." /></>;
  }
  return (
    <>
      <SlateExplainerNote />
      {tenantPicker}
      {/* AA-554 H.23 — sticky header stat bar, same mechanism Segment/Score's filter rows use.
          H.3 — CUT badge is NOT hidden (shows the real count). AA-556 removed the "coming soon"
          note that used to sit under it, now that the tenant-facing Cut button is real. */}
      <div style={{ display: "flex", gap: 14, marginBottom: 14, flexWrap: "wrap", alignItems: "flex-start",
        position: "sticky", top: 0, background: A.bg, zIndex: 5, paddingTop: 4, paddingBottom: 10 }}>
        {SLATE_STATE_ORDER.map(state => (
          <div key={state}>
            <span title={SLATE_STATE_TOOLTIP[state]} style={{ cursor: "help" }}>
              <Badge color={SLATE_STATE_COLOR[state] ?? "gray"}>{state}: {data.by_state[state] ?? 0}</Badge>
            </span>
          </div>
        ))}
      </div>
      {/* AA-554 H.25 — Score here is copied as-is from Segment's total_rank / Route's score at
          proposal time, never recomputed by Slate itself. */}
      <div style={{ fontSize: 12, color: A.muted, marginBottom: 10 }}>
        Score is copied as-is from the Segment's total rank or Route's score at proposal time —
        Slate never recalculates it.
      </div>
      {/* AA-557 G.13 — real table: sortable + per-column filter. */}
      <AuditTable sortable rows={data.data} rowKey={r => r.subject_id} columns={[
        { key: "channel", label: "Channel", render: r => r.channel,
          sortValue: r => r.channel, filterValue: r => r.channel },
        {
          key: "state", label: "State", render: r => (
            <span title={SLATE_STATE_TOOLTIP[r.state]} style={{ cursor: "help" }}>
              <Badge color={SLATE_STATE_COLOR[r.state] ?? "gray"}>{r.state}</Badge>
            </span>
          ),
          sortValue: r => r.state, filterValue: r => r.state,
        },
        { key: "score", label: "Score", render: r => r.score ?? "—", sortValue: r => r.score },
        {
          key: "kind", label: "Kind", render: r => (
            <span title={r.route_id
              ? "This idea covers a whole multi-day Route (a journey), not just one place."
              : "This idea is about one specific place/moment (a Segment)."}
              style={{ cursor: "help" }}>
              {r.route_id ? "Route" : "Segment"}
            </span>
          ),
          sortValue: r => r.route_id ? "Route" : "Segment", filterValue: r => r.route_id ? "Route" : "Segment",
        },
        {
          // AA-564 2.1 — the whole point of this fix: a real topic name + its originating Tour
          // (and segment count for Route-based ideas), instead of a generic "Segment"/"Route" label.
          key: "topic", label: "Topic", render: r => (
            <div>
              <div style={{ fontWeight: 600, color: A.ink }}>{slateTopicTitle(r)}</div>
              <div style={{ fontSize: 11, color: A.muted2, marginTop: 2 }}>
                ↳ tour: &quot;{r.tour_name || "—"}&quot;{r.segment_count != null ? ` · ${r.segment_count} segments` : ""}
              </div>
            </div>
          ),
          sortValue: r => slateTopicTitle(r), filterValue: r => `${slateTopicTitle(r)} ${r.tour_name ?? ""}`,
        },
        { key: "created", label: "Proposed", render: r => new Date(r.created_at).toLocaleString(),
          sortValue: r => r.created_at },
      ] as Col<SlateRow>[]} />
    </>
  );
}

// ══════════════════════════════════════════════════════════════════════════
// Page shell — sticky header (common Tour+Market filter + stat bar) + sticky inner nav (01-05)
// ══════════════════════════════════════════════════════════════════════════

// AA-575 — this page uses useSearchParams() (`?section=` deep-link from Content Trace's shared
// sub-nav, see SocialContentSubNav.tsx), which requires a Suspense boundary or `next build` fails
// prerendering this route (same pattern as frontend/app/(tenant)/portal/t4-pool/page.tsx).
export default function AtomCurationDashboardPage() {
  return (
    <Suspense>
      <AtomCurationDashboard />
    </Suspense>
  );
}

function AtomCurationDashboard() {
  const searchParams = useSearchParams();
  const sectionParam = searchParams.get("section");
  const initialSection: SectionKey =
    sectionParam && VALID_SECTIONS.has(sectionParam) ? (sectionParam as SectionKey) : "atomize";

  const [summary, setSummary] = useState<Summary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [selectedTour, setSelectedTour] = useState<string | null>(null);
  const [selectedMarket, setSelectedMarket] = useState("");
  const [activeSection, setActiveSection] = useState<SectionKey>(initialSection);
  const [stats, setStats] = useState<DashboardSummary | null>(null);
  const [statsLoading, setStatsLoading] = useState(true);
  // AA-554 E.12/F.17 — cross-navigation between sections: Segment's Route badge sets
  // `focusRouteId` and jumps to Route/Hub; Score's row link sets `focusSegmentId` and jumps to
  // Segment. Both sections filter their already-fetched page client-side (no new backend
  // query param) and clear when the user picks a section tab manually.
  const [focusRouteId, setFocusRouteId] = useState<string | null>(null);
  const [focusSegmentId, setFocusSegmentId] = useState<string | null>(null);

  function gotoSection(key: SectionKey) {
    setActiveSection(key);
    setFocusRouteId(null);
    setFocusSegmentId(null);
  }

  const loadSummary = useCallback(() => {
    setSummaryLoading(true);
    fetchJson<Summary>("/api/admin/atoms/summary")
      .then(setSummary)
      .catch(() => {})
      .finally(() => setSummaryLoading(false));
  }, []);
  useEffect(() => { loadSummary(); }, [loadSummary]);

  // AA-551 — header stat bar, re-fetched whenever the common Tour/Market filter changes,
  // independent of which section tab is open (GET /admin/dashboard/summary).
  useEffect(() => {
    setStatsLoading(true);
    const qs = new URLSearchParams();
    if (selectedTour) qs.set("tour_id", selectedTour);
    if (selectedMarket) qs.set("market", selectedMarket);
    fetchJson<DashboardSummary>(`/api/admin/dashboard/summary?${qs}`)
      .then(setStats)
      .catch(() => {})
      .finally(() => setStatsLoading(false));
  }, [selectedTour, selectedMarket]);

  const selectedTourMeta = summary?.by_tour.find(t => t.tour_id === selectedTour) ?? null;

  return (
    <div style={{ display: "flex", minHeight: "100vh", background: A.bg, fontFamily: sans }}>
      <AdminSidebar />
      {/* AA-551 sticky fix (AA-550 A.4): header is a normal, non-scrolling flex item OUTSIDE
          the scroll region — the original bug was a `position: sticky` header with no defined
          scroll-container relationship, not a missing style. Only the inner section-nav below
          still uses `sticky`, now correctly scoped to its own immediate scroll container. Both
          verified by a real Playwright scroll test post-build (see implementation notes). */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", height: "100vh" }}>
        <div style={{ flexShrink: 0, background: A.bg, padding: "24px 32px 16px", borderBottom: `1px solid ${A.line}` }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end", flexWrap: "wrap", gap: 12, marginBottom: 16 }}>
            <div>
              <h1 style={{ fontFamily: serif, fontSize: 26, fontWeight: 500, color: A.ink, margin: 0 }}>
                Social Content
              </h1>
              <div style={{ fontSize: 12, color: A.muted, marginTop: 4 }}>
                Platform-wide monitoring — Atomize is the only section AA acts on; 02–05 show what
                the pipeline has already produced across ALL tours, not just one. Per-tenant
                write/review/publish activity moved to{" "}
                <a href="/admin/tenant-activity" style={{ color: A.gold }}>Content Trace</a>.
              </div>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
              <span style={{ fontSize: 12, color: A.muted }}>Tour:</span>
              <select
                value={selectedTour ?? ""}
                onChange={e => setSelectedTour(e.target.value || null)}
                style={{ ...selectStyle, minWidth: 200, fontWeight: 600 }}
              >
                {/* AA-554 A.3 — copy reflects the real behavior of this filter (narrows the view;
                    it's never required to see data, unlike Slate below) instead of implying a
                    Tour must be picked. */}
                <option value="">All tours (or narrow to one)</option>
                {(summary?.by_tour ?? []).map(t => (
                  <option key={t.tour_id} value={t.tour_id}>{t.tour_name}</option>
                ))}
              </select>
              <span style={{ fontSize: 12, color: A.muted }}>Market:</span>
              <select value={selectedMarket} onChange={e => setSelectedMarket(e.target.value)} style={{ ...selectStyle, minWidth: 130 }}>
                <option value="">All markets</option>
                {MARKETS.map(m => <option key={m} value={m}>{m}</option>)}
              </select>
              {selectedTourMeta && selectedTourMeta.lifecycle_stage !== "active" && (
                <Badge color={LIFECYCLE_COLOR[selectedTourMeta.lifecycle_stage]}>{selectedTourMeta.lifecycle_stage}</Badge>
              )}
            </div>
          </div>

          {/* Header stat bar — AA-551, AA-550 mục F point 3/4: auto-updates with the filter above. */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(6, 1fr)", gap: 10 }}>
            {([
              ["Tours", stats?.tour_count],
              ["Atoms", stats?.atom_count],
              ["Segments", stats?.segment_count],
              ["Score rows", stats?.score_count],
              ["Routes", stats?.route_count],
              ["Hubs", stats?.hub_count],
            ] as [string, number | undefined][]).map(([label, value]) => (
              <div key={label} style={{
                background: A.card, border: `1px solid ${A.line}`, borderRadius: 8, padding: "8px 12px",
              }}>
                <div style={{ fontSize: 10.5, color: A.muted, textTransform: "uppercase", letterSpacing: "0.04em" }}>{label}</div>
                <div style={{ fontFamily: mono, fontSize: 17, fontWeight: 600, color: A.ink }}>
                  {statsLoading ? "…" : (value ?? "—")}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* AA-564 1.1 — `minHeight: 0` is required here: a flex column's default `min-height:auto`
            makes this item grow to its content's min-content size (which exceeds the viewport once
            the atom list/tables get tall) instead of shrinking to its flex-basis and scrolling
            internally, which pushed the overflow up to the document — dragging the header (which
            relies on being outside this scroll region, not on `position:sticky`) off-screen on a
            real full-page scroll. This is the actual root cause AA-554/AA-557 both missed. */}
        <div style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "20px 32px 32px" }}>
          <div className="a527-dash-body" style={{ display: "flex", gap: 20, alignItems: "flex-start" }}>
            {/* AA-575 — inner-sidebar extracted into a shared component (was JSX embedded only
                in this page, which is exactly why Content Trace (06) had no sub-nav at all: it's
                a genuinely separate route/page with its own render tree, never this page's inline
                markup). `onSelectSection` keeps 01-05 as instant same-page tab switches here — see
                SocialContentSubNav.tsx for why Content Trace renders the same list differently
                (real links, no local tab state to hook into). */}
            <SocialContentSubNav active={activeSection} onSelectSection={gotoSection} />

            <div style={{ flex: 1, minWidth: 0 }}>
              {activeSection === "atomize" && (
                <AtomizeSection
                  summary={summary} summaryLoading={summaryLoading}
                  selectedTour={selectedTour} onTourChange={setSelectedTour}
                  onSummaryChange={loadSummary}
                />
              )}
              {activeSection === "segment" && (
                <SegmentSection tourId={selectedTour} market={selectedMarket}
                  focusRouteId={focusRouteId} focusSegmentId={focusSegmentId}
                  onClearFocus={() => { setFocusRouteId(null); setFocusSegmentId(null); }}
                  onNavigateToRoute={routeId => { setFocusRouteId(routeId); setActiveSection("route_hub"); }} />
              )}
              {activeSection === "score" && (
                <ScoreSection tourId={selectedTour} market={selectedMarket}
                  onNavigateToSegment={segmentId => { setFocusSegmentId(segmentId); setActiveSection("segment"); }} />
              )}
              {activeSection === "route_hub" && (
                <RouteHubSection tourId={selectedTour} market={selectedMarket}
                  focusRouteId={focusRouteId} onClearFocus={() => setFocusRouteId(null)}
                  onNavigateToSegment={routeId => { setFocusRouteId(routeId); setActiveSection("segment"); }} />
              )}
              {activeSection === "slate" && <SlateSection />}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
