"use client";
// app/admin/ai-gateway/BudgetsTab.tsx — AA-804 UI part 2 / AA-1017: the Budgets tab of AI providers (replaces Cost
// Operations › Budgets, whose old link is sent on here).
//
// One read, GET /api/admin/ai-gateway/budgets (backend api/services/ai_gateway.py read_budgets): every cap by tier.
//   Provider daily caps + Job caps   shared.spend_budget, edited in a kit Drawer through the existing
//                                    PUT /api/admin/budgets/{provider}/{scope} (spend.write; confirm first).
//   LLM connection budgets           read here; edited with the connection's settings (Providers › Connections).
//   Tenant AI budgets                shared.tenant_ai_budget (AA-1016, migration 321): each tenant's monthly budget and
//                                    this month's spend; set on the tenant (Tenants › the tenant › Cost). Before 321
//                                    (tenant_ready false or absent) the designed empty state.
//   Coverage                         does every paid provider and switched-on connection have a cap.
//   Central LLM caps (AA-1025 A7)    spend_budget provider "llm" (CIS migration 340): app / stage / job kind / eval /
//                                    ad-hoc caps checked by the gateway before each call; edited in their own Drawer
//                                    (PUT / DELETE /api/admin/budgets/llm/{scope}, spend.write, confirm first). New
//                                    caps start alert only (tech lead T-8). Coverage per call site: which central cap
//                                    each CIS stage is under.
// C4 live findings: "Last changed" names a person, never a raw id (F5, lib/aiGateway actorLabel); state badges do not
// wrap (F5); a connection without a budget says "no cap set" instead of "Stops at the cap · 80 %" (F6).
// Built on the kit only (KpiTile, Section, DataTable, Drawer, Field/Input/Select, ConfirmModal, Badge, Toast).

import { useCallback, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Wallet } from "lucide-react";
import {
  Badge, Button, ConfirmModal, DataTable, DateText, Drawer, EmptyState, ErrorState, Field, Input, K, KpiTile, Section,
  Select, apiGet, apiSend, mono, useT, useToast, type ColumnDef,
} from "../../_kit";
import {
  BUDGETS_API, CENTRAL_SCOPE_KINDS, actorLabel, budgetBody, budgetDraftOf, budgetProviderLabel, budgetPutPath,
  budgetTone, centralBody, centralDraftOf, centralPath, isReadOnlyRefusal, pct, sameCentralDraft, sameDraft,
  tenantBudgetTone, tenantCostHref, usd, validScope, type BudgetDraft, type BudgetError, type BudgetsResp,
  type CallSiteRow, type CentralBudgetRow, type CentralDraft, type CentralError, type CentralScopeKind,
  type ConnectionBudgetRow, type CoverageRow, type SpendBudgetRow, type TenantBudgetRow,
} from "@/lib/aiGateway";
import { connectionName } from "@/lib/llmConnections";
import { useAccountText, useAwsAccounts } from "../_components/useAwsAccounts";
import { BUDGETS, BUDGET_STATE, CENTRAL } from "./budgets.messages";
import { usePageAccess } from "./usePageAccess";

export const BUDGETS_KEY = ["ai-gateway", "budgets"] as const;

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;

function Lines({ children }: { children: React.ReactNode }) {
  return <div style={{ display: "grid", gap: 2, minWidth: 0 }}>{children}</div>;
}

function StateBadge({ state, tone }: { state: string; tone?: ReturnType<typeof budgetTone> }) {
  const ts = useT(BUDGET_STATE);
  const known = state in BUDGET_STATE.en;
  // whiteSpace nowrap: a two-word state in a narrow column wrapped into a round blob (live finding F5).
  return (
    <Badge tone={tone ?? budgetTone(state)} dot={false} style={{ whiteSpace: "nowrap" }}>
      {known ? ts(state as keyof typeof BUDGET_STATE.en) : state}
    </Badge>
  );
}

/** "Last changed": who (a person's name, "Migration NNN", never a raw id) and when. */
function ChangedCell({ row }: {
  row: Pick<SpendBudgetRow, "updated_at" | "updated_by" | "updated_by_kind" | "updated_by_name">;
}) {
  const t = useT(BUDGETS);
  const who = actorLabel(row, t("staffMember"), (ref) => t("staffLogin", ref));
  return (
    <Lines>
      <span style={small}>{who ?? "—"}</span>
      {row.updated_at && <span style={small}><DateText value={row.updated_at} relative /></span>}
    </Lines>
  );
}

// ── the editor (one spend_budget row; new when `row` is null) ──────────────────────────────────────────────────────

interface Editing {
  row: SpendBudgetRow | null;
}

function BudgetEditor({ editing, providers, existing, onClose }: {
  editing: Editing; providers: string[]; existing: Set<string>; onClose: () => void;
}) {
  const t = useT(BUDGETS);
  const toast = useToast();
  const qc = useQueryClient();
  const row = editing.row;
  const start = useMemo<BudgetDraft>(
    () => row ? budgetDraftOf(row) : { per_run: "", per_day: "", hard_stop: false, alert_pct: "80" }, [row]);
  const [draft, setDraft] = useState<BudgetDraft>(start);
  const [provider, setProvider] = useState(row?.provider ?? providers[0] ?? "dfs");
  const [scope, setScope] = useState(row?.scope ?? "global");
  const [confirming, setConfirming] = useState(false);
  const [apiError, setApiError] = useState("");

  const { body, error } = budgetBody(draft);
  const scopeOk = validScope(scope);
  const key = `${provider}/${scope.trim()}`;
  const duplicate = !row && existing.has(key);
  const changed = !row || !sameDraft(draft, start);
  const name = `${budgetProviderLabel(provider)} · ${scope.trim() === "global" ? t("scopeGlobal") : scope.trim()}`;
  const errText = (e: BudgetError | undefined) =>
    e === "cap" ? t("errCap") : e === "alertPct" ? t("errAlertPct") : undefined;

  const mut = useMutation({
    mutationFn: () => apiSend(budgetPutPath(provider, scope.trim()), { method: "PUT", body, admin: true }),
    onSuccess: () => {
      toast.success(t("saved", name));
      qc.invalidateQueries({ queryKey: BUDGETS_KEY });
      qc.invalidateQueries({ queryKey: ["ai-gateway", "providers"] });
      onClose();
    },
    onError: (e) => {
      setConfirming(false);
      setApiError(isReadOnlyRefusal(e) ? t("readOnlySession") : e instanceof Error ? e.message : String(e));
    },
  });

  const set = (p: Partial<BudgetDraft>) => setDraft((d) => ({ ...d, ...p }));
  const blocked = !body || !scopeOk || duplicate || !changed || mut.isPending;

  return (
    <Drawer open onClose={onClose} title={row ? t("editTitle", name) : t("addTitle")} width={520}
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={onClose} disabled={mut.isPending}>{t("cancel")}</Button>
          <Button variant="primary" disabled={blocked} onClick={() => setConfirming(true)} testId="budget-save">
            {mut.isPending ? t("saving") : t("save")}
          </Button>
        </div>
      }>
      <div data-testid="budget-editor" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {row ? null : (
          <>
            <Field label={t("fieldProvider")}>
              <Select value={provider} onChange={(e) => setProvider(e.target.value)} disabled={mut.isPending}
                options={providers.map((p) => ({ value: p, label: budgetProviderLabel(p) }))} />
            </Field>
            <Field label={t("fieldScope")} hint={t("fieldScopeHint")}
              error={!scopeOk ? t("errScope") : duplicate ? t("errExists") : undefined}>
              <Input value={scope} onChange={(e) => setScope(e.target.value)} disabled={mut.isPending}
                spellCheck={false} autoComplete="off" style={{ fontFamily: mono }} />
            </Field>
          </>
        )}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 12 }}>
          <Field label={t("fieldPerRun")} hint={t("fieldCapHint")} error={cap(draft.per_run) ? undefined : errText("cap")}>
            <Input inputMode="decimal" value={draft.per_run} placeholder={t("noCap")} disabled={mut.isPending}
              onChange={(e) => set({ per_run: e.target.value })} data-testid="budget-per-run" />
          </Field>
          <Field label={t("fieldPerDay")} hint={t("fieldCapHint")} error={cap(draft.per_day) ? undefined : errText("cap")}>
            <Input inputMode="decimal" value={draft.per_day} placeholder={t("noCap")} disabled={mut.isPending}
              onChange={(e) => set({ per_day: e.target.value })} data-testid="budget-per-day" />
          </Field>
          <Field label={t("fieldAtCap")}>
            <Select value={draft.hard_stop ? "stop" : "alert"} disabled={mut.isPending}
              onChange={(e) => set({ hard_stop: e.target.value === "stop" })}
              options={[{ value: "stop", label: t("stopsAtCap") }, { value: "alert", label: t("alertOnly") }]} />
          </Field>
          <Field label={t("fieldAlertPct")} error={error === "alertPct" ? errText("alertPct") : undefined}>
            <Input inputMode="numeric" value={draft.alert_pct} disabled={mut.isPending}
              onChange={(e) => set({ alert_pct: e.target.value })} />
          </Field>
        </div>
        {apiError && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{apiError}</span>}
      </div>
      <ConfirmModal open={confirming} onClose={() => setConfirming(false)} onConfirm={() => mut.mutate()}
        title={t("confirmTitle", name)} body={t("confirmBody")} confirmLabel={t("save")} busy={mut.isPending}
        testId="budget-confirm" />
    </Drawer>
  );
}

/** A cap field is fine when empty or a number >= 0 (same rule as lib/aiGateway budgetBody). */
function cap(v: string): boolean {
  const s = v.trim();
  return s === "" || (Number.isFinite(Number(s)) && Number(s) >= 0);
}

// ── tables ─────────────────────────────────────────────────────────────────────────────────────────────────────────

function SpendBudgetTable({ id, rows, loading, onEdit }: {
  id: string; rows: SpendBudgetRow[]; loading: boolean; onEdit?: (r: SpendBudgetRow) => void;
}) {
  const t = useT(BUDGETS);
  const columns = useMemo<ColumnDef<SpendBudgetRow, unknown>[]>(() => [
    { id: "provider", header: t("colProvider"), accessorFn: (r) => budgetProviderLabel(r.provider),
      cell: (c) => <span style={{ fontWeight: 600, color: K.ink }}>{budgetProviderLabel(c.row.original.provider)}</span> },
    { id: "scope", header: t("colScope"), accessorKey: "scope",
      cell: (c) => <span>{c.row.original.job_kind ? t("scopeJob", c.row.original.job_kind) : t("scopeGlobal")}</span> },
    { id: "per_run", header: t("colPerRun"), accessorFn: (r) => r.per_run_usd ?? -1, meta: { align: "right" },
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.per_run_usd == null ? t("noCap") : usd(c.row.original.per_run_usd, 2)}</span> },
    { id: "per_day", header: t("colPerDay"), accessorFn: (r) => r.per_day_usd ?? -1, meta: { align: "right" },
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.per_day_usd == null ? t("noCap") : usd(c.row.original.per_day_usd, 2)}</span> },
    { id: "at_cap", header: t("colAtCap"), accessorKey: "hard_stop",
      cell: (c) => (
        <Badge tone={c.row.original.hard_stop ? "danger" : "warning"} dot={false} style={{ whiteSpace: "nowrap" }}>
          {c.row.original.hard_stop ? t("stopsAtCap") : t("alertOnly")}
        </Badge>
      ) },
    { id: "alert", header: t("colAlertAt"), accessorKey: "alert_pct", meta: { align: "right" },
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.alert_pct ?? "—"} %</span> },
    { id: "spent", header: t("colSpentToday"), accessorKey: "spent_today_usd", meta: { align: "right" },
      cell: (c) => (
        <Lines>
          <span style={{ fontFamily: mono }}>{usd(c.row.original.spent_today_usd)}</span>
          {c.row.original.used_pct != null && <span style={small}>{t("ofCap", pct(c.row.original.used_pct))}</span>}
          {c.row.original.job_kind && (
            <span style={small} data-testid={`budget-day-scope-${c.row.original.provider}-${c.row.original.scope}`}>
              {c.row.original.day_scope === "kind" ? t("spentKindOnly") : t("spentProviderDay")}
            </span>
          )}
        </Lines>
      ) },
    { id: "state", header: t("colState"), accessorKey: "state", cell: (c) => <StateBadge state={c.row.original.state} /> },
    { id: "changed", header: t("colChanged"), accessorFn: (r) => r.updated_at ?? "",
      cell: (c) => <ChangedCell row={c.row.original} /> },
    { id: "actions", header: "", enableSorting: false, enableHiding: false,
      cell: (c) => onEdit ? (
        <span onClick={(e) => e.stopPropagation()}>
          <Button size="sm" variant="secondary" onClick={() => onEdit(c.row.original)}
            testId={`budget-edit-${c.row.original.provider}-${c.row.original.scope}`}>{t("edit")}</Button>
        </span>
      ) : null },
  ], [t, onEdit]);
  return (
    <DataTable<SpendBudgetRow>
      tableId={id} data={rows} columns={columns} getRowId={(r) => `${r.provider}/${r.scope}`} loading={loading}
      onRowClick={onEdit} searchable={false} paginate={false} stickyHeader={false} tableMinWidth={1040}
      emptyTitle={t("emptyRows")}
    />
  );
}

function ConnectionBudgetTable({ rows, loading }: { rows: ConnectionBudgetRow[]; loading: boolean }) {
  const t = useT(BUDGETS);
  const dir = useAwsAccounts();
  const accountText = useAccountText();
  const columns = useMemo<ColumnDef<ConnectionBudgetRow, unknown>[]>(() => [
    { id: "connection", header: t("colConnection"), accessorKey: "label",
      cell: (c) => {
        const r = c.row.original;
        return (
          <Lines>
            <span style={{ fontWeight: 600, color: K.ink }}>{r.label}</span>
            <span style={{ fontFamily: mono, fontSize: 12, color: K.muted2 }}>
              {connectionName({ connection_key: r.connection_key, kind: r.kind ?? "", account_id: r.account_id },
                dir, accountText)}
            </span>
            {!r.enabled && <span style={small}>{t("off")}</span>}
          </Lines>
        );
      } },
    { id: "monthly", header: t("colMonthly"), accessorFn: (r) => r.monthly_budget_usd ?? -1, meta: { align: "right" },
      cell: (c) => (
        <span style={{ fontFamily: mono }}>
          {c.row.original.monthly_budget_usd == null ? t("noCap") : usd(c.row.original.monthly_budget_usd, 2)}
        </span>
      ) },
    { id: "spent", header: t("colMonthSpent"), accessorKey: "month_spent_usd", meta: { align: "right" },
      cell: (c) => (
        <Lines>
          <span style={{ fontFamily: mono }}>{usd(c.row.original.month_spent_usd)}</span>
          <span style={small}>
            {t("calls", c.row.original.month_calls)}
            {c.row.original.used_pct != null ? ` · ${t("ofCap", pct(c.row.original.used_pct))}` : ""}
          </span>
        </Lines>
      ) },
    { id: "at_cap", header: t("colAtCap"), accessorKey: "hard_stop",
      // F6: a connection without a budget has nothing to stop at or alert on.
      cell: (c) => c.row.original.monthly_budget_usd == null
        ? <span style={small}>{t("noCapSet")}</span>
        : <span style={small}>{c.row.original.hard_stop ? t("stopsAtCap") : t("alertOnly")} · {c.row.original.alert_pct} %</span> },
    { id: "state", header: t("colState"), accessorKey: "state", cell: (c) => <StateBadge state={c.row.original.state} /> },
  ], [t, dir, accountText]);
  return (
    <DataTable<ConnectionBudgetRow>
      tableId="ai-gateway-budgets-connections" data={rows} columns={columns} getRowId={(r) => r.connection_key}
      loading={loading} searchable={false} paginate={false} stickyHeader={false} tableMinWidth={760}
      emptyTitle={t("emptyRows")}
    />
  );
}

function CoverageTable({ rows, loading }: { rows: CoverageRow[]; loading: boolean }) {
  const t = useT(BUDGETS);
  const dir = useAwsAccounts();
  const accountText = useAccountText();
  const columns = useMemo<ColumnDef<CoverageRow, unknown>[]>(() => [
    { id: "source", header: t("colSource"), accessorKey: "key",
      cell: (c) => {
        const r = c.row.original;
        if (r.tier === "provider") return <span style={{ fontWeight: 600, color: K.ink }}>{budgetProviderLabel(r.key)}</span>;
        return (
          <Lines>
            <span style={{ fontWeight: 600, color: K.ink }}>{r.label ?? r.key}</span>
            <span style={{ fontFamily: mono, fontSize: 12, color: K.muted2 }}>
              {connectionName({ connection_key: r.key, kind: r.kind ?? "", account_id: r.account_id ?? null },
                dir, accountText)}
            </span>
          </Lines>
        );
      } },
    { id: "tier", header: t("colTier"), accessorKey: "tier",
      cell: (c) => <span>{c.row.original.tier === "provider" ? t("tierProvider") : t("tierConnection")}</span> },
    { id: "capped", header: t("colCapped"), accessorKey: "capped",
      cell: (c) => (
        <Badge tone={c.row.original.capped ? "success" : "warning"} dot={false}>
          {c.row.original.capped ? t("yes") : t("no")}
        </Badge>
      ) },
    { id: "at_cap", header: t("colAtCap"), enableSorting: false,
      cell: (c) => (
        <span style={small}>
          {c.row.original.hard_stop == null ? "—" : c.row.original.hard_stop ? t("stopsAtCap") : t("alertOnly")}
        </span>
      ) },
  ], [t, dir, accountText]);
  return (
    <DataTable<CoverageRow>
      tableId="ai-gateway-budgets-coverage" data={rows} columns={columns} getRowId={(r) => `${r.tier}:${r.key}`}
      loading={loading} searchable={false} paginate={false} stickyHeader={false} tableMinWidth={560}
      emptyTitle={t("emptyRows")}
    />
  );
}

function TenantBudgetTable({ rows, loading }: { rows: TenantBudgetRow[]; loading: boolean }) {
  const t = useT(BUDGETS);
  const columns = useMemo<ColumnDef<TenantBudgetRow, unknown>[]>(() => [
    { id: "tenant", header: t("colTenant"), accessorFn: (r) => r.tenant_name ?? r.tenant_id,
      cell: (c) => (
        <Lines>
          <span style={{ fontWeight: 600, color: K.ink }}>{c.row.original.tenant_name ?? "—"}</span>
          <a href={tenantCostHref(c.row.original.tenant_id)} style={{ fontSize: 12, color: K.accent }}>
            {t("openTenant")}</a>
        </Lines>
      ) },
    { id: "monthly", header: t("colMonthly"), accessorFn: (r) => r.monthly_usd ?? -1, meta: { align: "right" },
      cell: (c) => <span style={{ fontFamily: mono }}>
        {c.row.original.monthly_usd == null ? t("noCap") : usd(c.row.original.monthly_usd, 2)}</span> },
    { id: "spent", header: t("colMonthSpent"), accessorKey: "month_spent_usd", meta: { align: "right" },
      cell: (c) => (
        <Lines>
          <span style={{ fontFamily: mono }}>{usd(c.row.original.month_spent_usd)}</span>
          <span style={small}>
            {t("calls", c.row.original.month_calls)}
            {c.row.original.used_pct != null ? ` · ${t("ofCap", pct(c.row.original.used_pct))}` : ""}
          </span>
        </Lines>
      ) },
    { id: "levels", header: t("colLevels"), enableSorting: false,
      cell: (c) => c.row.original.monthly_usd == null ? <span style={small}>—</span> : (
        <Lines>
          <span style={small}>{t("levels", c.row.original.alert_pct, c.row.original.approval_pct)}</span>
          <span style={small}>{c.row.original.hard_stop ? t("holdsJobs") : t("alertsOnly")}</span>
        </Lines>
      ) },
    { id: "state", header: t("colState"), accessorKey: "state",
      cell: (c) => <StateBadge state={c.row.original.state} tone={tenantBudgetTone(c.row.original.state)} /> },
    { id: "changed", header: t("colChanged"), accessorFn: (r) => r.updated_at ?? "",
      cell: (c) => <ChangedCell row={c.row.original} /> },
  ], [t]);
  return (
    <DataTable<TenantBudgetRow>
      tableId="ai-gateway-budgets-tenant" data={rows} columns={columns} getRowId={(r) => r.tenant_id}
      loading={loading} searchable={false} paginate={false} stickyHeader={false} tableMinWidth={820}
      emptyTitle={t("tenantNoneTitle")} emptyDescription={t("tenantNoneBody")}
    />
  );
}

// ── AA-1025 A7: central LLM caps ───────────────────────────────────────────────────────────────────────────────────

const KIND_LABEL = {
  app: "kindApp", stage: "kindStage", job: "kindJob", eval: "kindEval", adhoc: "kindAdhoc",
} as const satisfies Record<CentralScopeKind, keyof typeof CENTRAL.en>;

/** The label of a scope's kind and target ("Stage: t9_write"). */
function useScopeLabel() {
  const tc = useT(CENTRAL);
  return useCallback((kind: CentralScopeKind, target: string | null) =>
    kind === "app" ? tc("scopeApp", target ?? "") : kind === "stage" ? tc("scopeStage", target ?? "")
      : kind === "job" ? tc("scopeJob", target ?? "") : kind === "eval" ? tc("scopeEval") : tc("scopeAdhoc"), [tc]);
}

/** "app:cis" -> its kind and target; null for a scope the page does not know (shown as sent). */
function scopeParts(scope: string): [CentralScopeKind, string | null] | null {
  const [k, ...rest] = scope.split(":");
  if (!(CENTRAL_SCOPE_KINDS as readonly string[]).includes(k)) return null;
  return [k as CentralScopeKind, rest.length ? rest.join(":") : null];
}

const grid = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 12 } as const;

function CentralEditor({ row, existing, onClose }: {
  row: CentralBudgetRow | null; existing: Set<string>; onClose: () => void;
}) {
  const t = useT(BUDGETS);
  const tc = useT(CENTRAL);
  const toast = useToast();
  const qc = useQueryClient();
  const start = useMemo<CentralDraft>(() => centralDraftOf(row), [row]);
  const [draft, setDraft] = useState<CentralDraft>(start);
  const [confirming, setConfirming] = useState<"save" | "remove" | null>(null);
  const [apiError, setApiError] = useState("");
  const { scope, body, error } = centralBody(draft);
  const duplicate = !row && !!scope && existing.has(scope);
  const changed = !row || !sameCentralDraft(draft, start);
  const name = row?.scope ?? scope ?? "";
  const errText = (e: CentralError | undefined) =>
    e === "cap" ? t("errCap") : e === "alertPct" ? t("errAlertPct") : e === "scope" ? tc("errScope")
      : e === "runScope" ? tc("errRunScope") : e === "noCap" ? tc("errNoCap") : undefined;
  const done = (msg: string) => {
    toast.success(msg);
    qc.invalidateQueries({ queryKey: BUDGETS_KEY });
    onClose();
  };
  const fail = (e: unknown) => {
    setConfirming(null);
    setApiError(isReadOnlyRefusal(e) ? t("readOnlySession") : e instanceof Error ? e.message : String(e));
  };
  const save = useMutation({
    mutationFn: () => apiSend(centralPath(scope ?? ""), { method: "PUT", body, admin: true }),
    onSuccess: () => done(t("saved", name)),
    onError: fail,
  });
  const remove = useMutation({
    mutationFn: () => apiSend(centralPath(row?.scope ?? ""), { method: "DELETE", admin: true }),
    onSuccess: () => done(tc("removed", name)),
    onError: fail,
  });
  const busy = save.isPending || remove.isPending;
  const set = (p: Partial<CentralDraft>) => setDraft((d) => ({ ...d, ...p }));
  const needsTarget = draft.kind === "app" || draft.kind === "stage" || draft.kind === "job";
  // A scope problem is shown under the name field once something is typed; the other problems below the fields.
  const scopeErr = needsTarget && draft.target.trim() && error === "scope" ? tc("errScope")
    : duplicate ? tc("errExists") : undefined;
  const otherErr = error && error !== "scope" ? errText(error) : undefined;
  const blocked = !body || duplicate || !changed || busy;

  return (
    <Drawer open onClose={onClose} title={row ? tc("editTitle", row.scope) : tc("addTitle")} width={520}
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "space-between", flexWrap: "wrap" }}>
          <span>
            {row && (
              <Button variant="ghost" onClick={() => setConfirming("remove")} disabled={busy}
                testId="central-remove">{tc("remove")}</Button>
            )}
          </span>
          <span style={{ display: "flex", gap: 8 }}>
            <Button variant="ghost" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
            <Button variant="primary" disabled={blocked} onClick={() => setConfirming("save")} testId="central-save">
              {save.isPending ? t("saving") : t("save")}
            </Button>
          </span>
        </div>
      }>
      <div data-testid="central-editor" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {row ? null : (
          <div style={grid}>
            <Field label={tc("fieldKind")}>
              <Select value={draft.kind} disabled={busy}
                onChange={(e) => set({ kind: e.target.value as CentralScopeKind })}
                options={CENTRAL_SCOPE_KINDS.map((k) => ({ value: k, label: tc(KIND_LABEL[k]) }))} />
            </Field>
            {needsTarget && (
              <Field label={tc("fieldTarget")} hint={tc("fieldTargetHint")} error={scopeErr}>
                <Input value={draft.target} onChange={(e) => set({ target: e.target.value })} disabled={busy}
                  spellCheck={false} autoComplete="off" style={{ fontFamily: mono }} data-testid="central-target" />
              </Field>
            )}
          </div>
        )}
        <div style={grid}>
          {draft.kind === "job" && (
            <Field label={tc("fieldPerRun")} hint={tc("fieldPerRunHint")}>
              <Input inputMode="decimal" value={draft.per_run} placeholder={t("noCap")} disabled={busy}
                onChange={(e) => set({ per_run: e.target.value })} />
            </Field>
          )}
          <Field label={t("fieldPerDay")} hint={t("fieldCapHint")}>
            <Input inputMode="decimal" value={draft.per_day} placeholder={t("noCap")} disabled={busy}
              onChange={(e) => set({ per_day: e.target.value })} data-testid="central-per-day" />
          </Field>
          <Field label={tc("fieldPerMonth")} hint={t("fieldCapHint")}>
            <Input inputMode="decimal" value={draft.per_month} placeholder={t("noCap")} disabled={busy}
              onChange={(e) => set({ per_month: e.target.value })} data-testid="central-per-month" />
          </Field>
          <Field label={t("fieldAtCap")}>
            <Select value={draft.hard_stop ? "stop" : "alert"} disabled={busy}
              onChange={(e) => set({ hard_stop: e.target.value === "stop" })}
              options={[{ value: "alert", label: t("alertOnly") }, { value: "stop", label: t("stopsAtCap") }]} />
          </Field>
          <Field label={t("fieldAlertPct")}>
            <Input inputMode="numeric" value={draft.alert_pct} disabled={busy}
              onChange={(e) => set({ alert_pct: e.target.value })} />
          </Field>
        </div>
        {otherErr && changed && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{otherErr}</span>}
        {apiError && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{apiError}</span>}
      </div>
      <ConfirmModal open={confirming === "save"} onClose={() => setConfirming(null)} onConfirm={() => save.mutate()}
        title={t("confirmTitle", name)} body={tc("confirmBody")} confirmLabel={t("save")} busy={save.isPending}
        testId="central-confirm" />
      <ConfirmModal open={confirming === "remove"} onClose={() => setConfirming(null)} onConfirm={() => remove.mutate()}
        title={tc("removeTitle", name)} body={tc("removeBody")} confirmLabel={tc("remove")} busy={remove.isPending}
        testId="central-remove-confirm" />
    </Drawer>
  );
}

function Money({ value }: { value: number | null }) {
  const t = useT(BUDGETS);
  return <span style={{ fontFamily: mono }}>{value == null ? t("noCap") : usd(value, 2)}</span>;
}

function CentralTable({ rows, loading, onEdit }: {
  rows: CentralBudgetRow[]; loading: boolean; onEdit?: (r: CentralBudgetRow) => void;
}) {
  const t = useT(BUDGETS);
  const tc = useT(CENTRAL);
  const label = useScopeLabel();
  const columns = useMemo<ColumnDef<CentralBudgetRow, unknown>[]>(() => [
    { id: "scope", header: tc("colScope"), accessorKey: "scope",
      cell: (c) => (
        <Lines>
          <span style={{ fontWeight: 600, color: K.ink }}>{label(c.row.original.scope_kind, c.row.original.target)}</span>
          <span style={{ fontFamily: mono, fontSize: 12, color: K.muted2 }}>{c.row.original.scope}</span>
        </Lines>
      ) },
    { id: "per_run", header: t("colPerRun"), accessorFn: (r) => r.per_run_usd ?? -1, meta: { align: "right" },
      cell: (c) => <Money value={c.row.original.per_run_usd} /> },
    { id: "per_day", header: t("colPerDay"), accessorFn: (r) => r.per_day_usd ?? -1, meta: { align: "right" },
      cell: (c) => <Money value={c.row.original.per_day_usd} /> },
    { id: "per_month", header: tc("colPerMonth"), accessorFn: (r) => r.per_month_usd ?? -1, meta: { align: "right" },
      cell: (c) => <Money value={c.row.original.per_month_usd} /> },
    { id: "at_cap", header: t("colAtCap"), accessorKey: "hard_stop",
      cell: (c) => (
        <Badge tone={c.row.original.hard_stop ? "danger" : "warning"} dot={false} style={{ whiteSpace: "nowrap" }}>
          {c.row.original.hard_stop ? t("stopsAtCap") : t("alertOnly")} · {c.row.original.alert_pct} %
        </Badge>
      ) },
    { id: "spent", header: t("colSpentToday"), accessorFn: (r) => r.spent_today_usd ?? -1, meta: { align: "right" },
      cell: (c) => {
        const r = c.row.original;
        if (r.spent_today_usd == null) return <span style={small}>{tc("unknown")}</span>;
        return (
          <Lines>
            <span style={{ fontFamily: mono }}>{usd(r.spent_today_usd)}</span>
            {r.spent_month_usd != null && <span style={small}>{tc("colSpentMonth")}: {usd(r.spent_month_usd)}</span>}
            {r.used_pct != null && <span style={small}>{t("ofCap", pct(r.used_pct))}</span>}
          </Lines>
        );
      } },
    { id: "state", header: t("colState"), accessorKey: "state",
      cell: (c) => c.row.original.state === "unknown"
        ? <Badge tone="neutral" dot={false} style={{ whiteSpace: "nowrap" }}>{tc("unknown")}</Badge>
        : <StateBadge state={c.row.original.state} /> },
    { id: "changed", header: t("colChanged"), accessorFn: (r) => r.updated_at ?? "",
      cell: (c) => <ChangedCell row={c.row.original} /> },
    { id: "actions", header: "", enableSorting: false, enableHiding: false,
      cell: (c) => onEdit ? (
        <span onClick={(e) => e.stopPropagation()}>
          <Button size="sm" variant="secondary" onClick={() => onEdit(c.row.original)}
            testId={`central-edit-${c.row.original.scope}`}>{t("edit")}</Button>
        </span>
      ) : null },
  ], [t, tc, label, onEdit]);
  return (
    <DataTable<CentralBudgetRow>
      tableId="ai-gateway-budgets-central" data={rows} columns={columns} getRowId={(r) => r.scope} loading={loading}
      onRowClick={onEdit} searchable={false} paginate={false} stickyHeader={false} tableMinWidth={1040}
      emptyTitle={t("emptyRows")}
    />
  );
}

function CallSiteTable({ rows, loading }: { rows: CallSiteRow[]; loading: boolean }) {
  const t = useT(BUDGETS);
  const tc = useT(CENTRAL);
  const label = useScopeLabel();
  const columns = useMemo<ColumnDef<CallSiteRow, unknown>[]>(() => [
    { id: "stage", header: tc("colStage"), accessorKey: "stage",
      cell: (c) => {
        const r = c.row.original;
        return (
          <Lines>
            <span style={{ fontFamily: mono, fontWeight: 600, color: K.ink }}>{r.stage}</span>
            <span style={small}>
              {r.role ?? "—"}
              {!r.routed ? ` · ${tc("notRouted")}` : r.active === false ? ` · ${tc("switchedOff")}` : ""}
            </span>
          </Lines>
        );
      } },
    { id: "caps", header: tc("colCaps"), accessorFn: (r) => r.caps.length,
      cell: (c) => c.row.original.caps.length === 0
        ? <Badge tone="warning" dot={false} style={{ whiteSpace: "nowrap" }}>{tc("noCentralCap")}</Badge>
        : (
          <span style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
            {c.row.original.caps.map((cap) => {
              const parts = scopeParts(cap.scope);
              return (
                <Badge key={cap.scope} tone={cap.hard_stop ? "danger" : budgetTone(cap.state)} dot={false}
                  style={{ whiteSpace: "nowrap" }}>
                  {parts ? label(parts[0], parts[1]) : cap.scope} · {cap.hard_stop ? t("stopsAtCap") : t("alertOnly")}
                </Badge>
              );
            })}
          </span>
        ) },
    { id: "jobs", header: tc("colJobKinds"), enableSorting: false,
      cell: (c) => <span style={{ ...small, fontFamily: mono }}>{c.row.original.job_kinds.join(", ") || "—"}</span> },
    { id: "today", header: t("colSpentToday"), accessorKey: "today_usd", meta: { align: "right" },
      cell: (c) => <span style={{ fontFamily: mono }}>{usd(c.row.original.today_usd)}</span> },
    { id: "month", header: tc("colMonthSpend"), accessorKey: "month_usd", meta: { align: "right" },
      cell: (c) => <span style={{ fontFamily: mono }}>{usd(c.row.original.month_usd)}</span> },
    { id: "calls", header: tc("colCalls7d"), accessorKey: "calls_7d", meta: { align: "right" },
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.calls_7d}</span> },
  ], [t, tc, label]);
  return (
    <DataTable<CallSiteRow>
      tableId="ai-gateway-budgets-call-sites" data={rows} columns={columns} getRowId={(r) => r.stage}
      loading={loading} paginate={false} stickyHeader={false} tableMinWidth={900}
      emptyTitle={t("emptyRows")}
    />
  );
}

// ── the tab ────────────────────────────────────────────────────────────────────────────────────────────────────────

export default function BudgetsTab({ onOpenConnections }: { onOpenConnections: () => void }) {
  const t = useT(BUDGETS);
  // AA-1025 C1 follow-up: Add / Edit only when the API says this session may write budgets (usePageAccess).
  const canEdit = usePageAccess().can("budgets");
  const q = useQuery({
    queryKey: BUDGETS_KEY,
    queryFn: () => apiGet<BudgetsResp>(BUDGETS_API, { admin: true }),
    refetchInterval: 120_000,
    refetchIntervalInBackground: false,
  });
  const [editing, setEditing] = useState<Editing | null>(null);
  const [centralEditing, setCentralEditing] = useState<{ row: CentralBudgetRow | null } | null>(null);
  const tc = useT(CENTRAL);
  const data = q.data;
  const tiers = data?.tiers;
  const existing = useMemo(
    () => new Set([...(tiers?.provider ?? []), ...(tiers?.job ?? [])].map((r) => `${r.provider}/${r.scope}`)),
    [tiers]);

  if (q.error) {
    return <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} onRetry={() => q.refetch()} />;
  }

  const loading = !data;
  const capped = (data?.coverage ?? []).filter((c) => c.tier === "provider" && c.capped).length;
  const enabledConns = (tiers?.connection ?? []).filter((c) => c.enabled);
  const central = tiers?.central ?? [];
  const sites = data?.call_sites ?? [];
  const near = [...(tiers?.provider ?? []), ...(tiers?.job ?? []), ...central]
    .filter((r) => r.state === "alert" || r.state === "cap_reached").length
    + (tiers?.connection ?? []).filter((c) => c.state === "alert" || c.state === "stopped").length;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16 }}
      data-testid={data ? "ai-gateway-budgets-ready" : undefined}>
      <p style={{ ...small, margin: 0 }}>{t("intro")}</p>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(220px, 100%), 1fr))", gap: 12 }}>
        <KpiTile label={t("tileProviders")} value={data ? `${capped} / ${data.providers.length}` : undefined}
          hint={data ? t("tileProvidersHint", data.providers.length) : undefined} />
        <KpiTile label={t("tileConnections")}
          value={data ? `${enabledConns.filter((c) => c.monthly_budget_usd != null).length} / ${enabledConns.length}` : undefined}
          hint={data ? t("tileConnectionsHint", enabledConns.length) : undefined} />
        <KpiTile label={t("tileNear")} value={data ? near : undefined} valueTone={near ? "warning" : undefined}
          hint={t("tileNearHint")} />
        <KpiTile label={tc("tileCovered")}
          value={data ? `${sites.filter((x) => x.capped).length} / ${sites.length}` : undefined}
          hint={data ? tc("tileCoveredHint", sites.length) : undefined} />
      </div>
      {data && data.errors.length > 0 && (
        <div role="status" style={{ ...small, color: K.danger }}>{t("sourceErrors")}</div>
      )}
      <Section title={t("groupProvider")} description={t("groupProviderHelp")} padded={false}
        actions={canEdit ? (
          <Button size="sm" variant="secondary" disabled={!data} onClick={() => setEditing({ row: null })}
            testId="budget-add"><Plus size={13} /> {t("addBudget")}</Button>
        ) : undefined}>
        <SpendBudgetTable id="ai-gateway-budgets-provider" rows={tiers?.provider ?? []} loading={loading}
          onEdit={canEdit ? (row) => setEditing({ row }) : undefined} />
      </Section>
      <Section title={t("groupJob")} description={t("groupJobHelp")} padded={false}>
        <SpendBudgetTable id="ai-gateway-budgets-job" rows={tiers?.job ?? []} loading={loading}
          onEdit={canEdit ? (row) => setEditing({ row }) : undefined} />
      </Section>
      <Section title={tc("group")} description={tc("groupHelp")} padded={false} testId="ai-gateway-budgets-central-tier"
        actions={canEdit ? (
          <Button size="sm" variant="secondary" disabled={!data} onClick={() => setCentralEditing({ row: null })}
            testId="central-add"><Plus size={13} /> {tc("add")}</Button>
        ) : undefined}>
        <CentralTable rows={central} loading={loading} onEdit={canEdit ? (row) => setCentralEditing({ row }) : undefined} />
      </Section>
      <Section title={t("groupConnection")} description={t("groupConnectionHelp")} padded={false}
        actions={<Button size="sm" variant="secondary" onClick={onOpenConnections}>{t("editConnections")}</Button>}>
        <ConnectionBudgetTable rows={tiers?.connection ?? []} loading={loading} />
      </Section>
      {data?.tenant_ready ? (
        <Section title={t("groupTenant")} description={<>{t("groupTenantHelp")} {t("tenantStoredOnly")}</>}
          padded={false} testId="ai-gateway-budgets-tenant-tier">
          <TenantBudgetTable rows={tiers?.tenant ?? []} loading={loading} />
        </Section>
      ) : (
        <Section title={t("groupTenant")} description={t("groupTenantHelp")} testId="ai-gateway-budgets-tenant-tier">
          <EmptyState icon={<Wallet size={28} />} title={t("tenantEmptyTitle")} description={t("tenantEmptyBody")} />
        </Section>
      )}
      <Section title={t("groupCoverage")} description={t("groupCoverageHelp")} padded={false}>
        <CoverageTable rows={data?.coverage ?? []} loading={loading} />
      </Section>
      <Section title={tc("sites")} description={tc("sitesHelp")} padded={false} testId="ai-gateway-budgets-call-sites">
        <CallSiteTable rows={sites} loading={loading} />
      </Section>
      {centralEditing && data && (
        <CentralEditor key={centralEditing.row?.scope ?? "new"} row={centralEditing.row}
          existing={new Set(central.map((r) => r.scope))} onClose={() => setCentralEditing(null)} />
      )}
      {editing && data && (
        <BudgetEditor key={editing.row ? `${editing.row.provider}/${editing.row.scope}` : "new"} editing={editing}
          providers={data.providers} existing={existing} onClose={() => setEditing(null)} />
      )}
    </div>
  );
}
