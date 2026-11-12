"use client";
// app/admin/reference-prices/ReferencePricesConsole.tsx
// AA-1091 — AA Catalogue › Reference prices: the structured reference price of every AA tour (amount, currency,
// basis) for the customer-service team. Each row shows the source price text of the upload, the saved price (or the
// parser's suggestion) in an inline editor, and its status; a bulk "Accept" saves the suggestions of the selected rows;
// Excel export / import (DRY RUN first) fills many tours at once; "Read source texts" saves the parser's suggestions as
// Not reviewed. Root ADR 0005 d7: a reference price, never a selling price.
//
// API (CIS BFF → api/routers/admin_reference_prices.py): GET list (server filters + paging), PATCH / DELETE one key,
// POST accept / parse / import, GET export (base64 JSON). Who: lib/rbac.ts PAGE_RULES /admin/reference-prices (readers),
// writes need reference_price.write (admin, content_lead, customer_success), hidden with can() and refused by the BFF
// (API_WRITE_RULES "reference-prices") and the backend. No polling (S240): load on open + Refresh.

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Download, FileUp, RefreshCw, ScanText, X } from "lucide-react";
import { useMemo, useState } from "react";
import {
  PageFrame,
  Badge,
  Button,
  DataTable,
  K,
  KpiTile,
  Modal,
  PageHeader,
  Pressable,
  ApiError,
  apiGet,
  useAdminIdentity,
  useFormat,
  useT,
  useToast,
  type ColumnDef,
  type Translator,
  type Tone,
  Checkbox, Input, Select,
} from "../../_kit";
import { can } from "@/lib/rbac";
import {
  BASES,
  CURRENCIES,
  EMPTY_FILTERS,
  FILTERS,
  MAX_IMPORT_BYTES,
  MAX_NOTE,
  SHOWN_ACTIONS,
  canAccept,
  draftChanged,
  draftOf,
  draftProblem,
  exportUrl,
  fromBase64,
  linesToShow,
  listUrl,
  refCall,
  saveBody,
  toBase64,
  trimZeros,
  type Basis,
  type Draft,
  type ImportLine,
  type ImportResult,
  type RefFilter,
  type RefFilters,
  type RefItem,
  type RefPage,
  type RefStatus,
} from "@/lib/referencePrices";
import { RM } from "./messages";

const LIST_KEY = ["admin-reference-prices"] as const;
type T = Translator<typeof RM.en>;

const STATUS_TONE: Record<RefStatus, Tone> = {
  missing: "danger", suggested: "info", needs_review: "warning", parsed: "neutral", confirmed: "success",
};

function basisLabel(t: T, b: Basis | "" | null | undefined): string {
  return b ? t(`b_${b}` as "b_from") : "";
}

function reasonLabel(t: T, r: string): string {
  const key = `r_${r}`;
  return key in RM.en ? t(key as "r_tiered") : r;
}

function priceText(t: T, amount: string | null | undefined, currency: string | null | undefined,
  basis: Basis | null | undefined): string {
  if (!amount || !currency) return "";
  return `${currency} ${trimZeros(amount)} · ${basisLabel(t, basis)}`;
}

export default function ReferencePricesConsole() {
  const t = useT(RM);
  const toast = useToast();
  const qc = useQueryClient();
  const role = useAdminIdentity().role;
  const canWrite = can(role, "reference_price.write");
  const [filters, setFilters] = useState<RefFilters>(EMPTY_FILTERS);
  const [page, setPage] = useState({ index: 0, size: 50 });
  const [importing, setImporting] = useState(false);
  const [parsing, setParsing] = useState(false);
  const [exporting, setExporting] = useState(false);

  const list = useQuery({
    queryKey: [...LIST_KEY, filters, page],
    queryFn: () => apiGet<RefPage>(listUrl(filters, page)),
    placeholderData: keepPreviousData,
    retry: false,
  });
  const refresh = () => { void qc.invalidateQueries({ queryKey: LIST_KEY }); };
  const setFilter = (next: Partial<RefFilters>) => { setFilters((f) => ({ ...f, ...next })); setPage((p) => ({ ...p, index: 0 })); };

  const notMigrated = list.error instanceof ApiError && list.error.status === 503;
  const listError = list.error ? (notMigrated ? t("notMigrated") : (list.error as Error).message || t("listError")) : null;
  const counts = list.data?.counts;

  const doExport = async () => {
    setExporting(true);
    const r = await refCall<{ filename: string; content_base64: string; content_type: string }>(
      "GET", exportUrl(filters).slice("/api/admin/reference-prices".length));
    setExporting(false);
    if (!r.ok) { toast.error(r.message); return; }
    const blob = new Blob([fromBase64(r.body.content_base64) as BlobPart], { type: r.body.content_type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = r.body.filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  const accept = async (keys: string[], done?: () => void) => {
    const r = await refCall<{ confirmed: string[]; skipped: unknown[] }>("POST", "/accept", { source_keys: keys });
    if (!r.ok) { toast.error(r.message); return; }
    toast.success(t("accepted", r.body.confirmed.length, r.body.skipped.length));
    done?.();
    refresh();
  };

  const columns = useMemo<ColumnDef<RefItem, unknown>[]>(() => [
    {
      id: "tour", header: t("colTour"), enableSorting: false,
      cell: (c) => {
        const i = c.row.original;
        return (
          <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0, maxWidth: 260 }}>
            <span style={{ fontWeight: 600, color: K.ink, overflowWrap: "anywhere" }}>{i.name || i.source_key}</span>
            <span style={{ fontSize: 11.5, color: K.muted }}>
              {[i.country, i.duration].filter(Boolean).join(" · ")}{!i.live && ` · ${t("notLive")}`}
            </span>
          </span>
        );
      },
    },
    {
      id: "source", header: t("colSource"), enableSorting: false,
      cell: (c) => {
        const i = c.row.original;
        return i.price_raw
          ? <span style={{ display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden",
            overflowWrap: "anywhere", maxWidth: 260, fontSize: 12.5, color: K.ink3 }} title={i.price_raw}>{i.price_raw}</span>
          : <span style={{ color: K.muted, fontSize: 12.5 }}>{t("noText")}</span>;
      },
    },
    {
      id: "price", header: t("colPrice"), enableSorting: false,
      cell: (c) => <PriceEditor key={`${c.row.original.source_key}:${c.row.original.saved?.updated_at ?? "-"}`}
        item={c.row.original} canWrite={canWrite} onSaved={refresh} onAccept={(k) => accept([k])} />,
    },
    {
      id: "status", header: t("colStatus"), enableSorting: false,
      cell: (c) => <StatusCell item={c.row.original} />,
    },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [t, canWrite]);

  const kpis: { key: RefFilter; label: string; hint: string; value: number | undefined; tone: Tone }[] = [
    { key: "no_price", label: t("kNoPrice"), hint: t("kNoPriceHint"), value: counts?.no_price, tone: "danger" },
    { key: "needs_review", label: t("kReview"), hint: t("kReviewHint"), value: counts?.needs_review, tone: "warning" },
    { key: "parsed", label: t("kParsed"), hint: t("kParsedHint"), value: counts?.parsed, tone: "neutral" },
    { key: "confirmed", label: t("kConfirmed"), hint: t("kConfirmedHint"), value: counts?.confirmed, tone: "success" },
  ];

  const small = { minHeight: 32, padding: "5px 8px", fontSize: 12.5, width: "auto", maxWidth: "100%" } as const;
  const filterBar = (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", minWidth: 0 }}>
      <Select aria-label={t("fStatus")} value={filters.status} style={small}
        onChange={(e) => setFilter({ status: e.target.value as RefFilter })}>
        {FILTERS.map((f) => <option key={f} value={f}>{t(`f_${f}` as "f_all")}</option>)}
      </Select>
      <Select aria-label={t("fCountry")} value={filters.country} style={small}
        onChange={(e) => setFilter({ country: e.target.value })}>
        <option value="">{t("allCountries")}</option>
        {(list.data?.countries ?? []).map((c) => <option key={c} value={c}>{c}</option>)}
      </Select>
      <Input type="search" aria-label={t("search")} placeholder={t("search")} value={filters.q} maxLength={200}
        onChange={(e) => setFilter({ q: e.target.value })} style={{ ...small, width: 240 }} />
      <label style={{ display: "inline-flex", gap: 6, alignItems: "center", fontSize: 12.5, color: K.ink3 }}>
        <Checkbox checked={filters.liveOnly} onChange={(e) => setFilter({ liveOnly: e.target.checked })} />
        {t("liveOnly")}
      </label>
    </div>
  );

  const filtered = filters.status !== "no_price" || !!filters.country || !!filters.q.trim() || !filters.liveOnly;

  return (
    <PageFrame>
      <PageHeader
        title={t("title")}
        breadcrumbs={[{ label: t("catalogue") }, { label: t("title") }]}
        description={t("description")}
        actions={
          <>
            <Button variant="secondary" size="sm" onClick={refresh} disabled={list.isFetching}>
              <RefreshCw size={13} /> {list.isFetching ? t("loading") : t("refresh")}
            </Button>
            <Button variant="secondary" size="sm" onClick={() => void doExport()} disabled={exporting || notMigrated}
              testId="refprice-export">
              <Download size={13} /> {exporting ? t("exporting") : t("exportExcel")}
            </Button>
            {canWrite && (
              <>
                <Button variant="secondary" size="sm" onClick={() => setParsing(true)} disabled={notMigrated}
                  testId="refprice-parse">
                  <ScanText size={13} /> {t("parseTexts")}
                </Button>
                <Button variant="primary" size="sm" onClick={() => setImporting(true)} disabled={notMigrated}
                  testId="refprice-import">
                  <FileUp size={13} /> {t("importExcel")}
                </Button>
              </>
            )}
          </>
        }
      />

      {!canWrite && <p style={{ margin: "0 0 12px", fontSize: 12.5, color: K.muted }}>{t("readOnly")}</p>}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12,
        marginBottom: 16 }}>
        {kpis.map((k) => (
          <Pressable key={k.key} onClick={() => setFilter({ status: k.key })} aria-pressed={filters.status === k.key}
            testId={`refprice-kpi-${k.key}`}
            style={{ borderRadius: 12, outline: filters.status === k.key ? `2px solid ${K.accent}` : "none" }}>
            <KpiTile size="sm" label={k.label} hint={k.hint} value={list.isLoading ? undefined : k.value ?? null}
              valueTone={k.tone} />
          </Pressable>
        ))}
      </div>

      <DataTable<RefItem>
        tableId="admin-reference-prices"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.source_key}
        rowTestId={(r) => `refprice-row-${r.source_key}`}
        loading={list.isLoading}
        error={listError}
        onRetry={refresh}
        toolbarExtra={filterBar}
        enableSelection={canWrite}
        bulkActions={canWrite ? ({ selectedRows, clearSelection }) => (
          <Button size="sm" variant="primary" testId="refprice-accept-selected"
            disabled={!selectedRows.some(canAccept)}
            onClick={() => void accept(selectedRows.filter(canAccept).map((r) => r.source_key), clearSelection)}>
            <Check size={13} /> {t("acceptSelected", selectedRows.filter(canAccept).length)}
          </Button>
        ) : undefined}
        serverPagination={{
          pageIndex: page.index, pageSize: page.size, total: list.data?.total ?? 0,
          onChange: (index, size) => setPage({ index, size }),
        }}
        pageSizeOptions={[25, 50, 100, 200]}
        emptyTitle={filtered ? t("emptyFiltered") : t("emptyNone")}
        emptyDescription={filtered ? t("emptyFilteredDesc") : t("emptyNoneDesc")}
        tableMinWidth={980}
        compact
      />

      {canWrite && importing && <ImportModal onClose={() => setImporting(false)} onApplied={refresh} />}
      {canWrite && parsing && <ParseModal onClose={() => setParsing(false)} onApplied={refresh} />}
    </PageFrame>
  );
}

// ── one row: status ──────────────────────────────────────────────────────────────────────────────────────────────

function StatusCell({ item }: { item: RefItem }) {
  const t = useT(RM);
  const { formatDate } = useFormat();
  const s = item.saved;
  const reasons = s?.needs_review ? s.review_reasons : item.status === "suggested" || item.status === "missing"
    ? item.parse_reasons : [];
  return (
    <span style={{ display: "flex", flexDirection: "column", gap: 4, alignItems: "flex-start", maxWidth: 200 }}>
      <Badge tone={STATUS_TONE[item.status]}>{t(`s_${item.status}` as "s_missing")}</Badge>
      {item.text_changed && <span style={{ fontSize: 11.5, color: K.amber }}>{t("textChanged")}</span>}
      {reasons.length > 0 && (
        <span style={{ fontSize: 11.5, color: K.muted }}>{reasons.map((r) => reasonLabel(t, r)).join(", ")}</span>
      )}
      {s?.reviewed_at && (
        <span style={{ fontSize: 11.5, color: K.muted }}>
          {formatDate(s.reviewed_at)}{s.reviewed_by ? ` ${t("by", s.reviewed_by.replace(/^admin:/, ""))}` : ""}
        </span>
      )}
    </span>
  );
}

// ── one row: the inline editor ───────────────────────────────────────────────────────────────────────────────────

function PriceEditor({ item, canWrite, onSaved, onAccept }: {
  item: RefItem; canWrite: boolean; onSaved: () => void; onAccept: (key: string) => void;
}) {
  const t = useT(RM);
  const toast = useToast();
  const [draft, setDraft] = useState<Draft>(() => draftOf(item));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const set = <F extends keyof Draft>(k: F, v: Draft[F]) => { setDraft((d) => ({ ...d, [k]: v })); setError(null); };
  const changed = draftChanged(item, draft);
  const stop = (e: React.SyntheticEvent) => e.stopPropagation();

  if (!canWrite) {
    const s = item.saved;
    const sug = item.suggestion;
    return (
      <span style={{ display: "flex", flexDirection: "column", gap: 2, fontSize: 12.5 }}>
        <span style={{ color: K.ink, fontWeight: 600 }}>{s ? priceText(t, s.amount, s.currency, s.basis) : "—"}</span>
        {sug && <span style={{ color: K.muted }}>{t("suggestion", priceText(t, sug.amount, sug.currency, sug.basis))}</span>}
        {s?.note && <span style={{ color: K.muted }}>{s.note}</span>}
      </span>
    );
  }

  const save = async () => {
    const problem = draftProblem(draft);
    if (problem) { setError(t(problem)); return; }
    setBusy(true);
    const r = await refCall<RefItem>("PATCH", `/${encodeURIComponent(item.source_key)}`, saveBody(item, draft));
    setBusy(false);
    if (!r.ok) { setError(r.message); return; }
    toast.success(t("saved"));
    onSaved();
  };
  const clear = async () => {
    setBusy(true);
    const r = await refCall("DELETE", `/${encodeURIComponent(item.source_key)}`);
    setBusy(false);
    setConfirmClear(false);
    if (!r.ok) { toast.error(r.message); return; }
    toast.success(t("cleared"));
    onSaved();
  };
  const id = (s: string) => `refprice-${item.source_key}-${s}`;
  const ctl = { minHeight: 30, padding: "4px 6px", fontSize: 12.5 } as const;

  return (
    <div onClick={stop} onKeyDown={stop} role="group" aria-label={`${t("colPrice")}: ${item.name ?? item.source_key}`}
      style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 300 }}>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
        <Input id={id("amount")} aria-label={t("amount")} inputMode="decimal" autoComplete="off" value={draft.amount}
          placeholder={t("amount")} onChange={(e) => set("amount", e.target.value)} disabled={busy}
          onKeyDown={(e) => { if (e.key === "Enter") void save(); }} style={{ ...ctl, width: 96 }} />
        <Select id={id("currency")} aria-label={t("currency")} value={draft.currency} disabled={busy}
          onChange={(e) => set("currency", e.target.value)} style={{ ...ctl, width: 76 }}>
          {!(CURRENCIES as readonly string[]).includes(draft.currency) && draft.currency && (
            <option value={draft.currency}>{draft.currency}</option>
          )}
          {CURRENCIES.map((c) => <option key={c} value={c}>{c}</option>)}
        </Select>
        <Select id={id("basis")} aria-label={t("basis")} value={draft.basis} disabled={busy}
          onChange={(e) => set("basis", e.target.value as Basis)} style={{ ...ctl, width: 130 }}>
          {BASES.map((b) => <option key={b} value={b}>{basisLabel(t, b)}</option>)}
        </Select>
        <Button size="sm" variant="primary" onClick={() => void save()} disabled={busy || !changed}
          testId={`refprice-save-${item.source_key}`}>
          {busy ? t("saving") : t("save")}
        </Button>
        {canAccept(item) && !changed && (
          <Button size="sm" variant="secondary" onClick={() => onAccept(item.source_key)} disabled={busy}
            testId={`refprice-accept-${item.source_key}`}>
            <Check size={12} /> {t("accept")}
          </Button>
        )}
        {item.saved && (
          <Button size="sm" variant="ghost" onClick={() => setConfirmClear(true)} disabled={busy} title={t("clear")}
            aria-label={t("clear")}>
            <X size={12} />
          </Button>
        )}
      </div>
      <Input aria-label={t("note")} placeholder={t("note")} value={draft.note} maxLength={MAX_NOTE} disabled={busy}
        onChange={(e) => set("note", e.target.value)} style={{ ...ctl, width: "100%" }} />
      {item.saved && item.suggestion && (
        <span style={{ fontSize: 11.5, color: K.muted }}>
          {t("suggestion", priceText(t, item.suggestion.amount, item.suggestion.currency, item.suggestion.basis))}
        </span>
      )}
      {error && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{error}</span>}
      <Modal open={confirmClear} onClose={() => { if (!busy) setConfirmClear(false); }} title={t("clearQ")}
        footer={<>
          <Button variant="ghost" size="sm" onClick={() => setConfirmClear(false)} disabled={busy}>{t("cancel")}</Button>
          <Button variant="danger" size="sm" onClick={() => void clear()} disabled={busy}>{t("clear")}</Button>
        </>}>
        <p style={{ margin: 0, fontSize: 13, color: K.ink3 }}>{t("clearBody")}</p>
      </Modal>
    </div>
  );
}

// ── Excel import (DRY RUN, then apply) ───────────────────────────────────────────────────────────────────────────

function ImportModal({ onClose, onApplied }: { onClose: () => void; onApplied: () => void }) {
  const t = useT(RM);
  const toast = useToast();
  const [file, setFile] = useState<{ name: string; b64: string } | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [busy, setBusy] = useState<"check" | "apply" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const pick = async (f: File | undefined) => {
    setResult(null);
    setError(null);
    if (!f) { setFile(null); return; }
    if (f.size > MAX_IMPORT_BYTES) { setFile(null); setError(t("fileTooBig")); return; }
    setFile({ name: f.name, b64: toBase64(new Uint8Array(await f.arrayBuffer())) });
  };
  const send = async (dryRun: boolean) => {
    if (!file) return;
    setBusy(dryRun ? "check" : "apply");
    setError(null);
    const r = await refCall<ImportResult>("POST", "/import", { file_base64: file.b64, dry_run: dryRun });
    setBusy(null);
    if (!r.ok) {
      const d = r.detail as { summary?: ImportResult["summary"]; rows?: ImportResult["rows"] } | undefined;
      if (d?.rows && d.summary) setResult({ dry_run: true, applied: 0, summary: d.summary, rows: d.rows });
      setError(r.message);
      return;
    }
    if (dryRun) { setResult(r.body); return; }
    toast.success(t("importDone", r.body.applied));
    onApplied();
    onClose();
  };

  const shown = result ? linesToShow(result.rows) : [];
  const importColumns = useMemo<ColumnDef<ImportLine, unknown>[]>(() => [
    { accessorKey: "line", header: t("colLine") },
    { id: "tour", header: t("colTour"), enableSorting: false,
      cell: (c) => <span style={{ overflowWrap: "anywhere" }}>{c.row.original.name || c.row.original.source_key || "—"}</span> },
    { id: "result", header: t("colResult"), enableSorting: false,
      cell: (c) => {
        const r = c.row.original;
        return r.action === "error"
          ? <span style={{ color: K.danger }}>{t("a_error")}: {r.error}</span>
          : <span>{t(r.action === "create" ? "a_create" : r.action === "confirm" ? "a_confirm" : "a_update")}
            {r.after && `: ${priceText(t, r.after.amount, r.after.currency, r.after.basis)}`}</span>;
      } },
  ], [t]);
  const hidden = result ? result.rows.filter((r) => SHOWN_ACTIONS.includes(r.action)).length - shown.length : 0;
  const canApply = !!result && result.summary.errors === 0 && result.summary.changes > 0 && !busy;

  return (
    <Modal open onClose={() => { if (!busy) onClose(); }} title={t("importTitle")} width={720} pinFooter
      testId="refprice-import-modal"
      footer={<>
        <Button variant="ghost" size="sm" onClick={onClose} disabled={!!busy}>{t("close")}</Button>
        <Button variant="secondary" size="sm" onClick={() => void send(true)} disabled={!file || !!busy}
          testId="refprice-import-check">
          {busy === "check" ? t("checking") : t("dryRun")}
        </Button>
        <Button variant="primary" size="sm" onClick={() => void send(false)} disabled={!canApply}
          testId="refprice-import-apply">
          {busy === "apply" ? t("applying") : t("applyImport")}
        </Button>
      </>}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12, fontSize: 13, color: K.ink3 }}>
        <p style={{ margin: 0, lineHeight: 1.5 }}>{t("importIntro")}</p>
        <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <span style={{ fontWeight: 600, color: K.ink }}>{t("chooseFile")}</span>
          <Input type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            onChange={(e) => void pick(e.target.files?.[0])} disabled={!!busy} />
        </label>
        {result && (
          <div role="status" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <strong style={{ color: K.ink }}>
              {t("importSummary", result.summary.rows, result.summary.changes, result.summary.errors)}
            </strong>
            {result.summary.errors > 0 && <span style={{ color: K.danger }}>{t("importFixErrors")}</span>}
            {result.summary.errors === 0 && result.summary.changes === 0 && <span>{t("importNothing")}</span>}
            {shown.length > 0 && (
              <DataTable<ImportLine>
                tableId="admin-reference-prices-import"
                data={shown}
                columns={importColumns}
                getRowId={(r) => `${r.line}-${r.source_key}`}
                paginate={false}
                maxBodyHeight={320}
                compact
              />
            )}
            {hidden > 0 && <span style={{ color: K.muted }}>{t("moreLines", hidden)}</span>}
          </div>
        )}
        {error && <span role="alert" style={{ color: K.danger }}>{error}</span>}
      </div>
    </Modal>
  );
}

// ── Read the source texts (DRY RUN on open, then save) ───────────────────────────────────────────────────────────

interface ParseSummary {
  dry_run: boolean;
  would_create?: number; would_update?: number; created?: number; updated?: number;
  unparseable: number; needs_review: number;
  sample: { source_key: string; name: string | null; price_raw: string | null; amount: string | null;
    currency: string | null; basis: Basis | null; reasons: string[] }[];
}

function ParseModal({ onClose, onApplied }: { onClose: () => void; onApplied: () => void }) {
  const t = useT(RM);
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const plan = useQuery({
    queryKey: [...LIST_KEY, "parse-plan"],
    queryFn: async () => {
      const r = await refCall<ParseSummary>("POST", "/parse", { dry_run: true });
      if (!r.ok) throw new Error(r.message);
      return r.body;
    },
    retry: false,
    gcTime: 0,
  });
  const p = plan.data;
  const changes = (p?.would_create ?? 0) + (p?.would_update ?? 0);
  const apply = async () => {
    setBusy(true);
    const r = await refCall<ParseSummary>("POST", "/parse", { dry_run: false });
    setBusy(false);
    if (!r.ok) { toast.error(r.message); return; }
    toast.success(t("parseDone", (r.body.created ?? 0) + (r.body.updated ?? 0)));
    onApplied();
    onClose();
  };
  return (
    <Modal open onClose={() => { if (!busy) onClose(); }} title={t("parseTitle")} width={640} pinFooter
      testId="refprice-parse-modal"
      footer={<>
        <Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>{t("close")}</Button>
        <Button variant="primary" size="sm" onClick={() => void apply()} disabled={busy || !changes}
          testId="refprice-parse-apply">
          {busy ? t("saving") : t("parseApply")}
        </Button>
      </>}>
      <div style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: 13, color: K.ink3 }}>
        <p style={{ margin: 0, lineHeight: 1.5 }}>{t("parseIntro")}</p>
        {plan.isLoading && <span>{t("loading")}</span>}
        {plan.error && <span role="alert" style={{ color: K.danger }}>{(plan.error as Error).message}</span>}
        {p && (
          <>
            <strong style={{ color: K.ink }}>
              {t("parseCounts", p.would_create ?? 0, p.would_update ?? 0, p.needs_review, p.unparseable)}
            </strong>
            {p.sample.length > 0 && (
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <span style={{ fontWeight: 600, color: K.ink }}>{t("sample")}</span>
                {p.sample.map((s) => (
                  <span key={s.source_key} style={{ fontSize: 12.5, overflowWrap: "anywhere" }}>
                    {s.name || s.source_key}: “{s.price_raw}” → {priceText(t, s.amount, s.currency, s.basis)}
                    {s.reasons.length > 0 && ` (${s.reasons.map((r) => reasonLabel(t, r)).join(", ")})`}
                  </span>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </Modal>
  );
}
