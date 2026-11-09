"use client";
// app/admin/places/cards/page.tsx — AA-1039 (R6 rest, IA §6.2): AA Catalogue › Place cards. Was the Cards tab of
// Content quality (/admin/tenant-qa/quality?tab=cards, a 307 here now); the tech lead chose option A (05/11/2026): its
// own route with its own page rule (lib/rbac.ts, TENANT_QA = admin, content_lead, tenant_qa, placed before the
// admin-only /admin/places rule), so nobody gains or loses access. The API is unchanged:
// /admin/content-quality/place-cards* (BFF write rule content-quality/place-cards, TENANT_QA).
//   AA-772 cards: verify / reject, build the missing cards for one tour (job); S234: review duplicate candidates and
//   merge them into one card; AA-774 (S235): "Facts" opens a drawer to re-kind / edit / drop a card's facts.
// View state (status, search, page) in the URL, read once hydrated (deep links never mismatch the server render).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo, useState, useSyncExternalStore } from "react";
import {
  K, mono,
  Button,
  Badge, ConfirmModal, DataTable, PageHeader, ToastProvider, apiGet, apiSend, useT, useToast,
  type ColumnDef,
  PageFrame,
  Checkbox, Input, Radio, Select,
} from "../../../_kit";
import type { Catalog, Messages } from "../../../_kit/core/i18n";
import PlaceCardFactsDrawer from "./PlaceCardFactsDrawer";
import type { DuplicateCluster, Paged, PlaceCard } from "./shared";
import { CARD_STATUS, DUPLICATE_REASON, PC } from "./messages";

const PAGE_SIZE = 50;

interface View { status: string; q: string; page: number }
const EMPTY: View = { status: "", q: "", page: 0 };

function readView(): View {
  if (typeof window === "undefined") return { ...EMPTY };
  const p = new URLSearchParams(window.location.search);
  return { status: p.get("status") ?? "", q: p.get("q") ?? "", page: Math.max(0, (Number(p.get("page")) || 1) - 1) };
}

function writeView(v: View) {
  if (typeof window === "undefined") return;
  const p = new URLSearchParams();
  if (v.status) p.set("status", v.status);
  if (v.q) p.set("q", v.q);
  if (v.page) p.set("page", String(v.page + 1));
  const qs = p.toString();
  window.history.replaceState(null, "", qs ? `?${qs}` : window.location.pathname);
}

const noopSubscribe = () => () => {};

const inputStyle: React.CSSProperties = {
  fontSize: 12, padding: "6px 8px", border: `1px solid ${K.line}`, borderRadius: 6, background: K.card, color: K.ink3,
};

const has = <T extends object>(o: T, k: string): k is Extract<keyof T, string> => Object.prototype.hasOwnProperty.call(o, k);

/** The UI-language text of a code the API sends (a status, a duplicate reason); an unknown code as it is. */
function useLookup<E extends Catalog>(messages: Messages<E>): (code: string | null | undefined) => string {
  const t = useT(messages) as unknown as (key: string) => string;
  return useCallback((code) => (code && has(messages.en, code) ? t(code) : code ?? ""), [t, messages]);
}

// S234 (AA-772) — one duplicate cluster: pick the card to keep (default: most atoms), then tick
// only the cards that are the SAME place. Nothing starts ticked: candidates include different places
// ("Punakha" vs "Punakha Dzong", "Paro" vs "Paro Taktsang"), so a merge is always a deliberate choice.
function DuplicateClusterRow({ cluster, onMerge, busy }: {
  cluster: DuplicateCluster; busy: boolean;
  onMerge: (keepId: string, fromIds: string[], names: string) => void;
}) {
  const t = useT(PC);
  const reason = useLookup(DUPLICATE_REASON);
  const status = useLookup(CARD_STATUS);
  const byAtoms = [...cluster.cards].sort((a, b) => b.atoms - a.atoms || b.facts - a.facts);
  const [keep, setKeep] = useState(byAtoms[0].card_id);
  const [merge, setMerge] = useState<Set<string>>(() => new Set());
  const fromIds = cluster.cards.filter((c) => c.card_id !== keep && merge.has(c.card_id)).map((c) => c.card_id);
  const toggle = (id: string) => setMerge((m) => { const n = new Set(m); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const keepName = cluster.cards.find((c) => c.card_id === keep)?.display_name ?? "";
  return (
    <div style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: "10px 12px", background: K.card }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 6 }}>
        <b style={{ fontSize: 13 }}>{cluster.country}</b>
        {cluster.reasons.map((r) => <Badge key={r} tone="warning" dot={false}>{reason(r)}</Badge>)}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {cluster.cards.map((c) => (
          <div key={c.card_id} style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", fontSize: 12.5 }}>
            <label style={{ display: "inline-flex", gap: 4, alignItems: "center", minWidth: 64 }}>
              <Radio name={`keep-${cluster.cards[0].card_id}`} checked={keep === c.card_id} onChange={() => setKeep(c.card_id)} aria-label={t("keepAria", c.display_name)} />
              {t("keep")}
            </label>
            <label style={{ display: "inline-flex", gap: 4, alignItems: "center", minWidth: 70, color: keep === c.card_id ? K.muted2 : K.ink3 }}>
              <Checkbox disabled={keep === c.card_id} checked={keep !== c.card_id && merge.has(c.card_id)} onChange={() => toggle(c.card_id)} aria-label={t("mergeAria", c.display_name)} />
              {t("merge")}
            </label>
            <span style={{ minWidth: 0 }}>
              <b>{c.display_name}</b>
              <span style={{ color: K.muted }}> — {[c.locality, c.region].filter(Boolean).join(", ") || t("noLocality")} · {t("cardCounts", c.atoms, c.facts)} · {status(c.status)}</span>
            </span>
          </div>
        ))}
      </div>
      <div style={{ marginTop: 8 }}>
        <Button size="sm" disabled={busy || fromIds.length === 0} onClick={() => onMerge(keep, fromIds, keepName)}>
          {t("mergeInto", fromIds.length ? String(fromIds.length) : "", keepName)}
        </Button>
      </div>
    </div>
  );
}

function DuplicatesPanel() {
  const t = useT(PC);
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
      toast.success(t("merged", r.atoms_moved));
      setPending(null);
      qc.invalidateQueries({ queryKey: ["cq-card-duplicates"] });
      qc.invalidateQueries({ queryKey: ["cq-cards"] });
    },
    onError: (e) => { toast.error(t("couldNotMerge", String(e))); setPending(null); },
  });
  const clusters = data?.data ?? [];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Button size="sm" variant="secondary" onClick={() => setOpen((o) => !o)}>
          {open ? t("hideDuplicates") : t("findDuplicates")}
        </Button>
        {open && !isLoading && !error && <span style={{ fontSize: 12, color: K.muted }}>{t("candidateGroups", clusters.length)}</span>}
      </div>
      {open && isLoading && <span style={{ fontSize: 12, color: K.muted }}>{t("loading")}</span>}
      {open && error && <span style={{ fontSize: 12, color: K.danger }}>{t("couldNotLoadDuplicates", String(error))}</span>}
      {open && clusters.map((c) => (
        <DuplicateClusterRow key={c.cards.map((x) => x.card_id).join(",")} cluster={c} busy={merge.isPending}
          onMerge={(keepId, fromIds, name) => setPending({ keepId, fromIds, name })} />
      ))}
      <ConfirmModal
        open={pending !== null}
        onClose={() => setPending(null)}
        onConfirm={() => pending && merge.mutate({ keepId: pending.keepId, fromIds: pending.fromIds })}
        title={t("mergeTitle")}
        body={t("mergeBody", pending?.fromIds.length ?? 0, pending?.name ?? "")}
        confirmLabel={t("mergeConfirm")}
        destructive
        busy={merge.isPending}
      />
    </div>
  );
}

function PlaceCardsTable({ view, update }: { view: View; update: (p: Partial<View>) => void }) {
  const t = useT(PC);
  const status = useT(CARD_STATUS);
  const statusOf = useLookup(CARD_STATUS);
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
    onError: (e) => toast.error(t("couldNotSave", String(e))),
  });
  const build = useMutation({
    mutationFn: () => apiSend<{ job_id: string }>(`/api/admin/content-quality/place-cards/build`, { body: { tour_id: tourId.trim(), force }, admin: true }),
    onSuccess: (r) => { toast.success(t("jobQueued", r.job_id)); setTourId(""); setForce(false); },
    onError: (e) => toast.error(t("couldNotQueue", String(e))),
  });
  const columns = useMemo<ColumnDef<PlaceCard, unknown>[]>(() => [
    { accessorKey: "display_name", header: t("colPlace"),
      cell: (c) => { const p = c.row.original; return <div><b>{p.display_name}</b><div style={{ fontSize: 11.5, color: K.muted }}>{[p.locality, p.region, p.country].filter(Boolean).join(", ")}</div></div>; } },
    { id: "facts", header: t("colCheckedFacts"), enableSorting: false,
      cell: (c) => <div style={{ fontSize: 12, maxWidth: 460 }}>{c.row.original.naming_note && <div style={{ color: K.muted, marginBottom: 2 }}>{c.row.original.naming_note}</div>}{c.row.original.facts.map((f, i) => <div key={i}>• {f.text} <span style={{ color: K.muted, fontSize: 11 }}>({f.kind || "what_it_is"})</span></div>)}</div> },
    { accessorKey: "atoms", header: t("colAtoms") },
    { accessorKey: "status", header: t("colStatus"),
      cell: (c) => <Badge tone={c.row.original.status === "verified" ? "success" : c.row.original.status === "rejected" ? "danger" : "neutral"} dot={false}>{statusOf(c.row.original.status)}</Badge> },
    { id: "actions", header: "", enableSorting: false,
      cell: (c) => (
        <div style={{ display: "flex", gap: 6 }} onClick={(e) => e.stopPropagation()}>
          <Button size="sm" disabled={verdict.isPending || c.row.original.status === "verified"} onClick={() => verdict.mutate({ id: c.row.original.card_id, status: "verified" })}>{t("verify")}</Button>
          <Button size="sm" variant="danger" disabled={verdict.isPending || c.row.original.status === "rejected"} onClick={() => verdict.mutate({ id: c.row.original.card_id, status: "rejected" })}>{t("reject")}</Button>
          <Button size="sm" variant="secondary" onClick={() => setEditing(c.row.original.card_id)}>{t("facts")}</Button>
        </div>
      ) },
  ], [verdict, t, statusOf]);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, minWidth: 0 }}>
    <DuplicatesPanel />
    <DataTable<PlaceCard>
      data={data?.data ?? []} columns={columns} tableId="admin-content-quality-cards" getRowId={(r) => r.card_id}
      loading={isLoading} error={error ? String(error) : null} onRetry={() => refetch()}
      serverPagination={{ pageIndex: view.page, pageSize: PAGE_SIZE, total: data?.total ?? 0, onChange: (i) => update({ page: i }) }}
      pageSize={PAGE_SIZE} tableMinWidth={900}
      emptyTitle={t("cardsEmpty")} emptyDescription={t("cardsEmptyDesc")}
      toolbarExtra={
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <Input value={view.q} onChange={(e) => update({ q: e.target.value, page: 0 })} placeholder={t("searchPlaces")} aria-label={t("searchPlacesAria")} style={inputStyle} />
          <Select value={view.status} onChange={(e) => update({ status: e.target.value, page: 0 })} aria-label={t("cardStatusAria")} style={inputStyle}>
            <option value="">{t("allStatuses")}</option>
            {(["auto_checked", "verified", "rejected"] as const).map((s) => <option key={s} value={s}>{status(s)}</option>)}
          </Select>
          <Input value={tourId} onChange={(e) => setTourId(e.target.value)} placeholder="tour_id (uuid)" aria-label={t("tourIdAria")} style={{ ...inputStyle, fontFamily: mono, minWidth: 260 }} />
          <Button size="sm" disabled={!/^[0-9a-f-]{36}$/i.test(tourId.trim()) || build.isPending} onClick={() => build.mutate()}>{t("buildCards")}</Button>
          <label style={{ display: "inline-flex", gap: 4, alignItems: "center", fontSize: 12, color: K.ink3 }} title={t("retryMissesTitle")}>
            <Checkbox checked={force} onChange={(e) => setForce(e.target.checked)} aria-label={t("retryMissesAria")} />
            {t("retryMisses")}
          </label>
        </div>
      }
    />
    {editing && <PlaceCardFactsDrawer cardId={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

function Page() {
  const t = useT(PC);
  const hydrated = useSyncExternalStore(noopSubscribe, () => true, () => false);
  const [edited, setEdited] = useState<View | null>(null);
  const view = edited ?? (hydrated ? readView() : EMPTY);
  const update = (patch: Partial<View>) => { const n = { ...view, ...patch }; writeView(n); setEdited(n); };
  return (
    <PageFrame>
      <PageHeader title={t("title")} description={t("description")} />
      <div data-testid="place-cards-ready" style={{ minWidth: 0, width: "100%", maxWidth: "100%" }}>
        <PlaceCardsTable view={view} update={update} />
      </div>
    </PageFrame>
  );
}

export default function PlaceCardsPage() {
  return <ToastProvider><Page /></ToastProvider>;
}
