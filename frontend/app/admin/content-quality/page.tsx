"use client";
// app/admin/content-quality/page.tsx — AA-769 "08 · Content Quality".
//
// Since AA-770 the tenant always receives T2/T9 content and sees no gate, score, held state or
// flag. This page is where AA admins see all of it, for every tenant, to improve the system:
//   Pieces        — every T9 piece: final status (held = admin label), failed blocking gates and
//                   warn flags read from gate_ledger (never held_reason, which names only the first
//                   gate), attempts, LLM cost; drawer = highlighted text + writer inputs.
//   Tour versions — every T2 tenant tour version with its score / QA state / escalations;
//                   AA-802: the drawer corrects a sentence in the tenant's unpublished copy.
//   Gate stats    — per gate and channel: runs, fails, blocking fails; per tenant outcomes.
//   Place cards   — AA-772 cards: verify / reject, build the missing cards for one tour (job),
//                   S234: review duplicate candidates and merge them into one card.
//                   AA-774 (S235): "Facts" opens a drawer to re-kind / edit / drop a card's facts.
// AA-791: Pieces has a human-label column + filter (latest label per piece) once migration 231 is
//   applied; the drawer's "Label" section saves one. Before 231 both are hidden.
// UI kit + react-query; view state in the URL (tab, filters, page) like the Jobs page.
// API: /admin/content-quality/* via the BFF.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState, useSyncExternalStore } from "react";
import type { SortingState } from "@tanstack/react-table";
import { SeedLine } from "../_components/SeedLine";
import SocialContentSubNav from "../_components/SocialContentSubNav";
import { A, mono, Btn } from "../_components/adminUi";
import {
  Badge, ConfirmModal, DataTable, PageHeader, StatusBadge, Tabs, ToastProvider, apiGet, apiSend, useToast,
  type ColumnDef,
} from "../../_kit";
import PieceDrawer from "./PieceDrawer";
import { LabelBadge } from "./PieceLabel";
import PlaceCardFactsDrawer from "./PlaceCardFactsDrawer";
import TourVersionDrawer from "./TourVersionDrawer";
import {
  DUPLICATE_REASON_LABELS, GATE_FILTER, LABEL_NAMES, fmtTime, gateLabel, usd,
  type DuplicateCluster, type GateStat, type Paged, type PieceLabelValue, type PieceRow, type PiecesPage, type PlaceCard, type TenantStat,
  type VersionRow,
} from "./shared";

type TabKey = "pieces" | "versions" | "stats" | "cards";
const TABS: { key: TabKey; label: string }[] = [
  { key: "pieces", label: "Pieces (T9)" },
  { key: "versions", label: "Tour versions (T2)" },
  { key: "stats", label: "Gate stats" },
  { key: "cards", label: "Place cards" },
];
const PAGE_SIZE = 50;

interface View {
  tab: TabKey; tenant: string; channel: string; status: string; gate: string; q: string; label: string;
  sort: string; dir: "asc" | "desc"; page: number; piece: string | null; version: string | null;
}
const EMPTY: View = { tab: "pieces", tenant: "", channel: "", status: "", gate: "", q: "", label: "", sort: "created_at",
                      dir: "desc", page: 0, piece: null, version: null };

function readView(): View {
  if (typeof window === "undefined") return { ...EMPTY };
  const p = new URLSearchParams(window.location.search);
  const t = p.get("tab");
  return {
    tab: t === "versions" || t === "stats" || t === "cards" ? t : "pieces",
    tenant: p.get("tenant") ?? "", channel: p.get("channel") ?? "", status: p.get("status") ?? "",
    gate: p.get("gate") ?? "", q: p.get("q") ?? "", label: p.get("label") ?? "", sort: p.get("sort") ?? "created_at",
    dir: p.get("dir") === "asc" ? "asc" : "desc", page: Math.max(0, (Number(p.get("page")) || 1) - 1),
    piece: p.get("piece"), version: p.get("version"),
  };
}

const noopSubscribe = () => () => {};

function writeView(v: View) {
  if (typeof window === "undefined") return;
  const p = new URLSearchParams();
  (["tenant", "channel", "status", "gate", "q", "label"] as const).forEach((k) => { if (v[k]) p.set(k, v[k]); });
  if (v.tab !== "pieces") p.set("tab", v.tab);
  if (v.sort !== "created_at") p.set("sort", v.sort);
  if (v.dir !== "desc") p.set("dir", v.dir);
  if (v.page) p.set("page", String(v.page + 1));
  if (v.piece) p.set("piece", v.piece);
  if (v.version) p.set("version", v.version);
  const qs = p.toString();
  window.history.replaceState(null, "", qs ? `?${qs}` : window.location.pathname);
}

const inputStyle: React.CSSProperties = {
  fontSize: 12, padding: "6px 8px", border: `1px solid ${A.line}`, borderRadius: 6, background: A.card, color: A.ink3,
};

function GateChips({ gates, tone }: { gates: string[]; tone: "danger" | "warning" }) {
  if (!gates.length) return <span style={{ color: A.muted }}>—</span>;
  return (
    <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
      {gates.map((g) => <span key={g} title={g}><Badge tone={tone} dot={false}>{gateLabel(g)}</Badge></span>)}
    </span>
  );
}

function statusBadge(s: string) {
  const map: Record<string, string> = { approved: "succeeded", held: "failed", failed: "failed", processing: "running" };
  return <StatusBadge status={map[s] ?? s} label={s} />;
}

// ── Pieces ──────────────────────────────────────────────────────────────────────────────────────
function PiecesTab({ view, update, tenants }: { view: View; update: (p: Partial<View>) => void; tenants: TenantStat[] }) {
  const qs = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(view.page * PAGE_SIZE),
                                   sort: view.sort, sort_dir: view.dir });
  if (view.tenant) qs.set("tenant_id", view.tenant);
  if (view.channel) qs.set("channel", view.channel);
  if (view.status) qs.set("status", view.status);
  if (view.gate) qs.set("gate", view.gate);
  if (view.q) qs.set("q", view.q);
  if (view.label) qs.set("label", view.label);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["cq-pieces", qs.toString()],
    queryFn: () => apiGet<PiecesPage>(`/api/admin/content-quality/pieces?${qs}`, { admin: true }),
  });
  const labelsEnabled = !!data?.labels_enabled;
  const columns = useMemo<ColumnDef<PieceRow, unknown>[]>(() => [
    { id: "created_at", accessorKey: "created_at", header: "Written",
      cell: (c) => <span style={{ whiteSpace: "nowrap", color: A.muted }}>{fmtTime(c.row.original.created_at)}</span> },
    { id: "tenant", accessorKey: "tenant_name", header: "Tenant" },
    { id: "channel", accessorKey: "channel", header: "Channel" },
    // AA-863 — under the title: the Segment the piece was written for and its seed place · country,
    // with a warning badge when the seed does not fit the Subject (SeedLine).
    { id: "title", accessorKey: "title", header: "Title / angle · seed", enableSorting: false,
      cell: (c) => (
        <span style={{ display: "block", maxWidth: 320, minWidth: 0 }}>
          <span style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={c.row.original.title ?? ""}>{c.row.original.title ?? "—"}</span>
          <SeedLine seed={c.row.original.seed} showSegment />
        </span>
      ) },
    { id: "status", accessorKey: "status", header: "Status", cell: (c) => statusBadge(c.row.original.status) },
    // AA-791 — the latest human label (and how many people labelled), once migration 231 is applied.
    // Sits right after Status (S239) so it is on screen at 1440 px; the gate-chip columns after it
    // can be wide and used to push it into the horizontal scroll.
    ...(labelsEnabled ? [{ id: "label", header: "Label", enableSorting: false,
      cell: (c) => {
        const r = c.row.original;
        if (!r.label) return <span style={{ color: A.muted }}>—</span>;
        return (
          <span style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
            <LabelBadge label={r.label} />
            {(r.label_count ?? 0) > 1 && <span style={{ fontSize: 11, color: A.muted }}>×{r.label_count}</span>}
          </span>
        );
      } } as ColumnDef<PieceRow, unknown>] : []),
    { id: "failed_gates", header: "Blocking fails", enableSorting: false,
      cell: (c) => <GateChips gates={c.row.original.failed_gates} tone="danger" /> },
    { id: "flag_gates", header: "Warnings", enableSorting: false,
      cell: (c) => <GateChips gates={c.row.original.flag_gates} tone="warning" /> },
    { id: "attempt", accessorKey: "attempt_number", header: "Att.",
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.attempt_number}</span> },
    { id: "cost_usd", accessorKey: "cost_usd", header: "Cost",
      cell: (c) => <span style={{ fontFamily: mono }}>{usd(c.row.original.cost_usd)}</span> },
  ], [labelsEnabled]);
  const sorting: SortingState = [{ id: view.sort, desc: view.dir === "desc" }];
  return (
    <DataTable<PieceRow>
      data={data?.data ?? []} columns={columns} tableId="admin-content-quality-pieces"
      getRowId={(r) => r.piece_id} loading={isLoading}
      error={error ? (error instanceof Error ? error.message : String(error)) : null} onRetry={() => refetch()}
      serverPagination={{ pageIndex: view.page, pageSize: PAGE_SIZE, total: data?.total ?? 0,
                          onChange: (pageIndex) => update({ page: pageIndex }) }}
      pageSize={PAGE_SIZE} manualSorting sorting={sorting}
      onSortingChange={(next) => { const s = next[0]; update(s ? { sort: s.id, dir: s.desc ? "desc" : "asc", page: 0 } : { sort: "created_at", dir: "desc", page: 0 }); }}
      enableCsv csvFilename="content-quality-pieces" stickyHeader maxBodyHeight="calc(100vh - 380px)" tableMinWidth={1100}
      emptyTitle="No pieces match these filters" emptyDescription="T9 pieces from every tenant show here."
      onRowClick={(r) => update({ piece: r.piece_id })}
      toolbarExtra={
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <input value={view.q} onChange={(e) => update({ q: e.target.value, page: 0 })} placeholder="Search title / text…"
                 aria-label="Search pieces" style={{ ...inputStyle, minWidth: 180 }} />
          <select value={view.tenant} onChange={(e) => update({ tenant: e.target.value, page: 0 })} aria-label="Tenant" style={inputStyle}>
            <option value="">All tenants</option>
            {tenants.map((t) => <option key={t.tenant_id} value={t.tenant_id}>{t.tenant_name}</option>)}
          </select>
          <select value={view.channel} onChange={(e) => update({ channel: e.target.value, page: 0 })} aria-label="Channel" style={inputStyle}>
            <option value="">All channels</option>
            {["blog", "linkedin", "facebook", "instagram", "tiktok", "email", "landing_page", "ads"].map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <select value={view.status} onChange={(e) => update({ status: e.target.value, page: 0 })} aria-label="Status" style={inputStyle}>
            <option value="">All statuses</option>
            {["approved", "held", "failed", "processing"].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <select value={view.gate} onChange={(e) => update({ gate: e.target.value, page: 0 })} aria-label="Gate" style={{ ...inputStyle, maxWidth: 220 }}>
            <option value="">Any gate</option>
            {GATE_FILTER.active.map((o) => <option key={o.value} value={o.value}>{o.text}</option>)}
            <optgroup label="Retired (older pieces only)">
              {GATE_FILTER.retired.map((o) => <option key={o.value} value={o.value}>{o.text}</option>)}
            </optgroup>
          </select>
          {labelsEnabled && (
            <select value={view.label} onChange={(e) => update({ label: e.target.value, page: 0 })} aria-label="Label" style={inputStyle}>
              <option value="">Any label</option>
              <option value="labelled">Labelled</option>
              <option value="unlabelled">Not labelled</option>
              {(Object.keys(LABEL_NAMES) as PieceLabelValue[]).map((l) => <option key={l} value={l}>{LABEL_NAMES[l]}</option>)}
            </select>
          )}
        </div>
      }
    />
  );
}

// ── T2 tour versions ── (drawer: TourVersionDrawer.tsx, AA-802 silent correction)
function VersionsTab({ view, update, tenants }: { view: View; update: (p: Partial<View>) => void; tenants: TenantStat[] }) {
  const qs = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(view.page * PAGE_SIZE) });
  if (view.tenant) qs.set("tenant_id", view.tenant);
  if (view.q) qs.set("q", view.q);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["cq-versions", qs.toString()],
    queryFn: () => apiGet<Paged<VersionRow>>(`/api/admin/content-quality/tour-versions?${qs}`, { admin: true }),
  });
  const columns = useMemo<ColumnDef<VersionRow, unknown>[]>(() => [
    { accessorKey: "created_at", header: "Written", cell: (c) => <span style={{ color: A.muted, whiteSpace: "nowrap" }}>{fmtTime(c.row.original.created_at)}</span> },
    { accessorKey: "tenant_name", header: "Tenant" },
    { accessorKey: "tour_name", header: "Tour" },
    { accessorKey: "version_number", header: "v" },
    { accessorKey: "status", header: "Status" },
    { accessorKey: "quality_score", header: "Score", cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.quality_score ?? "—"}</span> },
    { accessorKey: "qa_status", header: "QA", cell: (c) => <span>{c.row.original.qa_status ?? "—"}{c.row.original.qa_auto_passed ? " · auto" : ""}</span> },
    { accessorKey: "escalations", header: "Escalations" },
    { accessorKey: "cost_usd", header: "Cost", cell: (c) => <span style={{ fontFamily: mono }}>{usd(c.row.original.cost_usd)}</span> },
  ], []);
  return (
    <DataTable<VersionRow>
      data={data?.data ?? []} columns={columns} tableId="admin-content-quality-versions" getRowId={(r) => r.id}
      loading={isLoading} error={error ? String(error) : null} onRetry={() => refetch()}
      serverPagination={{ pageIndex: view.page, pageSize: PAGE_SIZE, total: data?.total ?? 0, onChange: (i) => update({ page: i }) }}
      pageSize={PAGE_SIZE} enableCsv csvFilename="content-quality-t2" stickyHeader tableMinWidth={980}
      emptyTitle="No tour versions yet" onRowClick={(r) => update({ version: r.id })}
      toolbarExtra={
        <div style={{ display: "flex", gap: 8 }}>
          <input value={view.q} onChange={(e) => update({ q: e.target.value, page: 0 })} placeholder="Search tour…" aria-label="Search tours" style={inputStyle} />
          <select value={view.tenant} onChange={(e) => update({ tenant: e.target.value, page: 0 })} aria-label="Tenant" style={inputStyle}>
            <option value="">All tenants</option>
            {tenants.map((t) => <option key={t.tenant_id} value={t.tenant_id}>{t.tenant_name}</option>)}
          </select>
        </div>
      }
    />
  );
}

// ── Gate stats ──────────────────────────────────────────────────────────────────────────────────
function StatsTab({ gates, tenants, loading }: { gates: GateStat[]; tenants: TenantStat[]; loading: boolean }) {
  const gateCols = useMemo<ColumnDef<GateStat, unknown>[]>(() => [
    { accessorKey: "gate", header: "Gate", cell: (c) => <span><b>{gateLabel(c.row.original.gate)}</b> <span style={{ fontFamily: mono, fontSize: 11, color: A.muted }}>{c.row.original.gate}</span></span> },
    { accessorKey: "channel", header: "Channel" },
    { accessorKey: "runs", header: "Runs" },
    { accessorKey: "failed", header: "Failed" },
    { accessorKey: "blocking_failed", header: "Blocking fails" },
    { id: "rate", header: "Fail rate", accessorFn: (r) => (r.runs ? r.failed / r.runs : 0),
      cell: (c) => <span style={{ fontFamily: mono }}>{Math.round((c.getValue() as number) * 100)}%</span> },
  ], []);
  const tenantCols = useMemo<ColumnDef<TenantStat, unknown>[]>(() => [
    { accessorKey: "tenant_name", header: "Tenant" },
    { accessorKey: "pieces", header: "Pieces" },
    { accessorKey: "approved", header: "Approved" },
    { accessorKey: "held", header: "Held" },
    { accessorKey: "failed", header: "Failed" },
    { accessorKey: "cost_usd", header: "LLM cost", cell: (c) => <span style={{ fontFamily: mono }}>{usd(c.row.original.cost_usd)}</span> },
  ], []);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div style={{ fontSize: 12, color: A.muted }}>Last 30 days, final attempt of each piece. Counted from the gate ledger — every gate, not only the one named in held_reason.</div>
      <DataTable<GateStat> data={gates} columns={gateCols} tableId="admin-cq-gate-stats" getRowId={(r) => `${r.gate}-${r.channel}`}
                           loading={loading} enableCsv csvFilename="gate-stats" pageSize={50} emptyTitle="No pieces in this window" />
      <DataTable<TenantStat> data={tenants} columns={tenantCols} tableId="admin-cq-tenant-stats" getRowId={(r) => r.tenant_id}
                             loading={loading} pageSize={25} emptyTitle="No pieces in this window" />
    </div>
  );
}

// ── Place cards ─────────────────────────────────────────────────────────────────────────────────
// S234 (AA-772) — one duplicate cluster: pick the card to keep (default: most atoms), then tick
// only the cards that are the SAME place. Nothing starts ticked: candidates include different places
// ("Punakha" vs "Punakha Dzong", "Paro" vs "Paro Taktsang"), so a merge is always a deliberate choice.
function DuplicateClusterRow({ cluster, onMerge, busy }: {
  cluster: DuplicateCluster; busy: boolean;
  onMerge: (keepId: string, fromIds: string[], names: string) => void;
}) {
  const byAtoms = [...cluster.cards].sort((a, b) => b.atoms - a.atoms || b.facts - a.facts);
  const [keep, setKeep] = useState(byAtoms[0].card_id);
  const [merge, setMerge] = useState<Set<string>>(() => new Set());
  const fromIds = cluster.cards.filter((c) => c.card_id !== keep && merge.has(c.card_id)).map((c) => c.card_id);
  const toggle = (id: string) => setMerge((m) => { const n = new Set(m); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const keepName = cluster.cards.find((c) => c.card_id === keep)?.display_name ?? "";
  return (
    <div style={{ border: `1px solid ${A.line}`, borderRadius: 8, padding: "10px 12px", background: A.card }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 6 }}>
        <b style={{ fontSize: 13 }}>{cluster.country}</b>
        {cluster.reasons.map((r) => <Badge key={r} tone="warning" dot={false}>{DUPLICATE_REASON_LABELS[r] ?? r}</Badge>)}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {cluster.cards.map((c) => (
          <div key={c.card_id} style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", fontSize: 12.5 }}>
            <label style={{ display: "inline-flex", gap: 4, alignItems: "center", minWidth: 64 }}>
              <input type="radio" name={`keep-${cluster.cards[0].card_id}`} checked={keep === c.card_id} onChange={() => setKeep(c.card_id)} aria-label={`Keep ${c.display_name}`} />
              keep
            </label>
            <label style={{ display: "inline-flex", gap: 4, alignItems: "center", minWidth: 70, color: keep === c.card_id ? A.muted2 : A.ink3 }}>
              <input type="checkbox" disabled={keep === c.card_id} checked={keep !== c.card_id && merge.has(c.card_id)} onChange={() => toggle(c.card_id)} aria-label={`Merge ${c.display_name}`} />
              merge
            </label>
            <span style={{ minWidth: 0 }}>
              <b>{c.display_name}</b>
              <span style={{ color: A.muted }}> — {[c.locality, c.region].filter(Boolean).join(", ") || "no locality"} · {c.atoms} atoms · {c.facts} facts · {c.status}</span>
            </span>
          </div>
        ))}
      </div>
      <div style={{ marginTop: 8 }}>
        <Btn size="sm" disabled={busy || fromIds.length === 0} onClick={() => onMerge(keep, fromIds, keepName)}>
          Merge {fromIds.length || ""} into “{keepName}”
        </Btn>
      </div>
    </div>
  );
}

function DuplicatesPanel() {
  const toast = useToast();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState<{ keepId: string; fromIds: string[]; name: string } | null>(null);
  const { data, isLoading, error } = useQuery({
    queryKey: ["cq-card-duplicates"],
    queryFn: () => apiGet<{ data: DuplicateCluster[]; total: number }>(`/api/admin/content-quality/place-cards/duplicates`, { admin: true }),
    enabled: open,
  });
  const merge = useMutation({
    mutationFn: (p: { keepId: string; fromIds: string[] }) =>
      apiSend<{ atoms_moved: number }>(`/api/admin/content-quality/place-cards/${p.keepId}/merge`, { body: { from_ids: p.fromIds }, admin: true }),
    onSuccess: (r) => {
      toast.success(`Merged — ${r.atoms_moved} atom link(s) moved`);
      setPending(null);
      qc.invalidateQueries({ queryKey: ["cq-card-duplicates"] });
      qc.invalidateQueries({ queryKey: ["cq-cards"] });
    },
    onError: (e) => { toast.error(`Could not merge: ${String(e)}`); setPending(null); },
  });
  const clusters = data?.data ?? [];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Btn size="sm" variant="secondary" onClick={() => setOpen((o) => !o)}>
          {open ? "Hide duplicates" : "Find duplicate cards"}
        </Btn>
        {open && !isLoading && !error && <span style={{ fontSize: 12, color: A.muted }}>{clusters.length} candidate group(s) — never merged automatically</span>}
      </div>
      {open && isLoading && <span style={{ fontSize: 12, color: A.muted }}>Loading…</span>}
      {open && error && <span style={{ fontSize: 12, color: A.red }}>Could not load duplicates: {String(error)}</span>}
      {open && clusters.map((c) => (
        <DuplicateClusterRow key={c.cards.map((x) => x.card_id).join(",")} cluster={c} busy={merge.isPending}
          onMerge={(keepId, fromIds, name) => setPending({ keepId, fromIds, name })} />
      ))}
      <ConfirmModal
        open={pending !== null}
        onClose={() => setPending(null)}
        onConfirm={() => pending && merge.mutate({ keepId: pending.keepId, fromIds: pending.fromIds })}
        title="Merge place cards"
        body={`Merge ${pending?.fromIds.length ?? 0} card(s) into “${pending?.name ?? ""}”? Their atoms move to this card, their names become aliases, and the merged cards are deleted.`}
        confirmLabel="Merge"
        destructive
        busy={merge.isPending}
      />
    </div>
  );
}

function PlaceCardsTab({ view, update }: { view: View; update: (p: Partial<View>) => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [tourId, setTourId] = useState("");
  const [force, setForce] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const qs = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(view.page * PAGE_SIZE) });
  if (view.status) qs.set("status", view.status);
  if (view.q) qs.set("q", view.q);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["cq-cards", qs.toString()],
    queryFn: () => apiGet<Paged<PlaceCard>>(`/api/admin/content-quality/place-cards?${qs}`, { admin: true }),
  });
  const verdict = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      apiSend(`/api/admin/content-quality/place-cards/${id}`, { method: "PATCH", body: { status }, admin: true }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["cq-cards"] }); },
    onError: (e) => toast.error(`Could not save: ${String(e)}`),
  });
  const build = useMutation({
    mutationFn: () => apiSend<{ job_id: string }>(`/api/admin/content-quality/place-cards/build`, { body: { tour_id: tourId.trim(), force }, admin: true }),
    onSuccess: (r) => { toast.success(`Place-card job queued (${r.job_id})`); setTourId(""); setForce(false); },
    onError: (e) => toast.error(`Could not queue: ${String(e)}`),
  });
  const columns = useMemo<ColumnDef<PlaceCard, unknown>[]>(() => [
    { accessorKey: "display_name", header: "Place",
      cell: (c) => { const p = c.row.original; return <div><b>{p.display_name}</b><div style={{ fontSize: 11.5, color: A.muted }}>{[p.locality, p.region, p.country].filter(Boolean).join(", ")}</div></div>; } },
    { id: "facts", header: "Checked facts", enableSorting: false,
      cell: (c) => <div style={{ fontSize: 12, maxWidth: 460 }}>{c.row.original.naming_note && <div style={{ color: A.muted, marginBottom: 2 }}>{c.row.original.naming_note}</div>}{c.row.original.facts.map((f, i) => <div key={i}>• {f.text} <span style={{ color: A.muted, fontSize: 11 }}>({f.kind || "what_it_is"})</span></div>)}</div> },
    { accessorKey: "atoms", header: "Atoms" },
    { accessorKey: "status", header: "Status",
      cell: (c) => <Badge tone={c.row.original.status === "verified" ? "success" : c.row.original.status === "rejected" ? "danger" : "neutral"} dot={false}>{c.row.original.status}</Badge> },
    { id: "actions", header: "", enableSorting: false,
      cell: (c) => (
        <div style={{ display: "flex", gap: 6 }} onClick={(e) => e.stopPropagation()}>
          <Btn size="sm" disabled={verdict.isPending || c.row.original.status === "verified"} onClick={() => verdict.mutate({ id: c.row.original.card_id, status: "verified" })}>Verify</Btn>
          <Btn size="sm" variant="danger" disabled={verdict.isPending || c.row.original.status === "rejected"} onClick={() => verdict.mutate({ id: c.row.original.card_id, status: "rejected" })}>Reject</Btn>
          <Btn size="sm" variant="secondary" onClick={() => setEditing(c.row.original.card_id)}>Facts</Btn>
        </div>
      ) },
  ], [verdict]);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, minWidth: 0 }}>
    <DuplicatesPanel />
    <DataTable<PlaceCard>
      data={data?.data ?? []} columns={columns} tableId="admin-content-quality-cards" getRowId={(r) => r.card_id}
      loading={isLoading} error={error ? String(error) : null} onRetry={() => refetch()}
      serverPagination={{ pageIndex: view.page, pageSize: PAGE_SIZE, total: data?.total ?? 0, onChange: (i) => update({ page: i }) }}
      pageSize={PAGE_SIZE} tableMinWidth={900}
      emptyTitle="No place cards yet" emptyDescription="Cards are built when T8 writes angles for an atom, or for a whole tour below."
      toolbarExtra={
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <input value={view.q} onChange={(e) => update({ q: e.target.value, page: 0 })} placeholder="Search place / country…" aria-label="Search places" style={inputStyle} />
          <select value={view.status} onChange={(e) => update({ status: e.target.value, page: 0 })} aria-label="Card status" style={inputStyle}>
            <option value="">All statuses</option>
            {["auto_checked", "verified", "rejected"].map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <input value={tourId} onChange={(e) => setTourId(e.target.value)} placeholder="tour_id (uuid)" aria-label="Tour id" style={{ ...inputStyle, fontFamily: mono, minWidth: 260 }} />
          <Btn size="sm" disabled={!/^[0-9a-f-]{36}$/i.test(tourId.trim()) || build.isPending} onClick={() => build.mutate()}>Build cards for tour</Btn>
          <label style={{ display: "inline-flex", gap: 4, alignItems: "center", fontSize: 12, color: A.ink3 }} title="Retry places whose last draft found no place or was rejected">
            <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} aria-label="Retry cached misses" />
            retry misses
          </label>
        </div>
      }
    />
    {editing && <PlaceCardFactsDrawer cardId={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

function Page() {
  // The server renders EMPTY; the URL is read only once hydrated (server snapshot false). Reading
  // window.location in the first render made deep links (?piece=, ?tab=) a hydration mismatch
  // (React #418) and the root re-render reset the dark theme.
  const hydrated = useSyncExternalStore(noopSubscribe, () => true, () => false);
  const [edited, setEdited] = useState<View | null>(null);
  const view = edited ?? (hydrated ? readView() : EMPTY);
  const update = (patch: Partial<View>) => { const n = { ...view, ...patch }; writeView(n); setEdited(n); };
  const stats = useQuery({
    queryKey: ["cq-stats"],
    queryFn: () => apiGet<{ gates: GateStat[]; tenants: TenantStat[] }>(`/api/admin/content-quality/stats?days=30`, { admin: true }),
  });
  const tenants = stats.data?.tenants ?? [];
  return (
    <div style={{ padding: "20px clamp(16px, 3vw, 24px)", maxWidth: "100%", boxSizing: "border-box" }}>
      <PageHeader title="Content Quality"
        description="Every tenant's written content with the quality signals tenants never see: gate results, flagged sentences, what the writer was given, cost. Tenants always receive their content (AA-770)." />
      <div className="a527-dash-body" style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
        <SocialContentSubNav active="content_quality" />
        {/* width/maxWidth 100%: in the phone column layout (.a527-dash-body) a flex-start child would
            shrink-wrap to its widest row and overflow the page (UI smoke, 390px). */}
        <div data-testid="content-quality-ready" style={{ flex: 1, minWidth: 0, width: "100%", maxWidth: "100%", display: "flex", flexDirection: "column", gap: 12 }}>
          <Tabs tabs={TABS} active={view.tab} onChange={(k) => update({ ...EMPTY, tab: k as TabKey })} />
          {view.tab === "pieces" && <PiecesTab view={view} update={update} tenants={tenants} />}
          {view.tab === "versions" && <VersionsTab view={view} update={update} tenants={tenants} />}
          {view.tab === "stats" && <StatsTab gates={stats.data?.gates ?? []} tenants={tenants} loading={stats.isLoading} />}
          {view.tab === "cards" && <PlaceCardsTab view={view} update={update} />}
        </div>
      </div>
      {view.piece && <PieceDrawer pieceId={view.piece} onClose={() => update({ piece: null })} />}
      {view.version && <TourVersionDrawer id={view.version} onClose={() => update({ version: null })} />}
    </div>
  );
}

export default function ContentQualityPage() {
  return <ToastProvider><Page /></ToastProvider>;
}
