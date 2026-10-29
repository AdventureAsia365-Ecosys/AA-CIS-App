"use client";
// app/admin/facts/FactsConsole.tsx
// AA-814 — CIS Content › Assets › Facts: the platform Facts Entries (acp_shared.facts, scope 'platform') that every
// agency's T9 writer may cite as [F:<id>]. An agency's own fact on the same subject (kind, subject, country) replaces
// the platform fact for that agency (services/acp_content_writing/facts.py); agencies keep their own facts in Content
// studio, never here.
//
// API (CIS BFF → api/routers/admin_facts.py):
//   GET    /api/admin/facts?kind=&q=&include_expired=   the platform facts, newest edit first
//   POST   /api/admin/facts                              a new fact
//   PATCH  /api/admin/facts/{id}                         changed fields + expected_updated_at (409 when stale)
//   DELETE /api/admin/facts/{id}
//   GET    /api/admin/facts/{id}/history                 the audit rows (fact.created / updated / deleted)
//
// Who: every staff role reads (lib/rbac.ts PAGE_RULES ALL_STAFF); writes need quality.write (admin), hidden with can()
// and refused by the BFF (API_WRITE_RULES "facts") and the backend. No polling (S240): load on open + Refresh.

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, RefreshCw, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import {
  AdminMain,
  Badge,
  Button,
  DataTable,
  Drawer,
  K,
  Modal,
  PageHeader,
  apiGet,
  useAdminIdentity,
  useFormat,
  useT,
  useToast,
  type ColumnDef,
  type Translator,
} from "../../_kit";
import { can } from "@/lib/rbac";
import {
  EMPTY_FILTERS,
  EMPTY_FORM,
  FACT_KINDS,
  MAX_BODY,
  MAX_COUNTRY,
  MAX_SOURCE,
  MAX_TITLE,
  changedFields,
  createBody,
  factCall,
  formOf,
  formProblem,
  listUrl,
  type Fact,
  type FactFilters,
  type FactForm,
  type FactHistoryRow,
  type FactKind,
  type FactList,
} from "@/lib/facts";
import { Field, InlineError, fieldStyle, noteStyle } from "../aa-content/AaContentEditor";
import { FM } from "./messages";

const LIST_KEY = ["admin-facts"] as const;
type T = Translator<typeof FM.en>;

function kindLabel(t: T, kind: FactKind): string {
  return t(`kind_${kind}` as "kind_price");
}

export default function FactsConsole() {
  const t = useT(FM);
  const { formatDate, formatDateTime } = useFormat();
  const qc = useQueryClient();
  const role = useAdminIdentity().role;
  const canWrite = can(role, "quality.write");
  const [filters, setFilters] = useState<FactFilters>(EMPTY_FILTERS);
  const [open, setOpen] = useState<Fact | null>(null);
  const [creating, setCreating] = useState(false);

  const list = useQuery({
    queryKey: [...LIST_KEY, filters],
    queryFn: () => apiGet<FactList>(listUrl(filters)),
    placeholderData: keepPreviousData,
    retry: false,
  });

  const columns = useMemo<ColumnDef<Fact, unknown>[]>(() => [
    {
      accessorKey: "title", header: t("colSubject"),
      cell: (c) => (
        <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
          <span style={{ fontWeight: 600, color: K.ink, overflowWrap: "anywhere" }}>{c.row.original.title}</span>
          <span style={{ fontSize: 11.5, color: K.muted }}>{c.row.original.country || t("anyCountry")}</span>
        </span>
      ),
    },
    { accessorKey: "kind", header: t("colKind"), cell: (c) => kindLabel(t, c.row.original.kind) },
    {
      accessorKey: "body", header: t("colFact"),
      cell: (c) => (
        <span style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden",
          overflowWrap: "anywhere", maxWidth: 420 }}>{c.row.original.body}</span>
      ),
    },
    {
      accessorKey: "provenance", header: t("colSource"),
      cell: (c) => <span style={{ color: K.ink3, overflowWrap: "anywhere" }}>{c.row.original.provenance}</span>,
    },
    {
      accessorKey: "valid_until", header: t("colValidUntil"),
      cell: (c) => {
        const f = c.row.original;
        if (f.expired) return <Badge tone="warning">{t("expired")}</Badge>;
        return f.valid_until ? formatDate(f.valid_until) : <span style={{ color: K.muted }}>{t("noEnd")}</span>;
      },
    },
    {
      accessorKey: "updated_at", header: t("colUpdated"),
      cell: (c) => (c.row.original.updated_at ? formatDateTime(c.row.original.updated_at) : ""),
    },
  ], [t, formatDate, formatDateTime]);

  const refresh = () => { void qc.invalidateQueries({ queryKey: LIST_KEY }); };
  const listError = list.error ? (list.error as Error).message || t("listError") : null;
  const filtered = !!(filters.kind || filters.q.trim() || !filters.includeExpired);

  const filterBar = (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", minWidth: 0 }}>
      <select aria-label={t("colKind")} value={filters.kind}
        onChange={(e) => setFilters((f) => ({ ...f, kind: e.target.value as FactKind | "" }))}
        style={{ ...fieldStyle, width: "auto", maxWidth: "100%", minHeight: 32, padding: "5px 8px", fontSize: 12.5 }}>
        <option value="">{t("allKinds")}</option>
        {FACT_KINDS.map((k) => <option key={k} value={k}>{kindLabel(t, k)}</option>)}
      </select>
      <input type="search" aria-label={t("search")} placeholder={t("search")} value={filters.q} maxLength={200}
        onChange={(e) => setFilters((f) => ({ ...f, q: e.target.value }))}
        style={{ ...fieldStyle, width: 240, maxWidth: "100%", minHeight: 32, padding: "5px 8px", fontSize: 12.5 }} />
      <label style={{ display: "inline-flex", gap: 6, alignItems: "center", fontSize: 12.5, color: K.ink3 }}>
        <input type="checkbox" checked={filters.includeExpired}
          onChange={(e) => setFilters((f) => ({ ...f, includeExpired: e.target.checked }))} />
        {t("showExpired")}
      </label>
    </div>
  );

  return (
    <AdminMain>
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
                <Plus size={13} /> {t("newFact")}
              </Button>
            )}
          </>
        }
      />

      <DataTable<Fact>
        tableId="admin-facts"
        data={list.data?.facts ?? []}
        columns={columns}
        getRowId={(r) => r.fact_id}
        loading={list.isLoading}
        error={listError}
        onRetry={refresh}
        toolbarExtra={filterBar}
        emptyTitle={filtered ? t("emptyFiltered") : t("emptyNone")}
        emptyDescription={filtered ? t("emptyFilteredDesc") : canWrite ? t("emptyWriter") : t("emptyReader")}
        onRowClick={(r) => setOpen(r)}
        tableMinWidth={860}
      />

      {open && (
        <FactDrawer key={`${open.fact_id}:${open.updated_at}`} fact={open} canWrite={canWrite}
          onClose={() => setOpen(null)}
          onChanged={(next) => { refresh(); setOpen(next); }} />
      )}
      {canWrite && (
        <CreateModal open={creating} onClose={() => setCreating(false)}
          onCreated={() => { setCreating(false); refresh(); }} />
      )}
    </AdminMain>
  );
}

// ── the form (create + edit) ─────────────────────────────────────────────────────────────────────────────────────

function FactFormFields({ form, setForm, disabled, idPrefix }: {
  form: FactForm; setForm: (f: FactForm) => void; disabled: boolean; idPrefix: string;
}) {
  const t = useT(FM);
  const set = <F extends keyof FactForm>(key: F, value: FactForm[F]) => setForm({ ...form, [key]: value });
  const id = (s: string) => `${idPrefix}-${s}`;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <Field label={t("kind")} htmlFor={id("kind")}>
        <select id={id("kind")} value={form.kind} disabled={disabled} style={fieldStyle}
          onChange={(e) => set("kind", e.target.value as FactKind)}>
          {FACT_KINDS.map((k) => <option key={k} value={k}>{kindLabel(t, k)}</option>)}
        </select>
      </Field>
      <Field label={t("subject")} htmlFor={id("title")} hint={t("subjectHint")}>
        <input id={id("title")} type="text" autoComplete="off" maxLength={MAX_TITLE} value={form.title}
          disabled={disabled} onChange={(e) => set("title", e.target.value)} style={fieldStyle} />
      </Field>
      <Field label={t("fact")} htmlFor={id("body")} hint={t("factHint")}>
        <textarea id={id("body")} rows={4} maxLength={MAX_BODY} value={form.body} disabled={disabled}
          onChange={(e) => set("body", e.target.value)} style={{ ...fieldStyle, resize: "vertical" }} />
      </Field>
      <Field label={t("source")} htmlFor={id("provenance")} hint={t("sourceHint")}>
        <input id={id("provenance")} type="text" autoComplete="off" maxLength={MAX_SOURCE} value={form.provenance}
          disabled={disabled} onChange={(e) => set("provenance", e.target.value)} style={fieldStyle} />
      </Field>
      <Field label={t("country")} htmlFor={id("country")} hint={t("countryHint")}>
        <input id={id("country")} type="text" autoComplete="off" maxLength={MAX_COUNTRY} value={form.country}
          disabled={disabled} onChange={(e) => set("country", e.target.value)} style={fieldStyle} />
      </Field>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <div style={{ flex: "1 1 160px", minWidth: 0 }}>
          <Field label={t("statedOn")} htmlFor={id("stated")} hint={t("statedOnHint")}>
            <input id={id("stated")} type="date" value={form.stated_on} disabled={disabled}
              onChange={(e) => set("stated_on", e.target.value)} style={fieldStyle} />
          </Field>
        </div>
        <div style={{ flex: "1 1 160px", minWidth: 0 }}>
          <Field label={t("validUntil")} htmlFor={id("until")} hint={t("validUntilHint")}>
            <input id={id("until")} type="date" value={form.valid_until} disabled={disabled}
              onChange={(e) => set("valid_until", e.target.value)} style={fieldStyle} />
          </Field>
        </div>
      </div>
    </div>
  );
}

function CreateModal({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: () => void }) {
  const t = useT(FM);
  const toast = useToast();
  const [form, setForm] = useState<FactForm>(EMPTY_FORM);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const problem = formProblem(form);

  const close = () => { if (!busy) { setForm(EMPTY_FORM); setError(null); onClose(); } };
  const submit = async () => {
    if (problem || busy) { setError(problem ? t(problem) : null); return; }
    setBusy(true);
    setError(null);
    const r = await factCall<Fact>("POST", "", createBody(form));
    setBusy(false);
    if (!r.ok) { setError(r.message); return; }
    toast.success(t("created"));
    setForm(EMPTY_FORM);
    onCreated();
  };

  return (
    <Modal open={open} onClose={close} title={t("newFact")} width={560}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>{t("cancel")}</Button>
          <Button variant="primary" onClick={() => void submit()} disabled={busy || !!problem}>
            {busy ? t("creating") : t("create")}
          </Button>
        </>
      }>
      <form data-testid="admin-fact-create" onSubmit={(e) => { e.preventDefault(); void submit(); }}
        style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <FactFormFields form={form} setForm={setForm} disabled={busy} idPrefix="fact-new" />
        {problem === "dates" && <div style={noteStyle}>{t("dates")}</div>}
        {error && <InlineError message={error} />}
      </form>
    </Modal>
  );
}

// ── the drawer (edit, delete, history) ───────────────────────────────────────────────────────────────────────────

function FactDrawer({ fact, canWrite, onClose, onChanged }: {
  fact: Fact; canWrite: boolean; onClose: () => void; onChanged: (next: Fact | null) => void;
}) {
  const t = useT(FM);
  const toast = useToast();
  const { formatDateTime } = useFormat();
  const [form, setForm] = useState<FactForm>(() => formOf(fact));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const changes = changedFields(fact, form);
  const dirty = Object.keys(changes).length > 0;
  const problem = formProblem(form);

  const history = useQuery({
    queryKey: [...LIST_KEY, "history", fact.fact_id, fact.updated_at],
    queryFn: () => apiGet<{ history: FactHistoryRow[] }>(`/api/admin/facts/${encodeURIComponent(fact.fact_id)}/history`),
    retry: false,
  });

  const close = () => {
    if (busy) return;
    if (dirty && typeof window !== "undefined" && !window.confirm(t("unsavedClose"))) return;
    onClose();
  };

  const save = async () => {
    if (!dirty) { setError(t("nothingChanged")); return; }
    if (problem) { setError(t(problem)); return; }
    setBusy(true);
    setError(null);
    const r = await factCall<Fact>("PATCH", `/${encodeURIComponent(fact.fact_id)}`,
      { ...changes, expected_updated_at: fact.updated_at });
    setBusy(false);
    if (!r.ok) { setError(r.status === 409 ? t("conflict") : r.message); return; }
    toast.success(t("saved"));
    onChanged(r.body);
  };

  const remove = async () => {
    setBusy(true);
    setError(null);
    const r = await factCall<{ deleted: boolean }>("DELETE", `/${encodeURIComponent(fact.fact_id)}`);
    setBusy(false);
    setConfirmDelete(false);
    if (!r.ok) { setError(r.message); return; }
    toast.success(t("deleted"));
    onChanged(null);
  };

  const actLabel = (a: string) =>
    a === "fact.created" ? t("actCreated") : a === "fact.deleted" ? t("actDeleted") : t("actUpdated");

  return (
    <>
      <Drawer open onClose={close} title={t("editTitle")} width={560}
        footer={canWrite ? (
          <div style={{ display: "flex", gap: 8, justifyContent: "space-between", width: "100%", flexWrap: "wrap" }}>
            <Button variant="danger" onClick={() => setConfirmDelete(true)} disabled={busy}>
              <Trash2 size={13} /> {t("delete")}
            </Button>
            <span style={{ display: "inline-flex", gap: 8 }}>
              <Button variant="ghost" onClick={close} disabled={busy}>{t("cancel")}</Button>
              <Button variant="primary" onClick={() => void save()} disabled={busy || !dirty || !!problem}>
                {busy ? t("saving") : t("save")}
              </Button>
            </span>
          </div>
        ) : undefined}>
        <div data-testid="admin-fact-drawer" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {!canWrite && <div style={noteStyle}>{t("readOnly")}</div>}
          <div style={{ ...noteStyle, display: "flex", gap: 12, flexWrap: "wrap" }}>
            <span>{t("factId")}: <code>{fact.fact_id}</code></span>
            {fact.expired && <Badge tone="warning">{t("expired")}</Badge>}
            {fact.updated_by && <span>{t("lastBy")}: {fact.updated_by}</span>}
          </div>
          <FactFormFields form={form} setForm={setForm} disabled={!canWrite || busy} idPrefix="fact-edit" />
          {problem === "dates" && <div style={noteStyle}>{t("dates")}</div>}
          {error && <InlineError message={error} />}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ fontSize: 12.5, fontWeight: 600, color: K.ink3 }}>{t("history")}</div>
            {history.error ? <InlineError message={t("historyError")} />
              : (history.data?.history ?? []).length === 0
                ? <div style={noteStyle}>{history.isLoading ? t("loading") : t("historyEmpty")}</div>
                : (history.data?.history ?? []).map((h, i) => (
                  <div key={`${h.created_at}-${i}`} style={{ fontSize: 12.5, color: K.ink, lineHeight: 1.5,
                    borderTop: `1px solid ${K.line}`, paddingTop: 6 }}>
                    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                      <Badge tone={h.action === "fact.deleted" ? "danger" : h.action === "fact.created" ? "success" : "info"}
                        dot={false}>{actLabel(h.action)}</Badge>
                      <span style={{ color: K.muted }}>{h.created_at ? formatDateTime(h.created_at) : ""}</span>
                      <span style={{ color: K.muted, overflowWrap: "anywhere" }}>{h.actor}</span>
                    </div>
                    {h.details?.changes && (
                      <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                        {Object.entries(h.details.changes).map(([field, [before, after]]) => (
                          <li key={field} style={{ overflowWrap: "anywhere" }}>
                            <strong>{field}</strong>: {String(before ?? "—")} → {String(after ?? "—")}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                ))}
          </div>
        </div>
      </Drawer>
      <Modal open={confirmDelete} onClose={() => { if (!busy) setConfirmDelete(false); }} title={t("deleteTitle")}
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)} disabled={busy}>{t("cancel")}</Button>
            <Button variant="danger" onClick={() => void remove()} disabled={busy}>
              {busy ? t("deleting") : t("delete")}
            </Button>
          </>
        }>
        <div style={noteStyle}>{t("deleteBody")}</div>
      </Modal>
    </>
  );
}
