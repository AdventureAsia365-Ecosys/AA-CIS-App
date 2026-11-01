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
// 05 Slate is NOT part of the platform-wide fix (AA-550 K.3, confirmed real schema:
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
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import {
  Trash2, ChevronDown, ChevronRight, Layers, Milestone,
} from "lucide-react";
import SocialContentSubNav, { SocialContentSplit, type SectionKey } from "../_components/SocialContentSubNav";
import { fetchJson, EmptyState, ErrorState, AuditTable, Col } from "../_components/auditPanels";
import {
  Button, PageFrame, PageFrameBody, PageFrameHeader, PageHeader, useFormat, useT, Checkbox, Input, Select,
  K, mono, sans, Section, Badge, LoadingScreen,
  type Tone,
  DataTable, IconButton, KX, LinkButton, ListButton, Pressable, useIsPhone, type ColumnDef,
} from "../../_kit";
import { phoneCardColumns } from "@/lib/tableSort";
import { SeedLine, type SeedInfo } from "../_components/SeedLine";
import { TenantSelect } from "../_components/TenantSelect";
import { AT } from "./messages";

const MARKETS = ["US", "UK", "AU", "DE", "FR", "NL"];

// AA-554 E.13 — shared market-code legend, used by Segment + Score (Route/Hub's own copy of this
// is part of the deferred G redesign, not added here).
// AA-994: market names are message keys (messages.ts), named in the UI language.
const MARKET_NAMES: Record<string, "marketUS" | "marketUK" | "marketAU" | "marketDE" | "marketFR" | "marketNL"> = {
  US: "marketUS", UK: "marketUK", AU: "marketAU",
  DE: "marketDE", FR: "marketFR", NL: "marketNL",
};

function MarketLegend() {
  const t = useT(AT);
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 10, fontSize: 11, color: K.muted2, marginBottom: 10 }}>
      {Object.entries(MARKET_NAMES).map(([code, name]) => (
        <span key={code}><strong style={{ color: K.muted }}>{code}</strong> = {t(name)}</span>
      ))}
    </div>
  );
}

/** The market's name in the UI language (a title tooltip). */
function useMarketTitle(): (code: string | null) => string | undefined {
  const t = useT(AT);
  return (code) => (code && MARKET_NAMES[code] ? t(MARKET_NAMES[code]) : undefined);
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
  // AA-754 — the distinctiveness breakdown was removed with the signal itself.
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
// AA-702 — Route scores are averages of Segment total ranks (1392.3333333333333 on screen).
function fmtScore(n: number | null | undefined): string {
  return n == null ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: 1 });
}

const VALID_SECTIONS: Set<string> = new Set(["atomize", "segment", "score", "route_hub", "slate"]);

const LIFECYCLE_COLOR: Record<string, Tone> = {
  active: "success", phasing_out: "warning", retired: "neutral",
};

const selectStyle: React.CSSProperties = {
  padding: "8px 12px", background: K.card, border: `1px solid ${K.line}`, borderRadius: 8,
  fontSize: 13, fontFamily: sans, color: K.body, cursor: "pointer",
};

const inputStyle: React.CSSProperties = {
  padding: "7px 10px", background: K.card, border: `1px solid ${K.line}`, borderRadius: 8,
  fontSize: 12.5, fontFamily: sans, color: K.body, width: 140,
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
  // AA-754 — the per-atom distinctiveness field was removed end-to-end; the API no longer
  // returns it.
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

const PAGE_SIZE = 50;
const TOURS_PAGE_SIZE = 30; // AA-554 B.6 — Tours sidebar "Load more" window size

function isLegacyScope(scope: string): boolean { return scope !== "platform"; }

function OwnerBadge({ scope }: { scope: string }) {
  const t = useT(AT);
  return isLegacyScope(scope) ? <Badge tone="warning">{t("legacyOwned")}</Badge> : <Badge tone="accent">{t("platform")}</Badge>;
}

function AtomizeSection({ summary, summaryLoading, selectedTour, onTourChange, onSummaryChange }: {
  summary: Summary | null; summaryLoading: boolean;
  selectedTour: string | null; onTourChange: (t: string | null) => void;
  onSummaryChange: () => void;
}) {
  const t = useT(AT);
  // < 768px the tour list stacks above the atoms (was the `.aa-curation-split` media query, AA-1006).
  const phone = useIsPhone();
  const [unreviewedOnly, setUnreviewedOnly] = useState(false);
  const [lifecycleFilter, setLifecycleFilter] = useState("");
  // AA-1006: the loaded list remembers which filters (and retry) it belongs to; a new key shows the loader until its
  // own first page arrives, so no effect sets state before the answer.
  const [reloadTick, setReloadTick] = useState(0);
  const listKey = JSON.stringify([unreviewedOnly, selectedTour, lifecycleFilter, reloadTick]);
  const [list, setList] = useState<{ key: string; atoms: Atom[]; total: number; error: string | null } | null>(null);
  const current = list?.key === listKey ? list : null;
  const atoms = current?.atoms ?? [];
  const total = current?.total ?? 0;
  const atomsLoading = current == null;
  const atomsError = current?.error ?? null;
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
  // AA-687 — the a3_atomize jobs the last trigger enqueued, linked to the Jobs page (kept after the
  // poll ends so a slow or failed run can still be opened).
  const [atomizeJobIds, setAtomizeJobIds] = useState<string[]>([]);
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
      const body: { tour_ids: string[]; job_ids?: string[] } = await res.json();
      setAtomizeJobIds(body.job_ids ?? []);
      setAtomizeRunning(
        "all" in target ? t("atomizingN", body.tour_ids.length) : t("atomizing"),
      );
      // Poll until every accepted tour has dropped off the unatomized list (or ~10 min elapses —
      // run_a3_atomize() can take a while, up to one LLM call per itinerary day, per tour).
      const pending = new Set(body.tour_ids);
      let attempts = 0;
      const poll = setInterval(async () => {
        attempts += 1;
        const d = await fetchJson<{ tours: { tour_id: string; tour_name: string }[] }>("/api/admin/atoms/unatomized-tours").catch(() => null);
        if (d) {
          const stillPending = new Set(d.tours.map(x => x.tour_id));
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

  const fetchAtoms = useCallback((offset: number) => {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
    if (unreviewedOnly) params.set("unreviewed_only", "true");
    if (selectedTour) params.set("tour_id", selectedTour);
    if (lifecycleFilter) params.set("lifecycle_stage", lifecycleFilter);
    return fetchJson<{ atoms: Atom[]; total: number }>(`/api/admin/atoms?${params}`);
  }, [unreviewedOnly, selectedTour, lifecycleFilter]);

  useEffect(() => {
    let alive = true;
    fetchAtoms(0)
      .then(d => { if (alive) setList({ key: listKey, atoms: d.atoms, total: d.total ?? 0, error: null }); })
      .catch(e => { if (alive) setList({ key: listKey, atoms: [], total: 0, error: String(e.message || e) }); });
    return () => { alive = false; };
  }, [fetchAtoms, listKey]);

  function loadMore() {
    setLoadingMore(true);
    fetchAtoms(atoms.length)
      .then(d => setList(prev => (prev && prev.key === listKey
        ? { ...prev, atoms: [...prev.atoms, ...d.atoms], total: d.total ?? prev.total } : prev)))
      .catch(e => setList(prev => (prev && prev.key === listKey ? { ...prev, error: String(e.message || e) } : prev)))
      .finally(() => setLoadingMore(false));
  }

  async function deleteAtom(atom: Atom) {
    if (!confirm(t("confirmDelete"))) return;
    setList(prev => (prev ? { ...prev, atoms: prev.atoms.filter(a => a.atom_id !== atom.atom_id) } : prev));
    await fetch(`/api/admin/atoms/${atom.atom_id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deleted: true }),
    });
    onSummaryChange();
  }

  // AA-564 1.3 — this block used to always read the whole-dataset `summary.total_count`/
  // `reviewed_count`, even when a single Tour was selected (the header stat bar above it does
  // filter correctly, which is why the two used to visibly disagree). `summary.by_tour` already
  // carries per-tour `atom_count`/`unreviewed_count` (GET /admin/atoms/summary, unchanged) — just
  // read that when a Tour is selected instead of the platform-wide totals.
  const selectedTourMeta = selectedTour ? (summary?.by_tour ?? []).find(x => x.tour_id === selectedTour) ?? null : null;
  const totalAtoms = selectedTourMeta ? selectedTourMeta.atom_count : (summary?.total_count ?? 0);
  const reviewedAtoms = selectedTourMeta
    ? selectedTourMeta.atom_count - selectedTourMeta.unreviewed_count
    : (summary?.reviewed_count ?? 0);

  return (
    <>
      {summaryLoading ? <LoadingScreen message={t("loadingDashboard")} /> : (
        <>
          {/* AA-601 — "content rendered" signal for the UI smoke: present only once the curation
              summary has loaded (never on the LoadingScreen), so a blank/broken page still fails. */}
          <div data-testid="admin-content-ready" style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 14, marginBottom: 6 }}>
            {[
              [t("totalAtoms"), totalAtoms, K.accent],
              [t("reviewed"), reviewedAtoms, K.success],
              // AA-754 — no distinctiveness KPI here: the signal was removed end-to-end.
            ].map(([label, value, accent]) => (
              <Section bodyStyle={{ padding: "14px 16px" }} key={label as string}>
                <div style={{ fontSize: 11.5, color: K.muted, marginBottom: 6 }}>{label}</div>
                <div style={{ fontFamily: sans, fontVariantNumeric: "tabular-nums", fontSize: 24, fontWeight: 600, color: accent as string }}>{value}</div>
              </Section>
            ))}
          </div>
          <div style={{ fontSize: 10.5, color: K.muted2, marginBottom: 14 }}>
            {selectedTourMeta
              ? t("forSelectedTour")
              : t("platformWide")}
          </div>

          {/* AA-564 3.2 — manual atomize backfill banner. Only 1 automatic trigger exists (a
              fresh publish, AA-526) — this is the only way an OLDER Master Content tour ever
              gets atomized. */}
          {unatomizedTours.length > 0 && (
            <Section style={{ marginBottom: 16, borderColor: K.accent }} bodyStyle={{ padding: "14px 16px" }}>
              <div style={{ fontSize: 13, color: K.body, marginBottom: 10 }}>
                ⚠ <strong>{t("unatomizedCount", unatomizedTours.length)}</strong>{t("unatomizedBody", unatomizedTours.length)}
              </div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <Button
                  variant="secondary" size="sm"
                  disabled={atomizeTriggering || !!atomizeRunning || !selectedTour || !unatomizedTours.some(x => x.tour_id === selectedTour)}
                  onClick={() => selectedTour && triggerAtomize({ tour_id: selectedTour })}
                >
                  {t("atomizeSelected")}
                </Button>
                <Button
                  variant="secondary" size="sm"
                  disabled={atomizeTriggering || !!atomizeRunning}
                  onClick={() => triggerAtomize({ all: true })}
                >
                  {t("atomizeAll", unatomizedTours.length)}
                </Button>
                {atomizeRunning && <span style={{ fontSize: 12, color: K.muted }}>{atomizeRunning}</span>}
                {atomizeJobIds.length > 0 && (
                  <a href={atomizeJobIds.length === 1 ? `/admin/jobs?job=${atomizeJobIds[0]}` : "/admin/jobs?kind=a3_atomize"}
                     style={{ fontSize: 12, color: K.accent }}>
                    {atomizeJobIds.length === 1 ? t("viewJob") : t("viewJobs", atomizeJobIds.length)}
                  </a>
                )}
              </div>
            </Section>
          )}

          <div style={{ display: "grid", gridTemplateColumns: phone ? "minmax(0, 1fr)" : "280px minmax(0, 1fr)", gap: 18, alignItems: "start" }}>
            {/* AA-554 B.4/B.5 — sticky (same `position: sticky, top: 0` pattern AA-551 already
                proved works for the 01-05 section-nav, within this same page's outer scroll
                container — see that inner-nav below for the identical mechanism) + taller
                viewport-relative maxHeight (was a fixed 640px, too small for 74+ tours) with its
                own internal scroll for the list itself. */}
            <Section padded={false} style={{ overflow: "hidden", position: "sticky", top: 0 }}>
              <div style={{ padding: "12px 16px", borderBottom: `1px solid ${K.line}`, fontSize: 12, fontWeight: 600, color: K.ink3, textTransform: "uppercase", letterSpacing: "0.06em" }}>
                {t("toursHeader", summary?.by_tour.length ?? 0)}
              </div>
              <div style={{ maxHeight: "calc(100vh - 260px)", overflowY: "auto" }}>
                <ListButton selected={selectedTour === null} aria-current={selectedTour === null ? "true" : undefined} onClick={() => onTourChange(null)} style={{
                  padding: "10px 16px", borderRadius: 0, borderBottom: `1px solid ${K.line2}`,
                  fontSize: 12.5, fontWeight: selectedTour === null ? 700 : 500,
                }}>
                  {t("allTours")}
                </ListButton>
                {(summary?.by_tour ?? []).slice(0, toursShown).map(tr => (
                  <ListButton selected={selectedTour === tr.tour_id} aria-current={selectedTour === tr.tour_id ? "true" : undefined} key={tr.tour_id} onClick={() => onTourChange(tr.tour_id)} style={{
                    display: "block", padding: "10px 16px", borderRadius: 0, borderBottom: `1px solid ${K.line2}`,
                  }}>
                    <div style={{ fontSize: 12.5, fontWeight: selectedTour === tr.tour_id ? 700 : 500, color: K.ink, marginBottom: 3 }}>
                      {tr.tour_name}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                      <span style={{ fontSize: 11, color: K.muted }}>{t("atomsN", tr.atom_count)}</span>
                      {tr.is_thin && <Badge tone="danger">{t("thin")}</Badge>}
                      {tr.unreviewed_count > 0 && <Badge tone="info">{t("newN", tr.unreviewed_count)}</Badge>}
                      {tr.lifecycle_stage !== "active" && <Badge tone={LIFECYCLE_COLOR[tr.lifecycle_stage]}>{tr.lifecycle_stage}</Badge>}
                      {tr.owner_scopes.map(s => <OwnerBadge key={s} scope={s} />)}
                    </div>
                    <div style={{ fontSize: 10.5, color: K.muted2, marginTop: 3 }}>
                      {t("usedInContent", tr.used_atom_count, tr.atom_count)}
                    </div>
                  </ListButton>
                ))}
                {(summary?.by_tour.length ?? 0) > toursShown && (
                  <LinkButton size="sm" onClick={() => setToursShown(n => n + TOURS_PAGE_SIZE)} style={{
                    display: "flex", width: "100%", justifyContent: "center", padding: "10px 16px",
                    borderTop: `1px solid ${K.line2}`, borderRadius: 0,
                  }}>
                    {t("loadMoreTours", Math.min(toursShown, summary?.by_tour.length ?? 0), summary?.by_tour.length ?? 0)}
                  </LinkButton>
                )}
              </div>
            </Section>

            <div>
              <div style={{ display: "flex", gap: 10, marginBottom: 14, alignItems: "center", flexWrap: "wrap" }}>
                <Select value={lifecycleFilter} onChange={e => setLifecycleFilter(e.target.value)} style={selectStyle}>
                  <option value="">{t("allLifecycle")}</option>
                  <option value="active">{t("lifecycleActive")}</option>
                  <option value="phasing_out">{t("lifecyclePhasingOut")}</option>
                  <option value="retired">{t("lifecycleRetired")}</option>
                </Select>
                <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, color: K.body, cursor: "pointer" }}>
                  <Checkbox checked={unreviewedOnly} onChange={e => setUnreviewedOnly(e.target.checked)} />
                  {t("unreviewedOnly")}
                </label>
              </div>
              {/* AA-554 C.8 — schema supports phasing_out/retired but every tour currently loaded
                  is "active" — this note is computed from the real, currently-loaded Tours list
                  (not hardcoded), so it disappears on its own the day a non-active tour exists. */}
              {(summary?.by_tour.length ?? 0) > 0 && (summary?.by_tour ?? []).every(x => x.lifecycle_stage === "active") && (
                <div style={{ fontSize: 11, color: K.muted2, marginTop: -8, marginBottom: 14 }}>
                  {t("allActive", summary?.by_tour.length ?? 0)}
                </div>
              )}

              {atomsError ? <ErrorState message={atomsError} onRetry={() => setReloadTick(n => n + 1)} /> :
                atomsLoading ? <LoadingScreen message={t("loadingAtoms")} /> : atoms.length === 0 ? (
                <EmptyState title={t("noAtoms")} body={t("noAtomsBody")} />
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
                  <Button variant="secondary" disabled={loadingMore} onClick={loadMore}>
                    {loadingMore ? t("loading") : t("loadMore", atoms.length, total)}
                  </Button>
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
  const t = useT(AT);
  return (
    <Section bodyStyle={{ padding: "14px 18px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          {showTour && <div style={{ fontSize: 11, color: K.muted2, marginBottom: 4, fontFamily: mono }}>{atom.tour_name}</div>}
          <div style={{ fontSize: 13.5, color: K.body, lineHeight: 1.5 }}>{atom.text}</div>
          <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap", alignItems: "center" }}>
            {atom.activity_type && <Badge tone="neutral">{atom.activity_type}</Badge>}
            {atom.unreviewed && <Badge tone="info">{t("new")}</Badge>}
            <OwnerBadge scope={atom.owner_scope} />
            {atom.lifecycle_stage !== "active" && <Badge tone={LIFECYCLE_COLOR[atom.lifecycle_stage]}>{atom.lifecycle_stage}</Badge>}
            {atom.recurrence != null && atom.recurrence > 0 && (
              <span style={{ fontSize: 10.5, fontFamily: mono, color: K.muted }}>{t("itineraries", atom.recurrence)}</span>
            )}
            {/* AA-554 D.10 — tooltip explaining what "used" counts. */}
            <span style={{ fontSize: 10.5, fontFamily: mono, color: atom.usage_count > 0 ? K.ink3 : K.muted2 }}
              title={t("usedTitle")}>
              {atom.usage_count > 0 ? t("usedN", atom.usage_count) : t("notUsed")}
            </span>
          </div>
        </div>
        <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
          <IconButton tone="danger" onClick={() => onDelete(atom)} label={t("remove")} style={{ color: K.danger }}>
            <Trash2 size={14} />
          </IconButton>
        </div>
      </div>
    </Section>
  );
}

function SegmentGroup({ place, action, atoms, score, routeHubName, showTour, collapsed, onToggle, onDelete }: {
  place: string; action: string; atoms: Atom[]; score: number | null; routeHubName: string | null;
  showTour: boolean; collapsed: boolean; onToggle: () => void; onDelete: (a: Atom) => void;
}) {
  const t = useT(AT);
  return (
    <div style={{ border: `1px solid ${K.line}`, borderRadius: 10, overflow: "hidden" }}>
      <Pressable aria-expanded={!collapsed} onClick={onToggle} style={{
        width: "100%", display: "flex", alignItems: "center", gap: 8, padding: "10px 14px",
        background: K.accentTint, border: "none", cursor: "pointer", textAlign: "left", flexWrap: "wrap",
      }}>
        {collapsed ? <ChevronRight size={14} color={K.muted} /> : <ChevronDown size={14} color={K.muted} />}
        <Layers size={13} color={K.accent} />
        <span style={{ fontSize: 13, fontWeight: 600, color: K.body, fontFamily: sans }}>
          {place}{action ? ` — ${action}` : ""}
        </span>
        {score != null && (
          <span style={{ fontFamily: mono, fontSize: 11, color: K.ink3, background: K.card, border: `1px solid ${K.line}`, borderRadius: 6, padding: "2px 7px" }} title={t("rankSumTitle")}>
            {t("scoreN", fmtScore(score))}
          </span>
        )}
        {routeHubName && (
          <Badge tone="accent"><Milestone size={11} style={{ verticalAlign: -2, marginRight: 3 }} />{t("partOfRoute", routeHubName)}</Badge>
        )}
        <span style={{ fontSize: 11.5, color: K.muted2, marginLeft: "auto" }}>{t("sameMoment", atoms.length)}</span>
      </Pressable>
      {!collapsed && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: 8, background: K.card }}>
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
// `enabled: !!tourId` since its backend endpoint still hard-requires `tour_id` (AA-550 K.3, real
// per-tenant exception, not touched by this task) — without this guard, deselecting the Tour
// filter would fire a request Slate's own API 422s on (found live during this task's own
// Playwright verify, fixed before merge).
/** A page offset that starts again at 0 whenever `deps` change (AA-1006: derived from the filter key, no effect
 *  that sets state). */
function useOffset(deps: unknown[]): [number, (n: number) => void] {
  const key = JSON.stringify(deps);
  const [state, setState] = useState({ key, offset: 0 });
  return [state.key === key ? state.offset : 0, (offset: number) => setState({ key, offset })];
}

function usePlatformFetch<T>(
  endpoint: string, params: Record<string, string | number | undefined>, enabled = true,
) {
  // Stable key — `params` is a fresh object every render otherwise. AA-1006: react-query owns the load state.
  const key = JSON.stringify(params);
  const q = useQuery({
    queryKey: ["atom-curation", endpoint, key],
    enabled,
    queryFn: () => {
      const qs = new URLSearchParams();
      Object.entries(JSON.parse(key) as Record<string, string | number | null>).forEach(([k, v]) => {
        if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
      });
      return fetchJson<T>(`${endpoint}?${qs}`);
    },
  });
  const data = enabled ? (q.data ?? null) : null;
  const loading = enabled && q.isPending;
  const error = enabled && q.isError ? String((q.error as Error).message || q.error) : null;
  const { refetch } = q;
  const reload = useCallback(() => { refetch(); }, [refetch]);
  return { data, loading, error, reload };
}

const filterBarStyle: React.CSSProperties = {
  display: "flex", gap: 8, marginBottom: 14, alignItems: "center", flexWrap: "wrap",
};

// AA-557 D.3/E.7/F.9 — live Playwright confirmed the previous sticky filter row (plain
// `position:sticky, background:K.bg`, no border) visually merges with whatever content scrolls
// up beneath it once stuck — the next Segment/Route group's own header sits flush against it with
// no visible seam (tests/e2e/results/aa557/D-00-segment-after-scroll-PREFIX.png). A bottom
// border + drop shadow gives it a real visual edge (the standard "elevated sticky bar" pattern)
// so scrolled content reads as passing UNDER it, not merging into it; extra paddingBottom widens
// the gap before the next card.
const stickyFilterBarStyle: React.CSSProperties = {
  ...filterBarStyle, position: "sticky", top: 0, background: K.bg, zIndex: 5,
  paddingTop: 8, paddingBottom: 14, marginBottom: 10,
  borderBottom: `1px solid ${K.line}`, boxShadow: KX.shadowSm,
};

interface SegmentRow {
  tour_id: string; tour_name: string | null;
  segment_id: string; canonical_place: string; canonical_action: string;
  member_count: number; market: string | null; total_rank: number | null; recurrence: number | null;
  excluded_reason: string | null; route_id: string | null; route_hub_name: string | null;
}

// AA-554 E.11 — one card per distinct segment_id (was one flat AuditTable row per
// (Segment, Tour, Market)); numbered in display order within the current page.
/** One line of the Segment table: a row of a Segment group, with its group and their positions. */
type SegmentLine = { g: SegmentGroupDisplay; r: SegmentRow; i: number; j: number; id: string };

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
  const t = useT(AT);
  const marketTitle = useMarketTitle();
  // A phone shows a card per row (no rows grouped under one segment cell): each card carries its own segment.
  const phone = useIsPhone();
  const [placeSearch, setPlaceSearch] = useState("");
  const [minRecurrence, setMinRecurrence] = useState("");
  const [offset, setOffset] = useOffset([tourId, market, placeSearch, minRecurrence, focusRouteId, focusSegmentId]);

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
        <Input style={inputStyle} placeholder={t("searchPlaceVerb")} value={placeSearch}
          onChange={e => setPlaceSearch(e.target.value)} />
        <Input style={{ ...inputStyle, width: 110 }} type="number" min={0} placeholder={t("minRecurrence")}
          value={minRecurrence} onChange={e => setMinRecurrence(e.target.value)} />
        {(focusRouteId || focusSegmentId) && (
          <Button variant="ghost" size="sm" onClick={onClearFocus}>{t("clearCross")}</Button>
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
        loading ? <LoadingScreen message={t("loadingSegments")} /> :
        (!data || data.total === 0 || groups.length === 0) ? (
          <EmptyState title={t("noSegments")} body={t("noSegmentsBody")} />
        ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {/* AA-1006: a kit DataTable; the Segment and Route/Hub cells show on the first row of their group only (the
              table cannot merge cells, the old rowSpan). Tour stays its own per-row column (AA-561 above). */}
          <DataTable<SegmentLine>
            tableId="atom-curation-segments"
            data={groups.flatMap((g, i) => g.rows.map((r, j) => ({ g, r, i, j, id: `${g.segmentId}-${r.tour_id}-${r.market ?? "none"}` })))}
            getRowId={(l) => l.id}
            searchable={false}
            paginate={false}
            compact
            // On a phone the tour is the card title and every card shows its segment, route / hub and number.
            columns={phoneCardColumns<ColumnDef<SegmentLine, unknown>>([
              { id: "n", header: "#", enableSorting: false, cell: ({ row: { original: l } }) => phone || l.j === 0
                ? <span style={{ fontFamily: mono, fontSize: 11, color: K.muted2 }}>{offset + l.i + 1}</span> : null },
              { id: "segment", header: t("colSegment"), enableSorting: false, cell: ({ row: { original: l } }) => phone || l.j === 0 ? (
                <div>
                  <span style={{ fontSize: 13, fontWeight: 600, color: K.body }}>
                    {l.g.place}{l.g.action ? ` — ${l.g.action}` : ""}
                  </span>
                  <div style={{ fontSize: 10.5, color: K.muted2, marginTop: 3 }}>{t("marketTourRows", l.g.rows.length)}</div>
                </div>
              ) : null },
              { id: "routeHub", header: t("colRouteHub"), enableSorting: false, cell: ({ row: { original: l } }) => !phone && l.j !== 0 ? null
                : l.g.routeHubName && l.g.routeId ? (
                  <Pressable display="inline" onClick={() => onNavigateToRoute(l.g.routeId!)} title={t("viewRoute")}>
                    <Badge tone="accent"><Milestone size={11} style={{ verticalAlign: -2, marginRight: 3 }} />{l.g.routeHubName}</Badge>
                  </Pressable>
                ) : "—" },
              { id: "tour", header: t("colTour"), enableSorting: false, cell: ({ row: { original: l } }) => l.r.tour_name ?? "—" },
              { id: "market", header: t("colMarket"), enableSorting: false, cell: ({ row: { original: l } }) =>
                l.r.market ? <span title={marketTitle(l.r.market)}>{l.r.market}</span> : "—" },
              { id: "atoms", header: t("colAtoms"), enableSorting: false, cell: ({ row: { original: l } }) => l.r.member_count },
              { id: "rank", header: t("colTotalRank"), enableSorting: false, cell: ({ row: { original: l } }) =>
                l.r.excluded_reason ? <Badge tone="neutral">{l.r.excluded_reason}</Badge> : (l.r.total_rank ?? "—") },
              { id: "recurrence", header: t("colRecurrence"), enableSorting: false, cell: ({ row: { original: l } }) => l.r.recurrence ?? "—" },
            ], "tour", phone)}
          />
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
  const t = useT(AT);
  const marketTitle = useMarketTitle();
  const [minRank, setMinRank] = useState("");
  const [maxRank, setMaxRank] = useState("");
  const [offset, setOffset] = useOffset([tourId, market, minRank, maxRank]);

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
      <div style={{ fontSize: 12, color: K.muted, marginBottom: 10 }}>
        {t("scoreIntro")}{" "}
        <span
          title={t("totalRankTitle")}
          style={{ borderBottom: `1px dotted ${K.muted2}`, cursor: "help" }}
        >
          {t("totalRankMeaning")}
        </span>
      </div>
      {/* AA-557 E.7 — sticky filter row, now with a real visual edge. */}
      <div style={stickyFilterBarStyle}>
        <Input style={{ ...inputStyle, width: 110 }} type="number" placeholder={t("minTotalRank")} value={minRank} onChange={e => setMinRank(e.target.value)} />
        <Input style={{ ...inputStyle, width: 110 }} type="number" placeholder={t("maxTotalRank")} value={maxRank} onChange={e => setMaxRank(e.target.value)} />
      </div>
      <MarketLegend />
      {error ? <ErrorState message={error} onRetry={reload} /> :
        loading ? <LoadingScreen message={t("loadingScore")} /> :
        (!data || data.total === 0) ? <EmptyState title={t("noScore")} body={t("noScoreBody")} /> : (
        <>
          {/* AA-557 E.8 — real table: sortable + per-column filter. */}
          <AuditTable sortable rows={numbered} rowKey={r => `${r.tour_id}-${r.segment_id}-${r.market ?? "none"}`} columns={[
            { key: "num", label: "#", render: r => r.__num, sortValue: r => r.__num },
            { key: "tour", label: t("colTour"), render: r => r.tour_name ?? "—",
              sortValue: r => r.tour_name ?? "", filterValue: r => r.tour_name ?? "" },
            {
              key: "place", label: t("colSegment"), render: r => r.canonical_place ? (
                // AA-554 F.17 — link into Segment, filtered to this segment_id.
                <LinkButton tone="inherit" size="inherit" weight="regular" onClick={() => onNavigateToSegment(r.segment_id)}
                  style={{ color: K.accent }} title={t("viewSegment")}>
                  {r.canonical_place} — {r.canonical_action} ↗
                </LinkButton>
              ) : "—",
              sortValue: r => r.canonical_place ?? "",
              filterValue: r => `${r.canonical_place ?? ""} ${r.canonical_action ?? ""}`,
            },
            { key: "market", label: t("colMarket"), render: r => r.market ? <span title={marketTitle(r.market)}>{r.market}</span> : "—",
              sortValue: r => r.market ?? "", filterValue: r => r.market ?? "" },
            { key: "total", label: t("colTotalRank"), render: r => r.excluded_reason ? <Badge tone="neutral">{r.excluded_reason}</Badge> : (r.total_rank ?? "—"),
              sortValue: r => r.total_rank ?? (r.excluded_reason ? Number.MAX_SAFE_INTEGER : null) },
            {
              key: "demand", label: t("colDemand"), render: r => r.demand_rank != null ? (
                // AA-554 F.16 — tooltip explaining the 3-part "#rank (volume · market)" format.
                <span title={t("demandTitle")}>
                  #{r.demand_rank} ({r.demand_volume ?? "—"} · {r.demand_market ?? "—"})
                </span>
              ) : "—",
              sortValue: r => r.demand_rank,
            },
            { key: "recurrence", label: t("colRecurrence"), render: r => r.recurrence_rank != null ? `#${r.recurrence_rank} (${r.recurrence})` : "—",
              sortValue: r => r.recurrence_rank },
            { key: "questions", label: t("colQuestions"), render: r => r.questions_rank != null ? `#${r.questions_rank} (${r.questions})` : "—",
              sortValue: r => r.questions_rank },
            { key: "said", label: t("colSaid"), render: r => r.said_rank != null ? `#${r.said_rank} (${r.said})` : "—",
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
  const t = useT(AT);
  const marketTitle = useMarketTitle();
  const { formatDateTime } = useFormat();
  const [minDays, setMinDays] = useState("");
  const [maxDays, setMaxDays] = useState("");
  const [hubSearch, setHubSearch] = useState("");
  const [offset, setOffset] = useOffset([tourId, market, minDays, maxDays, hubSearch]);

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
  const [hubOffset, setHubOffset] = useOffset([tourId, market]);
  const hubFetch = usePlatformFetch<{ data: HubRow[]; total: number }>(
    "/api/admin/dashboard/hubs",
    { tour_id: tourId ?? undefined, market: market || undefined, limit: PAGE_SIZE, offset: hubOffset },
  );

  const filteredRows = focusRouteId ? (data?.data ?? []).filter(r => r.route_id === focusRouteId) : (data?.data ?? []);
  const [expandedRouteId, setExpandedRouteId] = useState<string | null>(null);

  return (
    <>
      <div style={{ fontSize: 13, fontWeight: 600, color: K.body, marginBottom: 6 }}>{t("route")}</div>
      <div style={{ fontSize: 11.5, color: K.muted2, marginBottom: 10 }}>
        {t("routeIntro")}
      </div>
      {/* AA-557 F.11 — Nghiệp confirmed this reading directly; noted so admins don't need to ask. */}
      <div style={{ fontSize: 11.5, color: K.muted, marginBottom: 10, fontStyle: "italic" }}>
        {t("routeRow")}
      </div>
      {/* AA-557 F.9 — sticky filter row, now with a real visual edge. */}
      <div style={stickyFilterBarStyle}>
        <Input style={{ ...inputStyle, width: 100 }} type="number" min={1} placeholder={t("minDays")} value={minDays} onChange={e => setMinDays(e.target.value)} />
        <Input style={{ ...inputStyle, width: 100 }} type="number" min={1} placeholder={t("maxDays")} value={maxDays} onChange={e => setMaxDays(e.target.value)} />
        <Input style={inputStyle} placeholder={t("searchRoute")} value={hubSearch} onChange={e => setHubSearch(e.target.value)} />
        {focusRouteId && <Button variant="ghost" size="sm" onClick={onClearFocus}>{t("clearFromSegment")}</Button>}
        {!tourId && (
          <span style={{ fontSize: 11.5, color: K.muted2, fontStyle: "italic" }}>
            {t("currentOnly")}
          </span>
        )}
      </div>
      {/* AA-554 G.21 — shared market legend (same one Segment/Score already render). */}
      <MarketLegend />
      {error ? <ErrorState message={error} onRetry={reload} /> :
        loading ? <LoadingScreen message={t("loadingRoutes")} /> :
        (!data || data.total === 0 || (focusRouteId != null && filteredRows.length === 0)) ? (
          <EmptyState title={t("noRoutes")} body={t("noRoutesBody")} />
        ) : (
        <>
          {/* AA-557 F.10 — real table: sortable + per-column filter. F.12 — click a row (or its
              own "Days" cell) to expand a real per-Day breakdown below it. */}
          <AuditTable sortable rows={filteredRows} rowKey={r => `${r.route_id}-${r.market ?? "none"}`} columns={[
            { key: "status", label: t("colStatus"), render: r => r.superseded_at
              // AA-564 1.4 — SUPERSEDED had no explanation for anyone not reading route_detection.py.
              ? <span title={t("supersededTitle")}>
                  <Badge tone="neutral">{t("supersededV", r.version)}</Badge>
                </span>
              : <Badge tone="success">{t("current", r.version > 1 ? ` v${r.version}` : "")}</Badge>,
              sortValue: r => r.superseded_at ? 1 : 0 },
            { key: "tour", label: t("colTour"), render: r => r.tour_name ?? "—",
              sortValue: r => r.tour_name ?? "", filterValue: r => r.tour_name ?? "" },
            // AA-557 F.9 — was mislabeled "Hub Name"; this column is the Route's OWN name
            // (Hub — a genuinely different concept — has its own table + column below).
            { key: "routeName", label: t("colRouteName"), render: r => r.hub_name,
              sortValue: r => r.hub_name ?? "", filterValue: r => r.hub_name ?? "" },
            { key: "market", label: t("colMarket"), render: r => r.market ? <span title={marketTitle(r.market)}>{r.market}</span> : "—",
              sortValue: r => r.market ?? "", filterValue: r => r.market ?? "" },
            {
              key: "days", label: t("colDays"), render: r => (
                <LinkButton tone="inherit" size="inherit" weight="regular" onClick={() => setExpandedRouteId(id => id === r.route_id ? null : r.route_id)}
                  style={{ color: K.accent }} title={t("dayBreakdownTitle")} aria-expanded={expandedRouteId === r.route_id}>
                  {r.first_day}–{r.last_day} {expandedRouteId === r.route_id ? "▲" : "▼"}
                </LinkButton>
              ),
              sortValue: r => r.first_day,
            },
            {
              key: "segments", label: t("colSegments"), render: r => {
                const n = (r.ordered_segment_ids || []).length;
                // AA-554 G.22 — link into Segment, filtered to this Route's own ordered_segment_ids
                // (via the same focusRouteId cross-filter SegmentSection's route_id column already
                // narrows by — reused, not a new filter mechanism).
                return n > 0 ? (
                  <LinkButton tone="inherit" size="inherit" weight="regular" onClick={() => onNavigateToSegment(r.route_id)}
                    style={{ color: K.accent }} title={t("memberSegmentsTitle")}>
                    {t("segmentsN", n)}
                  </LinkButton>
                ) : 0;
              },
              sortValue: r => (r.ordered_segment_ids || []).length,
            },
            { key: "score", label: t("colScore"), render: r => fmtScore(r.score), sortValue: r => r.score },
            { key: "created", label: t("colCreated"), render: r => formatDateTime(r.created_at), sortValue: r => r.created_at },
          ] as Col<RouteRow>[]} />
          {expandedRouteId && <RouteDayBreakdown routeId={expandedRouteId} />}
          {!hasFocus && <PageFooter total={data.total} offset={offset} pageSize={PAGE_SIZE} onOffset={setOffset} />}
        </>
      )}

      <div style={{ fontSize: 13, fontWeight: 600, color: K.body, marginTop: 28, marginBottom: 6 }}>{t("hub")}</div>
      <div style={{ fontSize: 11.5, color: K.muted2, marginBottom: 10 }}>
        {t("hubIntro")}
      </div>
      {hubFetch.error ? <ErrorState message={hubFetch.error} onRetry={hubFetch.reload} /> :
        hubFetch.loading ? <LoadingScreen message={t("loadingHubs")} /> :
        (!hubFetch.data || hubFetch.data.total === 0) ? (
          <EmptyState title={t("noHub")} body={t("noHubBody")} />
        ) : (
        <>
          <AuditTable rows={hubFetch.data.data} rowKey={r => r.hub_id} columns={[
            { key: "hub", label: t("colHubName"), render: r => r.hub_name },
            { key: "tours", label: t("colTours"), render: r => (r.tour_names && r.tour_names.length > 0) ? r.tour_names.join(", ") : "—" },
            { key: "routes", label: t("colRoutes"), render: r => r.route_count },
            { key: "created", label: t("colCreated"), render: r => formatDateTime(r.created_at) },
            { key: "updated", label: t("colUpdated"), render: r => formatDateTime(r.updated_at) },
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
  const t = useT(AT);
  const q = useQuery({
    queryKey: ["atom-curation", "route-days", routeId],
    queryFn: () => fetchJson<{ days: RouteDayRow[]; found: boolean }>(`/api/admin/dashboard/routes/${encodeURIComponent(routeId)}/days`),
  });
  const data = q.data ?? null;
  const loading = q.isPending;
  const error = q.isError ? String((q.error as Error).message || q.error) : null;

  return (
    <Section style={{ marginTop: -1, borderTop: "none" }} bodyStyle={{ borderTopLeftRadius: 0, borderTopRightRadius: 0, padding: "14px 18px" }}>
      {loading ? <div style={{ fontSize: 12, color: K.muted2 }}>{t("loadingDays")}</div> :
        error ? <div style={{ fontSize: 12, color: K.danger }}>{t("daysError", error)}</div> :
        (!data || !data.found || data.days.length === 0) ? (
          <div style={{ fontSize: 12, color: K.muted2 }}>{t("noDays")}</div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {data.days.map((d, i) => (
              <div key={i} style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
                <div style={{
                  flexShrink: 0, width: 62, fontSize: 11, fontFamily: mono, fontWeight: 600,
                  color: K.accent, background: K.accentTint, borderRadius: 6, padding: "4px 8px", textAlign: "center",
                }}>
                  {t("dayN", d.day != null ? String(d.day) : "—")}
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {d.segments.map(s => (
                    <span key={s.segment_id} style={{
                      fontSize: 12, color: K.body, background: K.card, border: `1px solid ${K.line}`,
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
    </Section>
  );
}

function PageFooter({ total, offset, pageSize, onOffset }: {
  total: number; offset: number; pageSize: number; onOffset: (o: number) => void;
}) {
  const t = useT(AT);
  if (total <= pageSize) return null;
  const page = Math.floor(offset / pageSize) + 1;
  const pages = Math.ceil(total / pageSize);
  return (
    <div style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: 12, marginTop: 16 }}>
      <Button variant="secondary" size="sm" disabled={offset === 0} onClick={() => onOffset(Math.max(0, offset - pageSize))}>{t("previous")}</Button>
      <span style={{ fontSize: 12, color: K.muted, fontFamily: mono }}>{t("pageOf", page, pages, total)}</span>
      <Button variant="secondary" size="sm" disabled={offset + pageSize >= total} onClick={() => onOffset(offset + pageSize)}>{t("next")}</Button>
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════
// Section 05 — Slate: UNCHANGED, still per-Tour (AA-550 K.3 — real per-tenant exception)
// ══════════════════════════════════════════════════════════════════════════

interface SlateRow {
  subject_id: string; tenant_name: string | null; channel: string; state: string; score: number | null;
  segment_id: string | null; route_id: string | null; created_at: string;
  // AA-564 2.1 — real topic name, joined server-side the same way the Tenant Portal already does
  // (services/acp_shared/slate.py::fetch_slate()) instead of a raw segment_id/route_id.
  canonical_place: string | null; canonical_action: string | null; hub_name: string | null;
  tour_name: string | null; segment_count: number | null;
  // AA-863 — the Slate seed: the frozen request atom once picked, else the seed a pick would use now.
  seed?: SeedInfo | null;
}

const SLATE_STATE_COLOR: Record<string, Tone> = {
  proposed: "neutral", picked: "info", used: "success", cut: "danger",
};

// AA-554 H.24 — explains all 4 states via a tooltip on each stat-bar badge. `used` text reflects
// this build's own H.1 wiring (no longer "not yet active" — it's real now); `cut` still says so,
// since H.2 only prepared the backend, no tenant UI button exists yet (see the AA-554 child issue).
// Message keys (messages.ts).
const SLATE_STATE_TOOLTIP: Record<string, "tipProposed" | "tipPicked" | "tipUsed" | "tipCut"> = {
  proposed: "tipProposed",
  picked: "tipPicked",
  used: "tipUsed",
  cut: "tipCut",
};

const SLATE_STATE_ORDER = ["proposed", "picked", "used", "cut"] as const;

// AA-561 1e — Nghiệp confirmed the AA-557 G.14 one-liner + column-header tooltips (below) were
// still too technical for a non-code reader ("Kind: Segment" with no explanation). Replaced with
// a plain-language intro block (what Slate IS, in one paragraph) + a full legend for the 4 states
// and 2 Kind values, always visible above the table — not a hover-only tooltip, so it reads
// without the reader needing to know to hover. Keeps the AA-557 G.14 tenant-portal pointer too.
function SlateExplainerNote() {
  const t = useT(AT);
  return (
    <Section style={{ marginBottom: 14, background: K.card }} bodyStyle={{ padding: "14px 18px" }}>
      <div style={{ fontSize: 13, color: K.body, lineHeight: 1.55, marginBottom: 10 }}>
        <strong>{t("whatIsSlate")}</strong> {t("slateIntro")}{" "}
        (<code style={{ fontFamily: mono }}>/portal/t7-planning</code>).
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "6px 18px", fontSize: 11.5, color: K.muted }}>
        <span><strong>proposed</strong> {t("legendProposed")}</span>
        <span><strong>picked</strong> {t("legendPicked")}</span>
        <span><strong>used</strong> {t("legendUsed")}</span>
        <span><strong>cut</strong> {t("legendCut")}</span>
        <span style={{ borderLeft: `1px solid ${K.line}`, paddingLeft: 18 }}>
          <strong>{t("kindSegment")}</strong> {t("legendKindSegment")}
        </span>
        <span><strong>{t("kindRoute")}</strong> {t("legendKindRoute")}</span>
        <span style={{ borderLeft: `1px solid ${K.line}`, paddingLeft: 18 }}>
          <strong>{t("seed")}</strong> {t("legendSeed")}
        </span>
      </div>
    </Section>
  );
}

// AA-564 2.1 — same title logic as the Tenant Portal's SlateTab.tsx:295-297, so Admin and Tenant
// read identically instead of Admin showing a generic "Segment"/"Route" label.
function slateTopicTitle(r: SlateRow, untitled: { journey: string; moment: string }): string {
  if (r.route_id) return r.hub_name ?? untitled.journey;
  return [r.canonical_place, r.canonical_action].filter(Boolean).join(" — ") || untitled.moment;
}

const CHANNELS = ["blog", "linkedin", "facebook", "instagram", "tiktok", "email", "landing_page", "ads"];

function SlateSection() {
  const t = useT(AT);
  const { formatDateTime } = useFormat();
  const untitled = { journey: t("untitledJourney"), moment: t("untitledMoment") };
  // AA-564 2.2 — Slate is genuinely per-tenant (ADR-0003); this section now picks a Tenant, not a
  // Tour, and no longer depends on the page's shared Tour/Market filter at all.
  const [selectedTenant, setSelectedTenant] = useState<string | null>(null);
  const [selectedChannel, setSelectedChannel] = useState("");

  const { data, loading, error, reload } = usePlatformFetch<{ data: SlateRow[]; total: number; by_state: Record<string, number> }>(
    "/api/admin/dashboard/slate", { tenant_id: selectedTenant ?? undefined, channel: selectedChannel || undefined }, !!selectedTenant,
  );

  const tenantPicker = (
    <div style={{ display: "flex", gap: 10, marginBottom: 14, alignItems: "center", flexWrap: "wrap" }}>
      {/* AA-919 S254: the shared admin tenant picker (active tenants, by name). */}
      <TenantSelect inline value={selectedTenant ?? ""} onChange={id => setSelectedTenant(id || null)}
        placeholder={t("chooseTenant")} />
      <span style={{ fontSize: 12, color: K.muted }}>{t("channel")}</span>
      <Select value={selectedChannel} onChange={e => setSelectedChannel(e.target.value)} style={selectStyle}>
        <option value="">{t("allChannels")}</option>
        {CHANNELS.map(c => <option key={c} value={c}>{c}</option>)}
      </Select>
    </div>
  );

  if (!selectedTenant) {
    return <><SlateExplainerNote />{tenantPicker}<EmptyState title={t("selectTenant")} body={t("selectTenantBody")} /></>;
  }
  if (error) return <><SlateExplainerNote />{tenantPicker}<ErrorState message={error} onRetry={reload} /></>;
  if (loading) return <><SlateExplainerNote />{tenantPicker}<LoadingScreen message={t("loadingSlate")} /></>;
  if (!data || data.total === 0) {
    return <><SlateExplainerNote />{tenantPicker}<EmptyState title={t("noSlate")} body={t("noSlateBody")} /></>;
  }
  return (
    <>
      <SlateExplainerNote />
      {tenantPicker}
      {/* AA-554 H.23 — sticky header stat bar, same mechanism Segment/Score's filter rows use.
          H.3 — CUT badge is NOT hidden (shows the real count). AA-556 removed the "coming soon"
          note that used to sit under it, now that the tenant-facing Cut button is real. */}
      <div style={{ display: "flex", gap: 14, marginBottom: 14, flexWrap: "wrap", alignItems: "flex-start",
        position: "sticky", top: 0, background: K.bg, zIndex: 5, paddingTop: 4, paddingBottom: 10 }}>
        {SLATE_STATE_ORDER.map(state => (
          <div key={state}>
            <span title={t(SLATE_STATE_TOOLTIP[state])} style={{ cursor: "help" }}>
              <Badge tone={SLATE_STATE_COLOR[state] ?? "neutral"}>{state}: {data.by_state[state] ?? 0}</Badge>
            </span>
          </div>
        ))}
      </div>
      {/* AA-554 H.25 — Score here is copied as-is from Segment's total_rank / Route's score at
          proposal time, never recomputed by Slate itself. */}
      <div style={{ fontSize: 12, color: K.muted, marginBottom: 10 }}>
        {t("scoreCopied")}
      </div>
      {/* AA-557 G.13 — real table: sortable + per-column filter. */}
      <AuditTable sortable rows={data.data} rowKey={r => r.subject_id} columns={[
        { key: "channel", label: t("colChannel"), render: r => r.channel,
          sortValue: r => r.channel, filterValue: r => r.channel },
        {
          key: "state", label: t("colState"), render: r => (
            <span title={SLATE_STATE_TOOLTIP[r.state] ? t(SLATE_STATE_TOOLTIP[r.state]) : undefined} style={{ cursor: "help" }}>
              <Badge tone={SLATE_STATE_COLOR[r.state] ?? "neutral"}>{r.state}</Badge>
            </span>
          ),
          sortValue: r => r.state, filterValue: r => r.state,
        },
        { key: "score", label: t("colScore"), render: r => fmtScore(r.score), sortValue: r => r.score },
        {
          key: "kind", label: t("colKind"), render: r => (
            <span title={r.route_id ? t("kindRouteTitle") : t("kindSegmentTitle")}
              style={{ cursor: "help" }}>
              {r.route_id ? t("kindRouteWord") : t("kindSegmentWord")}
            </span>
          ),
          sortValue: r => r.route_id ? "Route" : "Segment",
          filterValue: r => r.route_id ? t("kindRouteWord") : t("kindSegmentWord"),
        },
        {
          // AA-564 2.1 — the whole point of this fix: a real topic name + its originating Tour
          // (and segment count for Route-based ideas), instead of a generic "Segment"/"Route" label.
          key: "topic", label: t("colTopic"), render: r => (
            <div>
              <div style={{ fontWeight: 600, color: K.ink }}>{slateTopicTitle(r, untitled)}</div>
              <div style={{ fontSize: 11, color: K.muted2, marginTop: 2 }}>
                {t("topicTour", r.tour_name || "—", r.segment_count != null ? t("topicSegments", r.segment_count) : "")}
              </div>
            </div>
          ),
          sortValue: r => slateTopicTitle(r, untitled),
          filterValue: r => `${slateTopicTitle(r, untitled)} ${r.tour_name ?? ""}`,
        },
        {
          // AA-863 — the seed atom's place · country next to the Subject, with a warning badge when
          // the seed is not the Subject's place (seed rule) or not in the Segment's country.
          key: "seed", label: t("colSeed"), render: r => (
            <div style={{ maxWidth: 280, minWidth: 0 }}>
              {r.seed ? <SeedLine seed={r.seed} /> : <span style={{ color: K.muted2 }}>—</span>}
            </div>
          ),
          sortValue: r => (r.seed?.mismatch ? "0" : "1") + (r.seed?.place ?? ""),
          filterValue: r => `${r.seed?.place ?? ""} ${r.seed?.country ?? ""} ${r.seed?.mismatch ? "mismatch" : ""}`,
        },
        { key: "created", label: t("colProposed"), render: r => formatDateTime(r.created_at),
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
  const t = useT(AT);
  // < 768px the six stat tiles wrap two a row (was the `.aa-kpi-grid` media query, AA-1006).
  const phone = useIsPhone();
  const searchParams = useSearchParams();
  // S241 UI verify (AA-863) / S252 — the header band and body are the kit PageFrameHeader / PageFrameBody inside a
  // split PageFrame: 36px side inset on desktop, 16px on a phone (globals.css), never stacked.
  const sectionParam = searchParams.get("section");
  const initialSection: SectionKey =
    sectionParam && VALID_SECTIONS.has(sectionParam) ? (sectionParam as SectionKey) : "atomize";


  const [selectedTour, setSelectedTour] = useState<string | null>(null);
  const [selectedMarket, setSelectedMarket] = useState("");
  const [activeSection, setActiveSection] = useState<SectionKey>(initialSection);

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

  const summaryQ = useQuery({
    queryKey: ["atom-curation", "atoms-summary"],
    queryFn: () => fetchJson<Summary>("/api/admin/atoms/summary"),
  });
  const summary = summaryQ.data ?? null;
  const summaryLoading = summaryQ.isPending;
  const refetchSummary = summaryQ.refetch;
  const loadSummary = useCallback(() => { refetchSummary(); }, [refetchSummary]);

  // AA-551 — header stat bar, re-fetched whenever the common Tour/Market filter changes,
  // independent of which section tab is open (GET /admin/dashboard/summary).
  const statsQ = useQuery({
    queryKey: ["atom-curation", "dashboard-summary", selectedTour, selectedMarket],
    queryFn: () => {
      const qs = new URLSearchParams();
      if (selectedTour) qs.set("tour_id", selectedTour);
      if (selectedMarket) qs.set("market", selectedMarket);
      return fetchJson<DashboardSummary>(`/api/admin/dashboard/summary?${qs}`);
    },
  });
  // Keeps the last numbers on screen while a new filter loads (the old page did the same).
  const stats = statsQ.data ?? null;
  const statsLoading = statsQ.isFetching;

  const selectedTourMeta = summary?.by_tour.find(x => x.tour_id === selectedTour) ?? null;

  return (
      <PageFrame variant="split">
        <PageFrameHeader>
          <PageHeader
            title={t("title")}
            description={
              <>
                {t("descLead")}{" "}
                <a href="/admin/tenant-activity" style={{ color: K.accent }}>{t("contentTrace")}</a>.
              </>
            }
          />
          <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "flex-end", flexWrap: "wrap", gap: 12, marginBottom: 16 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
              <span style={{ fontSize: 12, color: K.muted }}>{t("tourLabel")}</span>
              <Select
                value={selectedTour ?? ""}
                onChange={e => setSelectedTour(e.target.value || null)}
                // AA-601 (feedback 2) — a <select> sizes to its LONGEST option (full tour names,
                // ~1000px), which overflowed the page at 390px. minWidth: 0 is not enough: a grid/flex
                // ancestor still sizes to the select's min-content (= that longest option). A % width
                // makes a form control's min-content contribution 0, so it shrinks to its container.
                style={{ ...selectStyle, flex: "1 1 220px", width: "100%", minWidth: 0, maxWidth: "100%", fontWeight: 600 }}
              >
                {/* AA-554 K.3 — copy reflects the real behavior of this filter (narrows the view;
                    it's never required to see data, unlike Slate below) instead of implying a
                    Tour must be picked. */}
                <option value="">{t("allToursNarrow")}</option>
                {(summary?.by_tour ?? []).map(tr => (
                  <option key={tr.tour_id} value={tr.tour_id}>{tr.tour_name}</option>
                ))}
              </Select>
              <span style={{ fontSize: 12, color: K.muted }}>{t("marketLabel")}</span>
              <Select value={selectedMarket} onChange={e => setSelectedMarket(e.target.value)} style={{ ...selectStyle, flex: "0 1 130px", width: "100%", minWidth: 0, maxWidth: "100%" }}>
                <option value="">{t("allMarkets")}</option>
                {MARKETS.map(m => <option key={m} value={m}>{m}</option>)}
              </Select>
              {selectedTourMeta && selectedTourMeta.lifecycle_stage !== "active" && (
                <Badge tone={LIFECYCLE_COLOR[selectedTourMeta.lifecycle_stage]}>{selectedTourMeta.lifecycle_stage}</Badge>
              )}
            </div>
          </div>

          {/* Header stat bar — AA-551, AA-550 mục F point 3/4: auto-updates with the filter above. */}
          <div style={{ display: "grid", gridTemplateColumns: phone ? "repeat(2, minmax(0, 1fr))" : "repeat(6, minmax(0, 1fr))", gap: 10 }}>
            {([
              [t("statTours"), stats?.tour_count],
              [t("statAtoms"), stats?.atom_count],
              [t("statSegments"), stats?.segment_count],
              [t("statScoreRows"), stats?.score_count],
              [t("statRoutes"), stats?.route_count],
              [t("statHubs"), stats?.hub_count],
            ] as [string, number | undefined][]).map(([label, value]) => (
              <div key={label} style={{
                background: K.card, border: `1px solid ${K.line}`, borderRadius: 8, padding: "8px 12px",
              }}>
                <div style={{ fontSize: 10.5, color: K.muted, textTransform: "uppercase", letterSpacing: "0.04em" }}>{label}</div>
                <div style={{ fontFamily: mono, fontSize: 17, fontWeight: 600, color: K.ink }}>
                  {statsLoading ? "…" : (value ?? "—")}
                </div>
              </div>
            ))}
          </div>
        </PageFrameHeader>

        {/* AA-564 1.1 — `minHeight: 0` is required here: a flex column's default `min-height:auto`
            makes this item grow to its content's min-content size (which exceeds the viewport once
            the atom list/tables get tall) instead of shrinking to its flex-basis and scrolling
            internally, which pushed the overflow up to the document — dragging the header (which
            relies on being outside this scroll region, not on `position:sticky`) off-screen on a
            real full-page scroll. This is the actual root cause AA-554/AA-557 both missed. */}
        <PageFrameBody scroll>
          <SocialContentSplit>
            {/* AA-575 — inner-sidebar extracted into a shared component (was JSX embedded only
                in this page, which is exactly why Content Trace (06) had no sub-nav at all: it's
                a genuinely separate route/page with its own render tree, never this page's inline
                markup). `onSelectSection` keeps 01-05 as instant same-page tab switches here — see
                SocialContentSubNav.tsx for why Content Trace renders the same list differently
                (real links, no local tab state to hook into). */}
            <SocialContentSubNav active={activeSection} onSelectSection={gotoSection} />

            {/* S241 UI verify (AA-863) — width/maxWidth 100%: in the phone column layout
                (.a527-dash-body, < 980px) this column would otherwise shrink-wrap to its content's
                min-content, which a table's overflow-x wrapper does NOT cap (the Slate table's
                columns incl. the nowrap Seed line), so it grew past the scroller and the whole body
                scrolled sideways. Same fix as content-quality/page.tsx. */}
            <div style={{ flex: 1, minWidth: 0, width: "100%", maxWidth: "100%" }}>
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
          </SocialContentSplit>
        </PageFrameBody>
      </PageFrame>
  );
}
