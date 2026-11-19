"use client";
// app/admin/ai-gateway/ConnectionBudgetsSection.tsx — AA-1143 (tech lead decision G3): AI gateway › Budgets,
// "Connection budgets". One budget per LLM connection: its total, a fixed share per app, CIS takes what is left (the
// remainder the CIS gateway enforces). Read GET /api/admin/ai-gateway/connection-budgets; edit one connection in a kit
// Drawer (PUT …/connection-budgets/{key}: reason 20..500 characters, confirm first, audited); its versions in a
// second Drawer (GET …/{key}/history); "Send to CRM" sends the CRM its copy again (POST …/crm-sync). Replaces the
// AA-1113 "Connection budget split" table (three values kept in sum by hand). Kit only (Section, DataTable, Drawer,
// Field / Input / Textarea, ConfirmModal, Badge, Button, Toast). ConnectionBudgetSummary is the Connections drawer's
// read-only view with its link here.

import { useMemo, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge, Button, ConfirmModal, DataTable, DateText, Drawer, ErrorState, Field, Input, K, Section, Textarea, apiGet,
  apiSend, mono, useT, useToast, type ColumnDef, type Translator,
} from "../../_kit";
import { BUDGET_ROW_STATE, CONN_BUDGETS as M } from "./connectionBudgets.messages";
import { usePageAccess } from "./usePageAccess";
import { useRefusalText } from "./useRefusalText";
import { usd } from "@/lib/aiGateway";
import {
  BUDGETS_TAB_HREF, CONNECTION_BUDGETS_API, appLabel, budgetOrder, checkDraft, draftOf, rowState,
  type BudgetDraft, type ConnectionBudgetRow, type ConnectionBudgetsResp, type DraftError, type HistoryResp,
} from "@/lib/connectionBudgets";

export const CONNECTION_BUDGETS_KEY = ["ai-gateway", "connection-budgets"] as const;

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const amountStyle = { fontFamily: mono } as const;

export function useConnectionBudgets() {
  return useQuery({
    queryKey: CONNECTION_BUDGETS_KEY,
    queryFn: () => apiGet<ConnectionBudgetsResp>(CONNECTION_BUDGETS_API, { admin: true }),
    refetchInterval: 120_000,
    refetchIntervalInBackground: false,
  });
}

function Amount({ value, empty }: { value: number | null | undefined; empty: string }) {
  return <span style={amountStyle}>{value == null ? empty : usd(value, 2)}</span>;
}

function ShareAmount({ row, app }: { row: ConnectionBudgetRow; app: string }) {
  const t = useT(M);
  const share = row.shares[app];
  if (share != null) return <Amount value={share.usd} empty={t("notSet")} />;
  return <span style={small}>{app === "crm" ? usd(0, 2) : t("inPool")}</span>;
}

function ShareNote({ row, app }: { row: ConnectionBudgetRow; app: string }) {
  const t = useT(M);
  if (app === "crm") {
    if (row.crm_state === "in_sync") return null;
    const money = (v: number | null) => (v == null ? "—" : usd(v, 2));
    return <span style={small}>{t("crmCopy", money(row.crm_copy_usd), money(row.crm_setting_usd))}</span>;
  }
  const spent = row.spend[app] ?? 0;
  return spent > 0 ? <span style={small}>{t("spent", usd(spent, 2))}</span> : null;
}

function ShareCell({ row, app }: { row: ConnectionBudgetRow; app: string }) {
  if (row.total == null) return <span style={small}>—</span>;
  return (
    <div style={{ display: "grid", gap: 2, justifyItems: "end" }}>
      <ShareAmount row={row} app={app} />
      <ShareNote row={row} app={app} />
    </div>
  );
}

function useColumns(apps: string[], onEdit: ((r: ConnectionBudgetRow) => void) | null,
  onHistory: (r: ConnectionBudgetRow) => void) {
  const t = useT(M);
  const ts = useT(BUDGET_ROW_STATE);
  return useMemo<ColumnDef<ConnectionBudgetRow, unknown>[]>(() => [
    { id: "connection", header: t("colConnection"), accessorKey: "connection_key",
      cell: (c) => (
        <div style={{ display: "grid", gap: 2, minWidth: 0 }}>
          <span style={{ fontWeight: 600, color: K.ink }}>{c.row.original.label}</span>
          <span style={{ fontFamily: mono, fontSize: 12, color: K.muted2 }}>{c.row.original.connection_key}</span>
        </div>
      ) },
    { id: "total", header: t("colTotal"), meta: { align: "right" }, accessorFn: (r) => r.total?.usd ?? -1,
      cell: (c) => <Amount value={c.row.original.total?.usd} empty={t("notSet")} /> },
    ...apps.map((app): ColumnDef<ConnectionBudgetRow, unknown> => ({
      id: `share-${app}`, header: t("colShare", appLabel(app)), meta: { align: "right" },
      accessorFn: (r) => r.shares[app]?.usd ?? -1, cell: (c) => <ShareCell row={c.row.original} app={app} />,
    })),
    { id: "cis", header: t("colCis"), meta: { align: "right" }, accessorFn: (r) => r.cis_remainder_usd ?? -1,
      cell: (c) => {
        const r = c.row.original;
        const cap = r.total == null ? r.cis_cap_usd : r.cis_remainder_usd;
        return (
          <div style={{ display: "grid", gap: 2, justifyItems: "end" }}>
            <Amount value={cap} empty={t("notSet")} />
            {cap != null && <span style={small}>{t("poolSpent", usd(r.cis_pool_spent_usd, 2), usd(cap, 2))}</span>}
          </div>
        );
      } },
    { id: "state", header: t("colState"), accessorFn: (r) => rowState(r).key,
      cell: (c) => {
        const s = rowState(c.row.original);
        return <Badge tone={s.tone} dot={false} style={{ whiteSpace: "nowrap" }}>{ts(s.key as keyof typeof BUDGET_ROW_STATE.en)}</Badge>;
      } },
    { id: "actions", header: "", enableSorting: false,
      cell: (c) => (
        <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
          {onEdit && <Button size="sm" variant="ghost" onClick={() => onEdit(c.row.original)}
            testId={`conn-budget-edit-${c.row.original.connection_key}`}>{t("edit")}</Button>}
          {c.row.original.total && <Button size="sm" variant="ghost" onClick={() => onHistory(c.row.original)}
            testId={`conn-budget-history-${c.row.original.connection_key}`}>{t("history")}</Button>}
        </div>
      ) },
  ], [apps, onEdit, onHistory, t, ts]);
}

function errorText(t: Translator<typeof M.en>, e: DraftError | undefined): string | undefined {
  if (!e) return undefined;
  return { amount: t("errAmount"), totalRequired: t("errTotalRequired"), overAllocated: t("errOverAllocated"),
    reason: t("errReason") }[e];
}

function BudgetEditor({ row, apps, tightenOnly, onClose }: {
  row: ConnectionBudgetRow; apps: string[]; tightenOnly: boolean; onClose: () => void;
}) {
  const t = useT(M);
  const toast = useToast();
  const qc = useQueryClient();
  const refusal = useRefusalText();
  const [draft, setDraft] = useState<BudgetDraft>(() => draftOf(row, apps));
  const [confirming, setConfirming] = useState(false);
  const [apiError, setApiError] = useState("");
  const check = checkDraft(row, apps, draft);
  const mut = useMutation({
    mutationFn: () => apiSend<{ crm_sync: { sent: boolean } | null }>(
      `${CONNECTION_BUDGETS_API}/${encodeURIComponent(row.connection_key)}`,
      { method: "PUT", body: check.body, admin: true }),
    onSuccess: (out) => {
      const later = out?.crm_sync && !out.crm_sync.sent;
      (later ? toast.error : toast.success)(t(later ? "savedCrmLater" : "saved", row.label));
      qc.invalidateQueries({ queryKey: CONNECTION_BUDGETS_KEY });
      qc.invalidateQueries({ queryKey: ["ai-gateway", "budgets"] });
      qc.invalidateQueries({ queryKey: ["llm-connections"] });
      onClose();
    },
    onError: (e) => { setConfirming(false); setApiError(refusal(e)); },
  });
  const busy = mut.isPending;
  const set = (p: Partial<BudgetDraft>) => setDraft((d) => ({ ...d, ...p }));
  return (
    <Drawer open onClose={onClose} title={t("editTitle", row.label)} width={520}
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
          <Button variant="primary" disabled={!check.body || busy} onClick={() => setConfirming(true)}
            testId="conn-budget-save">{busy ? t("saving") : t("save")}</Button>
        </div>
      }>
      <div data-testid="conn-budget-editor" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {tightenOnly && <p role="note" style={{ ...small, margin: 0 }}>{t("tightenOnly")}</p>}
        <Field label={t("fieldTotal")} hint={t("fieldTotalHint")} error={errorText(t, check.errors.total)}>
          <Input inputMode="decimal" value={draft.total} disabled={busy} onChange={(e) => set({ total: e.target.value })}
            data-testid="conn-budget-total" />
        </Field>
        {apps.map((app) => (
          <Field key={app} label={t("fieldShare", appLabel(app))} hint={t("fieldShareHint")}
            error={errorText(t, check.errors[app])}>
            <Input inputMode="decimal" value={draft.shares[app] ?? ""} disabled={busy}
              onChange={(e) => set({ shares: { ...draft.shares, [app]: e.target.value } })}
              data-testid={`conn-budget-share-${app}`} />
          </Field>
        ))}
        <span role="status" data-testid="conn-budget-remainder" style={{ fontWeight: 600, color: K.ink }}>
          {check.remainder == null ? "—" : t("remainder", usd(check.remainder, 2))}
        </span>
        <Field label={t("fieldReason")} hint={t("fieldReasonHint")} error={errorText(t, check.errors.reason)}>
          <Textarea value={draft.reason} rows={2} disabled={busy} onChange={(e) => set({ reason: e.target.value })}
            data-testid="conn-budget-reason" />
        </Field>
        {apiError && <span data-testid="conn-budget-error" style={{ fontSize: 12, color: K.danger }}>{apiError}</span>}
      </div>
      <ConfirmModal open={confirming} onClose={() => setConfirming(false)} onConfirm={() => mut.mutate()}
        title={t("confirmTitle", row.label)} body={t("confirmBody")} confirmLabel={t("save")} busy={busy}
        testId="conn-budget-confirm" />
    </Drawer>
  );
}

function BudgetHistory({ row, onClose }: { row: ConnectionBudgetRow; onClose: () => void }) {
  const t = useT(M);
  const q = useQuery({
    queryKey: [...CONNECTION_BUDGETS_KEY, "history", row.connection_key],
    queryFn: () => apiGet<HistoryResp>(`${CONNECTION_BUDGETS_API}/${encodeURIComponent(row.connection_key)}/history`,
      { admin: true }),
  });
  return (
    <Drawer open onClose={onClose} title={t("historyTitle", row.label)} width={520}>
      {q.error ? <ErrorState message={(q.error as Error).message} onRetry={() => q.refetch()} /> : (
        <ol data-testid="conn-budget-history" style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 10 }}>
          {(q.data?.versions ?? []).map((v) => (
            <li key={`${v.app ?? "total"}-${v.version}`} style={{ display: "grid", gap: 2 }}>
              <span style={{ fontWeight: 600, color: K.ink }}>
                {v.app == null ? t("historyTotal") : appLabel(v.app)} · v{v.version} ·{" "}
                {v.usd == null ? t("historyRemoved") : usd(v.usd, 2)}
              </span>
              <span style={small}><DateText value={v.valid_from} relative /> · {v.actor}</span>
              <span style={small}>{v.reason}</span>
            </li>
          ))}
        </ol>
      )}
    </Drawer>
  );
}

function CrmSyncButton() {
  const t = useT(M);
  const toast = useToast();
  const qc = useQueryClient();
  const refusal = useRefusalText();
  const mut = useMutation({
    mutationFn: () => apiSend(`${CONNECTION_BUDGETS_API}/crm-sync`, { method: "POST", admin: true }),
    onSuccess: () => { toast.success(t("synced")); qc.invalidateQueries({ queryKey: CONNECTION_BUDGETS_KEY }); },
    onError: (e) => toast.error(t("syncFailed", refusal(e))),
  });
  return (
    <Button size="sm" variant="ghost" disabled={mut.isPending} onClick={() => mut.mutate()}
      testId="conn-budget-crm-sync">{mut.isPending ? t("syncing") : t("syncCrm")}</Button>
  );
}

function Notices({ data, canEdit }: { data: ConnectionBudgetsResp | undefined; canEdit: boolean }) {
  const t = useT(M);
  const crmError = data?.crm?.copy_error;
  const line = { ...small, padding: "8px 16px", margin: 0 } as const;
  return (
    <>
      {!canEdit && data && <p role="status" style={line}>{t("readOnly")}</p>}
      {crmError && <p role="status" style={{ ...line, color: K.amber }}>{t("crmError", crmError)}</p>}
    </>
  );
}

function BudgetsTable({ data, canEdit, tightenOnly }: {
  data: ConnectionBudgetsResp | undefined; canEdit: boolean; tightenOnly: boolean;
}) {
  const t = useT(M);
  const [editing, setEditing] = useState<ConnectionBudgetRow | null>(null);
  const [viewing, setViewing] = useState<ConnectionBudgetRow | null>(null);
  const apps = data?.apps ?? [];
  const columns = useColumns(apps, canEdit ? setEditing : null, setViewing);
  return (
    <>
      <Notices data={data} canEdit={canEdit} />
      <DataTable<ConnectionBudgetRow>
        tableId={SECTION_ID} data={data ? budgetOrder(data.rows) : []} columns={columns}
        getRowId={(r) => r.connection_key} loading={!data} searchable={false} paginate={false} stickyHeader={false}
        tableMinWidth={860} emptyTitle={t("empty")}
      />
      {editing && <BudgetEditor row={editing} apps={apps} tightenOnly={tightenOnly} onClose={() => setEditing(null)} />}
      {viewing && <BudgetHistory row={viewing} onClose={() => setViewing(null)} />}
    </>
  );
}

const SECTION_ID = "ai-gateway-budgets-connection-budgets";

export default function ConnectionBudgetsSection() {
  const t = useT(M);
  const access = usePageAccess();
  const q = useConnectionBudgets();
  const data = q.data;
  const canEdit = access.can("connection_budgets");
  if (q.error || (data && !data.ready)) {
    return (
      <Section title={t("title")} description={t("help")} testId={SECTION_ID}>
        {q.error
          ? <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} onRetry={() => q.refetch()} />
          : <p style={small}>{t("notReady")}</p>}
      </Section>
    );
  }
  const withCrm = canEdit && (data?.apps ?? []).includes("crm");
  return (
    <Section title={t("title")} description={t("help")} padded={false} testId={SECTION_ID}
      actions={withCrm ? <CrmSyncButton /> : undefined}>
      <BudgetsTable data={data} canEdit={canEdit} tightenOnly={access.tightenOnly} />
    </Section>
  );
}

/** The Connections drawer's read-only view of one connection's budget, with its link to Budgets. */
export function ConnectionBudgetSummary({ connectionKey }: { connectionKey: string }) {
  const t = useT(M);
  const q = useConnectionBudgets();
  const row = q.data?.rows.find((r) => r.connection_key === connectionKey);
  const apps = q.data?.apps ?? [];
  return (
    <div data-testid="conn-budget-summary" style={{ display: "grid", gap: 6 }}>
      <p style={{ ...small, margin: 0 }}>{t("drawerIntro")} <Link href={BUDGETS_TAB_HREF}>{t("drawerOpen")}</Link></p>
      {row && row.total == null && <p style={{ ...small, margin: 0 }}>{t("drawerNone")}</p>}
      {row && row.total != null && (
        <dl style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "2px 12px", margin: 0, fontSize: 13 }}>
          <dt style={small}>{t("colTotal")}</dt><dd style={{ margin: 0 }}><Amount value={row.total.usd} empty="—" /></dd>
          {apps.map((app) => (
            <Pair key={app} label={t("colShare", appLabel(app))}
              value={row.shares[app]?.usd ?? null} empty={app === "crm" ? usd(0, 2) : t("inPool")} />
          ))}
          <Pair label={t("colCis")} value={row.cis_remainder_usd} empty="—" />
        </dl>
      )}
    </div>
  );
}

function Pair({ label, value, empty }: { label: string; value: number | null; empty: string }) {
  return (
    <>
      <dt style={small}>{label}</dt>
      <dd style={{ margin: 0 }}><Amount value={value} empty={empty} /></dd>
    </>
  );
}
