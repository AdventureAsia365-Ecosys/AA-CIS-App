"use client";
// app/admin/aa-content/AaContentConsole.tsx
// AA-977 (CRM-B5, slice S3) — CIS Content › Assets › AA content (CRM): the list of AA-authored content (Before you go
// prep packs, starter templates, campaign blocks) that agencies receive in the partner CRM over catalog.http.v1
// `/catalog/v1/aa-content` once published.
//
// API (CIS BFF → api/routers/admin_aa_content.py):
//   GET  /api/admin/aa-content?kind=&locale=&status=&country=&limit=&offset=   one server page (filters + paging on the
//                                                                              server, lesson S218)
//   POST /api/admin/aa-content                                                 create {kind, locale, group_key, title}
// A row opens the editor drawer (AaContentEditor.tsx) on the row's group: the EN and VI copies side by side.
//
// Who sees what: every staff role reads the list (the viewer included); "New content" needs content.write. No
// polling (S240): load on open + Refresh. AA-994: text in the UI language (messages.ts, useAaText.ts).

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  K,
  Modal,
  PageHeader,
  apiGet,
  useAdminIdentity,
  useFormat,
  useT,
  useToast,
  type ColumnDef, PageFrame,
} from "../../_kit";
import { can } from "@/lib/rbac";
import {
  EMPTY_FILTERS,
  KINDS,
  LOCALES,
  STATUSES,
  aaCall,
  countryLabel,
  countryOptions,
  isGroupKey,
  listUrl,
  pendingDraftLabel,
  stateBadge,
  suggestGroupKey,
  type AaItem,
  type AaKind,
  type AaListFilters,
  type AaListPage,
  type AaListRow,
  type AaLocale,
  type AaState,
} from "@/lib/aaContent";
import AaContentEditor, { Field, InlineError, fieldStyle, noteStyle } from "./AaContentEditor";
import { AC } from "./messages";
import { useAaText } from "./useAaText";

export const LIST_KEY = ["aa-content-list"] as const;
const PAGE_SIZES = [25, 50, 100];

export default function AaContentConsole() {
  const t = useT(AC);
  const tx = useAaText();
  const { formatDateTime } = useFormat();
  const qc = useQueryClient();
  const role = useAdminIdentity().role;
  const canWrite = can(role, "content.write");
  const [filters, setFilters] = useState<AaListFilters>(EMPTY_FILTERS);
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0]);
  const [openGroup, setOpenGroup] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const list = useQuery({
    queryKey: [...LIST_KEY, filters, pageIndex, pageSize],
    queryFn: () => apiGet<AaListPage>(listUrl(filters, pageIndex, pageSize)),
    placeholderData: keepPreviousData,
    retry: false,
  });

  // A filter change goes back to page 1 in the same handler (no setState in an effect).
  const setFilter = <F extends keyof AaListFilters>(key: F, value: AaListFilters[F]) => {
    setFilters((f) => ({ ...f, [key]: value }));
    setPageIndex(0);
  };

  const columns = useMemo<ColumnDef<AaListRow, unknown>[]>(() => [
    {
      accessorKey: "title", header: t("colTitle"),
      cell: (c) => (
        <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
          <span style={{ fontWeight: 600, color: K.ink, overflowWrap: "anywhere" }}>
            {c.row.original.title || <span style={{ color: K.muted }}>{t("noTitle")}</span>}
          </span>
          <span style={{ fontSize: 11.5, color: K.muted, overflowWrap: "anywhere" }}>{c.row.original.group_key}</span>
        </span>
      ),
    },
    { accessorKey: "kind", header: t("colKind"), cell: (c) => tx.kind[c.row.original.kind] ?? c.row.original.kind },
    {
      accessorKey: "locale", header: t("colLanguage"),
      cell: (c) => <Badge tone="info" dot={false}>{c.row.original.locale.toUpperCase()}</Badge>,
    },
    {
      accessorKey: "state", header: t("colStatus"),
      cell: (c) => {
        const s = stateBadge(c.row.original, tx.words);
        const pending = pendingDraftLabel(c.row.original, tx.words);
        return (
          <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
            <Badge tone={s.tone}>{s.label}</Badge>
            {pending && <Badge tone="warning" dot={false}>{pending}</Badge>}
          </span>
        );
      },
    },
    {
      accessorKey: "country_codes", header: t("colCountries"),
      cell: (c) => {
        const codes = c.row.original.country_codes;
        if (!codes.length) return <span style={{ color: K.muted }}>{t("all")}</span>;
        return <span title={codes.map((code) => countryLabel(code, tx.lang)).join(", ")}>{codes.join(", ")}</span>;
      },
    },
    { accessorKey: "updated_at", header: t("colUpdated"), cell: (c) => formatDateTime(c.row.original.updated_at) },
  ], [t, tx, formatDateTime]);

  const refresh = () => { void qc.invalidateQueries({ queryKey: LIST_KEY }); };
  const listError = list.error ? (list.error as Error).message || t("listError") : null;
  const filtered = filters.kind || filters.locale || filters.status || filters.country;

  const filterBar = (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", minWidth: 0 }}>
      <FilterSelect label={t("colKind")} value={filters.kind} onChange={(v) => setFilter("kind", v as AaKind | "")}
        options={KINDS.map((k) => ({ value: k, label: tx.kind[k] }))} allLabel={t("allKinds")} />
      <FilterSelect label={t("colLanguage")} value={filters.locale} onChange={(v) => setFilter("locale", v as AaLocale | "")}
        options={LOCALES.map((l) => ({ value: l, label: tx.locale[l] }))} allLabel={t("allLanguages")} />
      <FilterSelect label={t("colStatus")} value={filters.status} onChange={(v) => setFilter("status", v as AaState | "")}
        options={STATUSES.map((s) => ({ value: s, label: tx.status[s] }))} allLabel={t("anyStatus")} />
      <FilterSelect label={t("country")} value={filters.country} onChange={(v) => setFilter("country", v)}
        options={countryOptions([], tx.lang).map((c) => ({ value: c.code, label: c.name }))} allLabel={t("anyCountry")} />
    </div>
  );

  return (
    <PageFrame>
      <PageHeader
        title={t("title")}
        breadcrumbs={[{ label: t("cisContent") }, { label: t("assets") }, { label: t("title") }]}
        description={t("description")}
        actions={
          <>
            <Button variant="secondary" size="sm" onClick={refresh} disabled={list.isFetching}>
              <RefreshCw size={13} /> {list.isFetching ? t("loading") : t("refresh")}
            </Button>
            {canWrite && (
              <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
                <Plus size={13} /> {t("newContent")}
              </Button>
            )}
          </>
        }
      />

      <DataTable<AaListRow>
        tableId="aa-content"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.content_id}
        loading={list.isLoading}
        error={listError}
        onRetry={refresh}
        toolbarExtra={filterBar}
        pageSize={pageSize}
        pageSizeOptions={PAGE_SIZES}
        serverPagination={{
          pageIndex, pageSize, total: list.data?.total ?? 0,
          onChange: (i, size) => { setPageIndex(size === pageSize ? i : 0); setPageSize(size); },
        }}
        emptyTitle={filtered ? t("emptyFiltered") : t("emptyNone")}
        emptyDescription={filtered ? t("emptyFilteredDesc") : canWrite ? t("emptyWriter") : t("emptyReader")}
        onRowClick={(r) => setOpenGroup(r.group_key)}
        tableMinWidth={760}
      />

      {openGroup && (
        <AaContentEditor key={openGroup} groupKey={openGroup} onClose={() => setOpenGroup(null)}
          onChanged={refresh} />
      )}
      {canWrite && (
        <CreateModal open={creating} onClose={() => setCreating(false)}
          onCreated={(item) => { setCreating(false); refresh(); setOpenGroup(item.group_key); }} />
      )}
    </PageFrame>
  );
}

function FilterSelect({ label, value, onChange, options, allLabel }: {
  label: string; value: string; onChange: (v: string) => void; options: { value: string; label: string }[];
  allLabel: string;
}) {
  return (
    <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}
      style={{ ...fieldStyle, width: "auto", maxWidth: "100%", minWidth: 0, minHeight: 32, padding: "5px 8px", fontSize: 12.5 }}>
      <option value="">{allLabel}</option>
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

// ── New content ──────────────────────────────────────────────────────────────────────────────────────────────────

function CreateModal({ open, onClose, onCreated }: {
  open: boolean; onClose: () => void; onCreated: (item: AaItem) => void;
}) {
  const t = useT(AC);
  const tx = useAaText();
  const toast = useToast();
  const [kind, setKind] = useState<AaKind>("prep_pack");
  const [locale, setLocale] = useState<AaLocale>("en");
  const [title, setTitle] = useState("");
  // The key follows the title until it is edited by hand (derived, no effect).
  const [manualKey, setManualKey] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; field: string | null } | null>(null);

  const groupKey = manualKey ?? suggestGroupKey(title);
  const keyOk = isGroupKey(groupKey);
  const canSubmit = title.trim().length > 0 && keyOk && !busy;

  const reset = () => { setKind("prep_pack"); setLocale("en"); setTitle(""); setManualKey(null); setError(null); };
  const close = () => { if (!busy) { reset(); onClose(); } };

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    const r = await aaCall<AaItem>("POST", "", { kind, locale, group_key: groupKey, title: title.trim() }, tx.words);
    setBusy(false);
    if (!r.ok) {
      setError({ message: r.code === "duplicate" ? t("duplicate", r.message) : r.message, field: r.field });
      return;
    }
    toast.success(t("created", r.body.group_key, r.body.locale.toUpperCase()));
    reset();
    onCreated(r.body);
  };

  return (
    <Modal open={open} onClose={close} title={t("newTitle")} width={520}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>{t("cancel")}</Button>
          <Button variant="primary" onClick={() => void submit()} disabled={!canSubmit}>
            {busy ? t("creating") : t("createDraft")}
          </Button>
        </>
      }>
      <form data-testid="aa-content-create" onSubmit={(e) => { e.preventDefault(); void submit(); }}
        style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Field label={t("kind")} htmlFor="aa-create-kind">
          <select id="aa-create-kind" value={kind} onChange={(e) => setKind(e.target.value as AaKind)} style={fieldStyle}>
            {KINDS.map((k) => <option key={k} value={k}>{tx.kind[k]}</option>)}
          </select>
        </Field>
        <Field label={t("language")} htmlFor="aa-create-locale" hint={t("languageHint")}>
          <select id="aa-create-locale" value={locale} onChange={(e) => setLocale(e.target.value as AaLocale)} style={fieldStyle}>
            {LOCALES.map((l) => <option key={l} value={l}>{tx.locale[l]}</option>)}
          </select>
        </Field>
        <Field label={t("fieldTitle")} htmlFor="aa-create-title">
          <input id="aa-create-title" type="text" autoComplete="off" maxLength={300} value={title}
            onChange={(e) => setTitle(e.target.value)} style={fieldStyle} />
        </Field>
        <Field label={t("groupKey")} htmlFor="aa-create-key"
          hint={groupKey && !keyOk ? t("groupKeyBad") : t("groupKeyHint")}>
          <input id="aa-create-key" type="text" autoComplete="off" maxLength={80} value={groupKey}
            onChange={(e) => setManualKey(e.target.value.toLowerCase())} style={fieldStyle}
            aria-invalid={!!groupKey && !keyOk} placeholder="prep-pack-vietnam" />
        </Field>
        <div style={noteStyle}>{t("startsDraft")}</div>
        {error && <InlineError message={error.field ? `${error.field}: ${error.message}` : error.message} />}
      </form>
    </Modal>
  );
}
