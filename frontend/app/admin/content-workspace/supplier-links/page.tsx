"use client";
// app/admin/content-workspace/supplier-links/page.tsx — AA-1148: the live tours whose supplier is only the text of the
// Excel sheet (raw_tours.supplier_id not set), with the AA supplier the text names when it names exactly one. Link one
// tour to its suggestion, link the selected tours to one supplier, or link every suggestion (DRY RUN first; content
// leads and administrators). Counts: not linked, with a suggestion, linked.
//
// API: GET / POST /api/admin/content-workspace/supplier-links, POST …/apply-suggestions, GET …/suppliers, GET …/options
// (BFF → api/routers/admin_content_workspace.py). Loaded on open, on a filter change and after a link: no polling.

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link2, Wand2 } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import {
  Badge, Button, Checkbox, DataTable, Field, Input, K, KpiTile, Modal, PageFrame, PageHeader, Select, ToastProvider,
  apiGet, apiSend, useAdminIdentity, useT, useToast, type ColumnDef,
} from "../../../_kit";
import { can } from "@/lib/rbac";
import {
  CW_API, CW_PAGE, tourHref, unlinkedUrl, type Options, type Supplier, type UnlinkedRow,
} from "@/lib/contentWorkspace";
import { CW } from "../messages";

interface Report {
  items: UnlinkedRow[]; total: number; unlinked: number; with_suggestion: number; linked: number;
  suppliers_available: boolean;
}

export default function SupplierLinksPage() {
  return <ToastProvider><SupplierLinks /></ToastProvider>;
}

function SupplierLinks() {
  const t = useT(CW);
  const qc = useQueryClient();
  const toast = useToast();
  const { role } = useAdminIdentity();
  const [country, setCountry] = useState("");
  const [q, setQ] = useState("");
  const [onlySuggested, setOnlySuggested] = useState(false);
  const [page, setPage] = useState({ index: 0, size: 50 });
  const [linking, setLinking] = useState<UnlinkedRow[] | null>(null);
  const [applyOpen, setApplyOpen] = useState(false);
  const options = useQuery({ queryKey: ["content-workspace-options"], retry: false,
    queryFn: () => apiGet<Options>(`${CW_API}/options`) });
  const url = unlinkedUrl({ country, q, onlySuggested, limit: page.size, offset: page.index * page.size });
  const report = useQuery({ queryKey: ["content-workspace-links", url], queryFn: () => apiGet<Report>(url),
    placeholderData: keepPreviousData, retry: false });
  const refresh = () => void qc.invalidateQueries({ queryKey: ["content-workspace-links"] });
  const writer = can(role, "content.write");
  const bulkMax = options.data?.limits.bulk_max ?? 0;

  const linkOne = async (row: UnlinkedRow) => {
    if (!row.suggestion) return;
    try {
      await apiSend(`${CW_API}/supplier-links`, { body: {
        tour_ids: [row.tour_id], supplier_id: row.suggestion.id, reason: t("linkReasonDefault") } });
      toast.success(t("linked", 1), 3000);
      refresh();
    } catch (e) {
      toast.error((e as Error).message, 5000);
    }
  };

  const columns = useMemo<ColumnDef<UnlinkedRow, unknown>[]>(() => [
    { accessorKey: "name", header: t("colName"), cell: (c) => (
      <span style={{ overflowWrap: "anywhere" }}>
        {c.row.original.entry_origin === "manual"
          ? <Link href={tourHref(c.row.original.tour_id)} style={{ color: K.accentDeep }}>{c.row.original.name}</Link>
          : c.row.original.name}
      </span>) },
    { accessorKey: "provider", header: t("colProvider"), cell: (c) => c.row.original.provider ?? "—" },
    { accessorKey: "country", header: t("colCountry"), cell: (c) => c.row.original.country ?? "—" },
    { accessorKey: "live", header: t("colLive"), cell: (c) => (
      <Badge tone={c.row.original.live ? "success" : "neutral"}>{c.row.original.live ? t("yes") : t("no")}</Badge>) },
    { id: "suggestion", header: t("colSuggestion"), cell: (c) => {
      const s = c.row.original.suggestion;
      if (!s) return <span style={{ color: K.muted }}>{t("noSuggestion")}</span>;
      return writer
        ? <Button size="sm" variant="secondary" onClick={(e) => { e.stopPropagation(); void linkOne(c.row.original); }}>
          {t("linkTo", s.company_name)}</Button>
        : s.company_name;
    } },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [t, writer]);

  const data = report.data;
  return (
    <PageFrame>
      <PageHeader
        breadcrumbs={[{ label: t("back"), href: CW_PAGE }]}
        title={t("linksTitle")}
        description={t("linksDescription")}
        actions={can(role, "content.publish") ? (
          <Button variant="secondary" size="sm" onClick={() => setApplyOpen(true)}
            disabled={!data?.suppliers_available}><Wand2 size={13} /> {t("applyButton")}</Button>) : undefined}
      />
      <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 200px), 1fr))",
        marginBottom: 16 }}>
        <KpiTile label={t("kpiUnlinked")} value={data?.unlinked} valueTone={data?.unlinked ? "warning" : undefined} />
        <KpiTile label={t("kpiSuggested")} value={data?.with_suggestion} />
        <KpiTile label={t("kpiLinked")} value={data?.linked} />
      </div>
      {data && !data.suppliers_available && (
        <p role="status" style={{ color: K.amberDeep, fontSize: 13 }}>{t("suppliersDown")}</p>)}
      <DataTable<UnlinkedRow>
        tableId="content-workspace-supplier-links"
        data={data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.tour_id}
        loading={report.isLoading}
        error={report.isError ? ((report.error as Error)?.message || t("loadError")) : null}
        onRetry={refresh}
        emptyTitle={t("noUnlinked")}
        tableMinWidth={820}
        enableSelection={writer}
        serverPagination={{ pageIndex: page.index, pageSize: page.size, total: data?.total ?? 0,
          onChange: (index, size) => setPage({ index, size }) }}
        toolbarExtra={
          <span style={{ display: "inline-flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <Input value={q} placeholder={t("search")} aria-label={t("search")} size="sm"
              onChange={(e) => { setQ(e.target.value); setPage((p) => ({ ...p, index: 0 })); }} />
            <Select value={country} size="sm" aria-label={t("colCountry")}
              options={[{ value: "", label: t("allCountries") },
                ...(options.data?.countries ?? []).map((c) => ({ value: c.iso2, label: c.name }))]}
              onChange={(e) => { setCountry(e.target.value); setPage((p) => ({ ...p, index: 0 })); }} />
            <Checkbox label={t("onlySuggested")} checked={onlySuggested}
              onChange={(e) => { setOnlySuggested(e.target.checked); setPage((p) => ({ ...p, index: 0 })); }} />
          </span>
        }
        bulkActions={({ selectedRows, clearSelection }) => (
          <Button size="sm" variant="primary" disabled={!data?.suppliers_available}
            onClick={() => { setLinking(selectedRows); clearSelection(); }}>
            <Link2 size={13} /> {t("linkSelected", selectedRows.length)}
          </Button>
        )}
      />
      {linking && <LinkModal rows={linking} bulkMax={bulkMax} onClose={() => setLinking(null)}
        onDone={(n) => { toast.success(t("linked", n), 3000); setLinking(null); refresh(); }} />}
      {applyOpen && <ApplyModal bulkMax={bulkMax} onClose={() => setApplyOpen(false)} onDone={refresh} />}
    </PageFrame>
  );
}

function LinkModal({ rows, bulkMax, onClose, onDone }: {
  rows: UnlinkedRow[]; bulkMax: number; onClose: () => void; onDone: (n: number) => void;
}) {
  const t = useT(CW);
  const toast = useToast();
  const [query, setQuery] = useState("");
  const [supplierId, setSupplierId] = useState("");
  const [reason, setReason] = useState(t("linkReasonDefault"));
  const [busy, setBusy] = useState(false);
  const sup = useQuery({
    queryKey: ["content-workspace-suppliers", query, ""],
    queryFn: () => apiGet<{ available: boolean; items: Supplier[] }>(
      `${CW_API}/suppliers?limit=200${query.trim() ? `&q=${encodeURIComponent(query.trim())}` : ""}`),
    retry: false,
  });
  const tooMany = bulkMax > 0 && rows.length > bulkMax;
  const submit = async () => {
    setBusy(true);
    try {
      const res = await apiSend<{ linked: string[] }>(`${CW_API}/supplier-links`, { body: {
        tour_ids: rows.map((r) => r.tour_id), supplier_id: supplierId, reason: reason.trim() } });
      onDone(res.linked.length);
    } catch (e) {
      toast.error((e as Error).message, 5000);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open onClose={onClose} title={t("linkTitle")} width={520} initialFocus footer={
      <>
        <Button variant="ghost" onClick={onClose}>{t("cancel")}</Button>
        <Button variant="primary" disabled={busy || tooMany || !supplierId || reason.trim().length < 3}
          onClick={() => void submit()}>{busy ? t("linking") : t("linkSelected", rows.length)}</Button>
      </>
    }>
      <div style={{ display: "grid", gap: 10 }}>
        {tooMany && <p role="alert" style={{ color: K.danger, margin: 0 }}>{t("tooMany", bulkMax)}</p>}
        <Field label={t("supplier")}>
          <div style={{ display: "grid", gap: 6 }}>
            <Input value={query} placeholder={t("supplierSearch")} aria-label={t("supplierSearch")}
              onChange={(e) => setQuery(e.target.value)} style={{ width: "100%" }} />
            <Select value={supplierId} placeholder={t("chooseSupplier")} style={{ width: "100%" }}
              options={(sup.data?.items ?? []).map((s) => ({ value: s.id,
                label: `${s.company_name ?? s.id}${s.code ? ` (${s.code})` : ""}` }))}
              onChange={(e) => setSupplierId(e.target.value)} />
          </div>
        </Field>
        <Field label={t("linkReason")}>
          <Input value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} style={{ width: "100%" }} />
        </Field>
      </div>
    </Modal>
  );
}

function ApplyModal({ bulkMax, onClose, onDone }: { bulkMax: number; onClose: () => void; onDone: () => void }) {
  const t = useT(CW);
  const toast = useToast();
  const [reason, setReason] = useState(t("linkReasonDefault"));
  const [result, setResult] = useState<string | null>(null);
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const run = async (dryRun: boolean) => {
    setBusy(true);
    try {
      const res = await apiSend<{ matches: number; would_link?: number; linked?: number; remaining?: number }>(
        `${CW_API}/supplier-links/apply-suggestions`, { body: { dry_run: dryRun, reason: reason.trim() } });
      if (dryRun) {
        setResult(t("dryResult", res.matches, res.would_link ?? 0));
        setChecked(true);
      } else {
        setResult(t("applyResult", res.linked ?? 0, res.remaining ?? 0));
        setChecked(false);
        onDone();
      }
    } catch (e) {
      toast.error((e as Error).message, 5000);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open onClose={onClose} title={t("applyTitle")} width={520} footer={
      <>
        <Button variant="ghost" onClick={onClose}>{t("close")}</Button>
        <Button variant="secondary" disabled={busy || reason.trim().length < 3} onClick={() => void run(true)}>
          {t("dryRun")}</Button>
        <Button variant="primary" disabled={busy || !checked || reason.trim().length < 3}
          onClick={() => void run(false)}>{t("applyNow")}</Button>
      </>
    }>
      <div style={{ display: "grid", gap: 10 }}>
        <p style={{ margin: 0, fontSize: 13 }}>{t("applyIntro", bulkMax)}</p>
        <Field label={t("linkReason")}>
          <Input value={reason} maxLength={500} onChange={(e) => { setReason(e.target.value); setChecked(false); }}
            style={{ width: "100%" }} />
        </Field>
        {result && <p role="status" style={{ margin: 0, fontWeight: 600 }}>{result}</p>}
      </div>
    </Modal>
  );
}
