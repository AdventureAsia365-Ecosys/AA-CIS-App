"use client";
// app/admin/ai-gateway/BudgetsTab.tsx — AA-804 UI part 2 / AA-1017: the Budgets tab of AI providers (replaces Cost
// Operations › Budgets, whose old link is sent on here).
//
// One read, GET /api/admin/ai-gateway/budgets (backend api/services/ai_gateway.py read_budgets): every cap by tier.
//   Provider daily caps + Job caps   shared.spend_budget, edited in a kit Drawer through the existing
//                                    PUT /api/admin/budgets/{provider}/{scope} (spend.write; confirm first).
//   LLM connection budgets           read here; edited with the connection's settings (Providers › Connections).
//   Tenant AI budgets                empty until the tenant budget table exists (AA-1016).
//   Coverage                         does every paid provider and switched-on connection have a cap.
// Built on the kit only (KpiTile, Section, DataTable, Drawer, Field/Input/Select, ConfirmModal, Badge, Toast).

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Wallet } from "lucide-react";
import {
  Badge, Button, ConfirmModal, DataTable, DateText, Drawer, EmptyState, ErrorState, Field, Input, K, KpiTile, Section,
  Select, apiGet, apiSend, mono, useT, useToast, type ColumnDef,
} from "../../_kit";
import {
  BUDGETS_API, budgetBody, budgetDraftOf, budgetProviderLabel, budgetPutPath, budgetTone, pct, sameDraft, usd,
  validScope, type BudgetDraft, type BudgetError, type BudgetsResp, type ConnectionBudgetRow, type CoverageRow,
  type SpendBudgetRow,
} from "@/lib/aiGateway";
import { connectionName } from "@/lib/llmConnections";
import { useAccountText, useAwsAccounts } from "../_components/useAwsAccounts";
import { BUDGETS, BUDGET_STATE } from "./budgets.messages";

export const BUDGETS_KEY = ["ai-gateway", "budgets"] as const;

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;

function Lines({ children }: { children: React.ReactNode }) {
  return <div style={{ display: "grid", gap: 2, minWidth: 0 }}>{children}</div>;
}

function StateBadge({ state }: { state: string }) {
  const ts = useT(BUDGET_STATE);
  const known = state in BUDGET_STATE.en;
  return <Badge tone={budgetTone(state)} dot={false}>{known ? ts(state as keyof typeof BUDGET_STATE.en) : state}</Badge>;
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
      setApiError(e instanceof Error ? e.message : String(e));
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
  id: string; rows: SpendBudgetRow[]; loading: boolean; onEdit: (r: SpendBudgetRow) => void;
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
        <Badge tone={c.row.original.hard_stop ? "danger" : "warning"} dot={false}>
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
        </Lines>
      ) },
    { id: "state", header: t("colState"), accessorKey: "state", cell: (c) => <StateBadge state={c.row.original.state} /> },
    { id: "changed", header: t("colChanged"), accessorFn: (r) => r.updated_at ?? "",
      cell: (c) => (
        <Lines>
          <span style={small}>{c.row.original.updated_by ?? "—"}</span>
          {c.row.original.updated_at && <span style={small}><DateText value={c.row.original.updated_at} relative /></span>}
        </Lines>
      ) },
    { id: "actions", header: "", enableSorting: false, enableHiding: false,
      cell: (c) => (
        <span onClick={(e) => e.stopPropagation()}>
          <Button size="sm" variant="secondary" onClick={() => onEdit(c.row.original)}
            testId={`budget-edit-${c.row.original.provider}-${c.row.original.scope}`}>{t("edit")}</Button>
        </span>
      ) },
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
      cell: (c) => <span style={small}>{c.row.original.hard_stop ? t("stopsAtCap") : t("alertOnly")} · {c.row.original.alert_pct} %</span> },
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

// ── the tab ────────────────────────────────────────────────────────────────────────────────────────────────────────

export default function BudgetsTab({ onOpenConnections }: { onOpenConnections: () => void }) {
  const t = useT(BUDGETS);
  const q = useQuery({
    queryKey: BUDGETS_KEY,
    queryFn: () => apiGet<BudgetsResp>(BUDGETS_API, { admin: true }),
    refetchInterval: 120_000,
    refetchIntervalInBackground: false,
  });
  const [editing, setEditing] = useState<Editing | null>(null);
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
  const near = [...(tiers?.provider ?? []), ...(tiers?.job ?? [])].filter((r) => r.state === "alert" || r.state === "cap_reached").length
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
      </div>
      {data && data.errors.length > 0 && (
        <div role="status" style={{ ...small, color: K.danger }}>{t("sourceErrors")}</div>
      )}
      <Section title={t("groupProvider")} description={t("groupProviderHelp")} padded={false}
        actions={
          <Button size="sm" variant="secondary" disabled={!data} onClick={() => setEditing({ row: null })}
            testId="budget-add"><Plus size={13} /> {t("addBudget")}</Button>
        }>
        <SpendBudgetTable id="ai-gateway-budgets-provider" rows={tiers?.provider ?? []} loading={loading}
          onEdit={(row) => setEditing({ row })} />
      </Section>
      <Section title={t("groupJob")} description={t("groupJobHelp")} padded={false}>
        <SpendBudgetTable id="ai-gateway-budgets-job" rows={tiers?.job ?? []} loading={loading}
          onEdit={(row) => setEditing({ row })} />
      </Section>
      <Section title={t("groupConnection")} description={t("groupConnectionHelp")} padded={false}
        actions={<Button size="sm" variant="secondary" onClick={onOpenConnections}>{t("editConnections")}</Button>}>
        <ConnectionBudgetTable rows={tiers?.connection ?? []} loading={loading} />
      </Section>
      <Section title={t("groupTenant")} description={t("groupTenantHelp")}>
        <EmptyState icon={<Wallet size={28} />} title={t("tenantEmptyTitle")} description={t("tenantEmptyBody")} />
      </Section>
      <Section title={t("groupCoverage")} description={t("groupCoverageHelp")} padded={false}>
        <CoverageTable rows={data?.coverage ?? []} loading={loading} />
      </Section>
      {editing && data && (
        <BudgetEditor key={editing.row ? `${editing.row.provider}/${editing.row.scope}` : "new"} editing={editing}
          providers={data.providers} existing={existing} onClose={() => setEditing(null)} />
      )}
    </div>
  );
}
