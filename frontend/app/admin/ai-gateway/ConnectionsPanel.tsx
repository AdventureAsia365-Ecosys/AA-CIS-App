"use client";
// app/admin/ai-gateway/ConnectionsPanel.tsx
// Gateway slice S4 — the connection settings, now AI providers › Providers › Connections (AA-804 UI part 2; was the
// Connections sub-tab of Settings › LLM Models). Admin only (the page; the writes need ai.admin on the backend).
// One row per LLM connection (GET /api/admin/llm-connections) with its circuit-breaker health
// (GET /api/admin/llm-availability → connections). Switch a connection on/off and edit its timeout, retries, limiter
// and budget (PATCH /api/admin/llm-connections/{key}). AA-1017: the secret NAME and the base URL are shown read-only
// (they change through a reviewed migration; lib/llmConnections patchBody never sends them); send one small test request
// (POST /api/admin/llm-connections/{key}/test). A Bedrock connection is named by its AWS account ID.
// Gateway slice S10 — the budget column and the drawer's "Budget and cost check" section
// (GET /api/admin/llm-connections/costs: month-to-date spend against each budget, the nightly reconciliation rows;
// POST /api/admin/llm-connections/reconcile queues a $0 check); the budget policy fields join the settings form.
// Built on the kit only (DataTable, Drawer, ConfirmModal, Field/Input/Select/Textarea, Badge, Button, Toast).

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, X } from "lucide-react";
import {
  Badge, Button, ConfirmModal, DataTable, DateText, Drawer, Field, Input, K, Select, Textarea, apiGet, apiSend, mono,
  sans, useT, useToast, type ColumnDef, type Translator,
} from "../../_kit";
import { CONNECTIONS } from "./connections.messages";
import { useErrorClassText } from "./useErrorClassText";
import { type CatalogRow } from "./llmShared";
import { useAccountText, useAwsAccounts } from "../_components/useAwsAccounts";
import {
  type AvailabilityHealth, type BudgetSummary, type Connection, type ConnectionCost, type ConnectionHealth,
  type ConnectionsResp, type CostsResp, type Deployment, type EditField, type EditForm, type HealthStatus,
  type ReconcileRow, type TestResult, budgetTone, canSwitchOn, circuitTone, connectionName, formOf, healthTone,
  isProvisional, modelHealth, patchBody, reconcileTone, takesBaseUrl, takesSecret, takesUsageKey, testChoices, usd,
} from "@/lib/llmConnections";

type T = Translator<typeof CONNECTIONS.en>;

const HEALTH_KEY: Record<HealthStatus, "healthOk" | "healthOpen" | "healthDegraded" | "healthDisabled" | "healthUnknown"> = {
  ok: "healthOk", open: "healthOpen", degraded: "healthDegraded", disabled: "healthDisabled", unknown: "healthUnknown",
};
const CIRCUIT_KEY = { closed: "circuitClosed", open: "circuitOpen", half_open: "circuitHalfOpen" } as const;

const small = { fontSize: 12, color: K.muted } as const;
const code = { fontFamily: mono, fontSize: 12, color: K.ink2, overflowWrap: "anywhere" } as const;

function credentialText(c: Connection, t: T): string {
  switch (c.credential_source) {
    case "sts": return t("credIamRole");
    case "task_role": return t("credTaskRole");
    case "secret": return c.secret_name ?? t("credSecret");
    case "env": return t("credEnv", c.credential_env ?? "", !!c.credential_env_set);
    default: return t("credMissing");
  }
}

function limitsText(c: Connection, t: T): string {
  const parts = [
    c.timeout_s != null ? t("limitTimeout", c.timeout_s) : null,
    c.max_retries != null ? t("limitRetries", c.max_retries) : null,
    c.max_concurrency != null ? t("limitConcurrency", c.max_concurrency) : null,
    c.rpm_limit != null ? t("limitRpm", c.rpm_limit) : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : t("noLimits");
}

const BUDGET_KEY = { ok: "budgetOk", alert: "budgetAlert", stopped: "budgetStopped" } as const;
const STATUS_KEY = {
  ok: "stOk", mismatch: "stMismatch", not_configured: "stNotConfigured", unsupported: "stUnsupported",
  no_data: "stNoData", error: "stError",
} as const;
const SOURCE_KEY: Record<string, "srcAws" | "srcAnthropic" | "srcOpenai" | "srcNone"> = {
  aws_cost_explorer: "srcAws", anthropic_cost_report: "srcAnthropic", openai_costs: "srcOpenai", none: "srcNone",
};

function BudgetBadge({ budget, t }: { budget: BudgetSummary | null | undefined; t: T }) {
  if (!budget) return <span style={small}>{t("budgetNone")}</span>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3, alignItems: "flex-start" }}>
      <Badge tone={budgetTone(budget.state)}>{t(BUDGET_KEY[budget.state] ?? "budgetOk")}</Badge>
      <span style={small}>
        {t("budgetOf", usd(budget.month_spent_usd), usd(budget.monthly_budget_usd))}
        {!budget.hard_stop && ` · ${t("budgetAlertOnly")}`}
      </span>
    </div>
  );
}

function StatusBadge({ row, t }: { row: ReconcileRow; t: T }) {
  return (
    <span style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
      <Badge tone={reconcileTone(row.status)}>{t(STATUS_KEY[row.status] ?? "stNoData")}</Badge>
      {isProvisional(row) && <span style={small}>{t("provisional")}</span>}
    </span>
  );
}

function HealthBadge({ health, t }: { health?: ConnectionHealth; t: T }) {
  const status = (health?.status ?? "unknown") as HealthStatus;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3, alignItems: "flex-start" }}>
      <Badge tone={healthTone(status)}>{t(HEALTH_KEY[status] ?? "healthUnknown")}</Badge>
      {!!health?.open_models && <span style={small}>{t("openModels", health.open_models)}</span>}
    </div>
  );
}

// ── the drawer: details, deployments with their circuit, settings, test ─────────────────────────────────────────

function DeploymentsTable({ conn, health, t }: { conn: Connection; health?: ConnectionHealth; t: T }) {
  const errorText = useErrorClassText();
  const columns = useMemo<ColumnDef<Deployment, unknown>[]>(() => [
    {
      accessorKey: "model_key",
      header: t("colModel"),
      cell: (c) => (
        <div>
          <div style={{ ...code, color: K.ink }}>{c.row.original.model_key}</div>
          <div style={{ ...code, color: K.muted2, fontSize: 11 }}>{c.row.original.wire_id}</div>
        </div>
      ),
    },
    {
      accessorKey: "priority",
      header: t("colPriority"),
      cell: (c) => (
        <span style={{ fontFamily: mono, fontSize: 12 }}>
          {c.row.original.priority}{!c.row.original.enabled && <span style={{ color: K.muted2 }}> · {t("depOff")}</span>}
        </span>
      ),
    },
    {
      id: "circuit",
      header: t("colCircuit"),
      enableSorting: false,
      cell: (c) => {
        const h = modelHealth(health, c.row.original.model_key);
        if (!h) return <span style={small}>{t("noHealth")}</span>;
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 3, alignItems: "flex-start" }}>
            <Badge tone={circuitTone(h.state)}>{t(CIRCUIT_KEY[h.state] ?? "circuitClosed")}</Badge>
            {h.state === "open" && h.circuit_open_until && (
              <span style={small}>{t("openUntil")} <DateText value={h.circuit_open_until} withTime /></span>
            )}
            {h.consecutive_failures > 0 && <span style={small}>{t("failuresInRow", h.consecutive_failures)}</span>}
          </div>
        );
      },
    },
    {
      id: "last_ok",
      header: t("colLastOk"),
      enableSorting: false,
      cell: (c) => {
        const h = modelHealth(health, c.row.original.model_key);
        return h?.last_ok_at ? <DateText value={h.last_ok_at} relative /> : <span style={small}>—</span>;
      },
    },
    {
      id: "last_error",
      header: t("colLastError"),
      enableSorting: false,
      cell: (c) => {
        const h = modelHealth(health, c.row.original.model_key);
        if (!h?.last_error_at) return <span style={small}>—</span>;
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 2, maxWidth: 260 }}>
            <span style={{ ...small, color: K.danger }}>{errorText(h.last_error_class)}</span>
            <DateText value={h.last_error_at} relative style={small} />
            {h.last_error && <span style={{ ...small, overflowWrap: "anywhere" }} title={h.last_error}>
              {h.last_error.slice(0, 120)}</span>}
          </div>
        );
      },
    },
  ], [health, t, errorText]);
  return (
    <DataTable<Deployment>
      data={conn.deployments}
      columns={columns}
      tableId={`admin-llm-connection-deployments`}
      getRowId={(d) => d.model_key}
      pageSize={25}
      tableMinWidth={640}
      emptyTitle={t("empty")}
    />
  );
}

function SettingsForm({ conn, name, t }: { conn: Connection; name: string; t: T }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [form, setForm] = useState<EditForm>(() => formOf(conn));
  const [apiError, setApiError] = useState("");
  const { body, errors } = patchBody(conn, form);
  const changed = Object.keys(body).length > 0;
  const hasErrors = Object.keys(errors).length > 0;

  const mut = useMutation({
    mutationFn: () => apiSend(`/api/admin/llm-connections/${encodeURIComponent(conn.connection_key)}`,
      { method: "PATCH", body, admin: true }),
    onSuccess: () => {
      toast.success(t("saved", name));
      setApiError("");
      qc.invalidateQueries({ queryKey: ["llm-connections"] });
      qc.invalidateQueries({ queryKey: ["llm-connection-costs"] });
    },
    onError: (e) => setApiError(e instanceof Error ? e.message : String(e)),
  });

  const errorText = (f: EditField) => {
    const e = errors[f];
    if (!e) return undefined;
    if (e.code === "secretPrefix") return t("errSecretPrefix");
    if (e.code === "wholeNumber") return t("errWhole");
    if (e.code === "scopeId") return t("errScopeId");
    return t("errRange", e.min ?? 0, e.max ?? 0);
  };
  const set = (f: EditField) => (e: { target: { value: string } }) => setForm((x) => ({ ...x, [f]: e.target.value }));
  const num = (f: EditField, label: string) => (
    <Field label={label} error={errorText(f)} hint={t("emptyMeansDefault")}>
      <Input inputMode="decimal" value={form[f]} onChange={set(f)} disabled={mut.isPending}
             data-testid={`conn-field-${f}`} />
    </Field>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <p style={{ ...small, margin: 0 }}>{t("editIntro")}</p>
      {takesSecret(conn) && (
        <Field label={t("secretName")} hint={`${t("secretHint")} ${t("viaMigration")}`} locked>
          <Input value={conn.secret_name ?? ""} readOnly spellCheck={false} autoComplete="off"
                 style={{ fontFamily: mono }} data-testid="conn-field-secret_name" />
        </Field>
      )}
      {takesBaseUrl(conn) && (
        <Field label={t("baseUrl")} hint={`${t("baseUrlHint")} ${t("viaMigration")}`} locked>
          <Input type="url" value={conn.base_url ?? ""} readOnly spellCheck={false} autoComplete="off"
                 style={{ fontFamily: mono }} data-testid="conn-field-base_url" />
        </Field>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 12 }}>
        {num("timeout_s", t("timeout"))}
        {num("max_retries", t("retries"))}
        {num("max_concurrency", t("concurrency"))}
        {num("rpm_limit", t("rpm"))}
        {num("monthly_budget_usd", t("budget"))}
        {conn.cost_policy && num("budget_alert_pct", t("alertPct"))}
      </div>
      {conn.cost_policy && (
        <Field label={t("hardStop")} hint={t("hardStopHint")}>
          <Select value={form.budget_hard_stop} disabled={mut.isPending}
                  onChange={(e) => setForm((x) => ({ ...x, budget_hard_stop: e.target.value === "false" ? "false" : "true" }))}
                  options={[{ value: "true", label: t("hardStopYes") }, { value: "false", label: t("hardStopNo") }]}
                  style={{ maxWidth: "100%", minWidth: 0 }} />
        </Field>
      )}
      {conn.cost_policy && takesUsageKey(conn) && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))", gap: 12 }}>
          <Field label={t("usageSecret")} error={errorText("usage_secret_name")} hint={t("usageSecretHint")}>
            <Input value={form.usage_secret_name} onChange={set("usage_secret_name")} disabled={mut.isPending}
                   spellCheck={false} autoComplete="off" style={{ fontFamily: mono }} data-testid="conn-field-usage_secret_name" />
          </Field>
          <Field label={t("usageScope")} error={errorText("usage_scope_id")} hint={t("usageScopeHint")}>
            <Input value={form.usage_scope_id} onChange={set("usage_scope_id")} disabled={mut.isPending}
                   spellCheck={false} autoComplete="off" style={{ fontFamily: mono }} />
          </Field>
        </div>
      )}
      <Field label={t("notes")}>
        <Textarea value={form.notes} onChange={set("notes")} disabled={mut.isPending} rows={2} />
      </Field>
      {apiError && <span data-testid="conn-save-error" style={{ fontSize: 12, color: K.danger }}>{apiError}</span>}
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <Button variant="primary" size="sm" disabled={!changed || hasErrors || mut.isPending} onClick={() => mut.mutate()}>
          {mut.isPending ? t("saving") : t("save")}
        </Button>
        {!changed && <span style={small}>{t("noChange")}</span>}
      </div>
    </div>
  );
}

function TestPanel({ conn, name, t }: { conn: Connection; name: string; t: T }) {
  const errorText = useErrorClassText();
  const qc = useQueryClient();
  const catalog = useQuery({
    queryKey: ["llm-catalog"],
    queryFn: () => apiGet<{ models: CatalogRow[] }>("/api/admin/llm-catalog", { admin: true }),
  });
  const styleOf = (key: string) => catalog.data?.models.find((m) => m.model_key === key)?.api_style;
  const choices = testChoices(conn, styleOf);
  const [model, setModel] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [result, setResult] = useState<TestResult | null>(null);
  const [error, setError] = useState("");

  const mut = useMutation({
    mutationFn: () => apiSend<TestResult>(
      `/api/admin/llm-connections/${encodeURIComponent(conn.connection_key)}/test`,
      { method: "POST", body: model ? { model_key: model } : {}, admin: true }),
    onSuccess: (r) => {
      setResult(r);
      setError("");
      setConfirm(false);
      qc.invalidateQueries({ queryKey: ["llm-availability"] });
    },
    onError: (e) => {
      setError(e instanceof Error ? e.message : String(e));
      setConfirm(false);
    },
  });

  if (!choices.length) return <span style={small}>{t("noTestable")}</span>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <p style={{ ...small, margin: 0 }}>{t("testIntro")}</p>
      <div style={{ display: "flex", gap: 8, alignItems: "flex-end", flexWrap: "wrap" }}>
        <Field label={t("testModel")} style={{ flex: "1 1 220px" }}>
          <Select value={model} onChange={(e) => setModel(e.target.value)} disabled={mut.isPending}
                  placeholder={t("testCheapest")} style={{ maxWidth: "100%", minWidth: 0 }}
                  options={choices.map((d) => ({ value: d.model_key, label: `${d.model_key}${d.enabled ? "" : ` (${t("depOff")})`}` }))} />
        </Field>
        <span data-testid="conn-run-test">
          <Button variant="secondary" size="md" disabled={mut.isPending} onClick={() => setConfirm(true)}>
            {mut.isPending ? t("testing") : t("runTest")}
          </Button>
        </span>
      </div>
      {error && <span style={{ fontSize: 12, color: K.danger }}>{error}</span>}
      {result && (
        <div data-testid="conn-test-result" style={{
          display: "flex", flexDirection: "column", gap: 4, padding: "10px 12px", borderRadius: 8,
          border: `1px solid ${K.line}`, background: K.card,
        }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <Badge tone={result.ok ? (result.reply_usable ? "success" : "warning") : "danger"}>
              {result.ok ? (result.reply_usable ? t("testOk") : t("testUnusable")) : t("testFailed")}
            </Badge>
            <span style={small}>
              {t("testDetail", result.model_key, result.latency_ms, `$${(result.cost_usd ?? 0).toFixed(6)}`)}
            </span>
          </div>
          {result.error_class && <span style={small}>{t("testErrorClass")}: {errorText(result.error_class)}</span>}
          {result.error && <span style={{ ...small, overflowWrap: "anywhere" }}>{result.error}</span>}
        </div>
      )}
      <ConfirmModal
        open={confirm}
        onClose={() => setConfirm(false)}
        onConfirm={() => mut.mutate()}
        title={t("testConfirmTitle", name)}
        body={t("testIntro")}
        confirmLabel={t("testConfirm")}
        busy={mut.isPending}
        testId="conn-test-confirm"
      />
    </div>
  );
}

function CostPanel({ cost, tolerance, ready, loadError, t }: {
  cost?: ConnectionCost; tolerance: CostsResp["tolerance"]; ready: boolean; loadError: string; t: T;
}) {
  const toast = useToast();
  const qc = useQueryClient();
  const [error, setError] = useState("");
  const run = useMutation({
    mutationFn: () => apiSend("/api/admin/llm-connections/reconcile", { method: "POST", body: {}, admin: true }),
    onSuccess: () => {
      toast.success(t("reconcileQueued"));
      setError("");
      qc.invalidateQueries({ queryKey: ["llm-connection-costs"] });
    },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  });
  const columns = useMemo<ColumnDef<ReconcileRow, unknown>[]>(() => [
    { accessorKey: "day", header: t("colDay"), cell: (c) => <span style={code}>{c.row.original.day}</span> },
    {
      id: "logged", header: t("colLogged"), enableSorting: false,
      cell: (c) => <span style={code}>{usd(c.row.original.logged_usd)} · {c.row.original.logged_calls}</span>,
    },
    { id: "provider", header: t("colProvider"), enableSorting: false,
      cell: (c) => <span style={code}>{usd(c.row.original.provider_usd)}</span> },
    {
      id: "diff", header: t("colDiff"), enableSorting: false,
      cell: (c) => {
        const r = c.row.original;
        if (r.diff_usd == null) return <span style={small}>—</span>;
        return <span style={code}>{usd(r.diff_usd)}{r.diff_pct != null ? ` (${r.diff_pct}%)` : ""}</span>;
      },
    },
    { id: "status", header: t("colStatus"), enableSorting: false, cell: (c) => <StatusBadge row={c.row.original} t={t} /> },
  ], [t]);

  if (loadError) return <span style={{ fontSize: 12, color: K.danger }}>{loadError}</span>;
  if (!cost) return <span style={small}>{ready ? t("reconcileNever") : t("costsNotReady")}</span>;
  const b = cost.budget;
  const last = cost.reconcile.last;
  const fact = (label: string, value: React.ReactNode) => (
    <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
      <span style={{ fontSize: 11, color: K.muted2, textTransform: "uppercase", letterSpacing: "0.06em" }}>{label}</span>
      <span style={{ fontSize: 13, color: K.ink, overflowWrap: "anywhere" }}>{value}</span>
    </div>
  );
  return (
    <div data-testid="conn-costs" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <p style={{ ...small, margin: 0 }}>{t("costsIntro")}</p>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 12 }}>
        {fact(t("colBudget"), <BudgetBadge budget={b} t={t} />)}
        {b && fact(t("monthSpent"), (
          <span style={code}>
            {usd(b.month_spent_usd)}{b.used_pct != null ? ` · ${t("budgetPct", Math.round(b.used_pct))}` : ""}
            <br />{t("monthCalls", b.month_calls)}
          </span>
        ))}
        {fact(t("reconcileSource"), <span style={code}>{t(SOURCE_KEY[cost.reconcile.source] ?? "srcNone")}</span>)}
        {fact(t("reconcileLast"), last ? (
          <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <StatusBadge row={last} t={t} />
            <span style={small}>{last.day}{last.checked_at && <> · <DateText value={last.checked_at} relative /></>}</span>
          </span>
        ) : <span style={small}>{t("reconcileNever")}</span>)}
      </div>
      {!cost.reconcile.configured && cost.reconcile.source !== "none" && (
        <span style={small}>{t("reconcileNotConfigured")}</span>
      )}
      {cost.reconcile.days.length > 0 && (
        <DataTable<ReconcileRow>
          data={cost.reconcile.days}
          columns={columns}
          tableId="admin-llm-connection-reconcile"
          getRowId={(r) => r.day}
          pageSize={14}
          tableMinWidth={560}
          emptyTitle={t("reconcileNever")}
        />
      )}
      {tolerance && <span style={small}>{t("reconcileNote", usd(tolerance.usd), tolerance.pct)}</span>}
      {error && <span style={{ fontSize: 12, color: K.danger }}>{error}</span>}
      <div>
        <span data-testid="conn-reconcile-now">
          <Button variant="secondary" size="sm" disabled={run.isPending || !ready} onClick={() => run.mutate()}>
            {run.isPending ? t("reconcileRunning") : t("reconcileNow")}
          </Button>
        </span>
      </div>
    </div>
  );
}

function ConnectionDrawer({ conn, health, name, onClose, t, cost, costs, costsError }: {
  conn: Connection; health?: ConnectionHealth; name: string; onClose: () => void; t: T;
  cost?: ConnectionCost; costs?: CostsResp; costsError: string;
}) {
  const fact = (label: string, value: React.ReactNode) => (
    <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
      <span style={{ fontSize: 11, color: K.muted2, textTransform: "uppercase", letterSpacing: "0.06em" }}>{label}</span>
      <span style={{ fontSize: 13, color: K.ink, overflowWrap: "anywhere" }}>{value || "—"}</span>
    </div>
  );
  const section = (title: string, children: React.ReactNode) => (
    <section style={{ display: "flex", flexDirection: "column", gap: 10, paddingTop: 16, borderTop: `1px solid ${K.line}` }}>
      <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600, color: K.ink, fontFamily: sans }}>{title}</h3>
      {children}
    </section>
  );
  return (
    <Drawer open onClose={onClose} title={t("drawerTitle", name)} width={640}>
      <div data-testid="conn-drawer" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <HealthBadge health={health} t={t} />
          <Badge tone={conn.enabled ? "success" : "neutral"} dot={false}>{conn.enabled ? t("on") : t("off")}</Badge>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 12 }}>
          {fact(t("fieldKey"), <span style={code}>{conn.kind === "bedrock" ? name : conn.connection_key}</span>)}
          {fact(t("fieldKind"), <span style={code}>{conn.kind} · {conn.auth_mode}</span>)}
          {fact(t("fieldScope"), <span style={code}>{conn.data_scope}</span>)}
          {conn.kind === "bedrock" && fact(t("fieldAccount"), <span style={code}>{name}</span>)}
          {conn.region && fact(t("fieldRegion"), <span style={code}>{conn.region}</span>)}
          {fact(t("colCredential"), <span style={code}>{credentialText(conn, t)}</span>)}
          {fact(t("fieldUpdated"), conn.updated_at ? <DateText value={conn.updated_at} withTime /> : null)}
        </div>
        {conn.policy_problems.length > 0 && (
          <div style={{ padding: "8px 12px", borderRadius: 8, background: K.dangerSoft, color: K.danger, fontSize: 12 }}>
            <strong>{t("problems")}:</strong> {conn.policy_problems.join("; ")}
          </div>
        )}
        {section(t("deployments"), <DeploymentsTable conn={conn} health={health} t={t} />)}
        {section(t("costsTitle"), (
          <CostPanel cost={cost} tolerance={costs?.tolerance ?? null} ready={!!costs?.schema_ready}
                     loadError={costsError} t={t} />
        ))}
        {section(t("editTitle"), (
          <SettingsForm key={`${conn.updated_at ?? ""}|${conn.cost_policy?.updated_at ?? ""}`} conn={conn} name={name} t={t} />
        ))}
        {section(t("testTitle"), <TestPanel conn={conn} name={name} t={t} />)}
      </div>
    </Drawer>
  );
}

// ── the tab ────────────────────────────────────────────────────────────────────────────────────────────────────

export function ConnectionsPanel() {
  const t = useT(CONNECTIONS);
  const toast = useToast();
  const qc = useQueryClient();
  const dir = useAwsAccounts();
  const accountText = useAccountText();
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [pending, setPending] = useState<{ conn: Connection; enabled: boolean } | null>(null);
  const [switchError, setSwitchError] = useState("");

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["llm-connections"],
    queryFn: () => apiGet<ConnectionsResp>("/api/admin/llm-connections", { admin: true }),
  });
  const avail = useQuery({
    queryKey: ["llm-availability", 24],
    queryFn: () => apiGet<AvailabilityHealth>("/api/admin/llm-availability?hours=24", { admin: true }),
    refetchInterval: 60_000,
  });
  const costs = useQuery({
    queryKey: ["llm-connection-costs"],
    queryFn: () => apiGet<CostsResp>("/api/admin/llm-connections/costs?days=14", { admin: true }),
    refetchInterval: 120_000,
  });
  const costOf = useMemo(() => {
    const m = new Map<string, ConnectionCost>();
    for (const c of costs.data?.connections ?? []) m.set(c.connection_key, c);
    return m;
  }, [costs.data]);
  const healthOf = useMemo(() => {
    const m = new Map<string, ConnectionHealth>();
    for (const h of avail.data?.connections ?? []) m.set(h.connection_key, h);
    return m;
  }, [avail.data]);
  const nameOf = useMemo(
    () => (c: Connection) => connectionName(c, dir, accountText),
    [dir, accountText],
  );

  const switchMut = useMutation({
    mutationFn: ({ conn, enabled }: { conn: Connection; enabled: boolean }) =>
      apiSend(`/api/admin/llm-connections/${encodeURIComponent(conn.connection_key)}`,
        { method: "PATCH", body: { enabled }, admin: true }),
    onSuccess: (_r, v) => {
      toast.success(t("switched", nameOf(v.conn), v.enabled));
      setPending(null);
      setSwitchError("");
      qc.invalidateQueries({ queryKey: ["llm-connections"] });
      qc.invalidateQueries({ queryKey: ["llm-availability"] });
    },
    onError: (e) => setSwitchError(e instanceof Error ? e.message : String(e)),
  });

  const rows = useMemo(() => data?.connections ?? [], [data]);

  const columns = useMemo<ColumnDef<Connection, unknown>[]>(() => [
    {
      accessorKey: "label",
      header: t("colConnection"),
      cell: (c) => (
        <div style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 600, color: K.ink }}>{c.row.original.label}</div>
          <div style={{ ...code, color: K.muted2 }}>{nameOf(c.row.original)}</div>
        </div>
      ),
    },
    {
      accessorKey: "kind",
      header: t("colKind"),
      cell: (c) => <span style={code}>{c.row.original.kind} · {c.row.original.data_scope}</span>,
    },
    {
      id: "credential",
      header: t("colCredential"),
      enableSorting: false,
      cell: (c) => <span style={code}>{credentialText(c.row.original, t)}</span>,
    },
    {
      id: "limits",
      header: t("colLimits"),
      enableSorting: false,
      cell: (c) => <span style={small}>{limitsText(c.row.original, t)}</span>,
    },
    {
      id: "health",
      header: t("colHealth"),
      enableSorting: false,
      cell: (c) => <HealthBadge health={healthOf.get(c.row.original.connection_key)} t={t} />,
    },
    {
      id: "budget",
      header: t("colBudget"),
      enableSorting: false,
      cell: (c) => <BudgetBadge budget={costOf.get(c.row.original.connection_key)?.budget} t={t} />,
    },
    {
      accessorKey: "enabled",
      header: t("colEnabled"),
      cell: (c) => {
        const r = c.row.original;
        const can = r.enabled || canSwitchOn(r);
        return (
          <span data-testid={`conn-enabled-${r.connection_key}`} onClick={(e) => e.stopPropagation()}>
            <Button
              size="sm"
              variant={r.enabled ? "primary" : "secondary"}
              ariaLabel={t("toggle", nameOf(r))}
              title={t("toggle", nameOf(r))}
              disabled={!can || switchMut.isPending}
              onClick={() => { setSwitchError(""); setPending({ conn: r, enabled: !r.enabled }); }}
            >
              {r.enabled ? <Check size={11} /> : <X size={11} />}
              {r.enabled ? t("on") : t("off")}
            </Button>
          </span>
        );
      },
    },
    {
      id: "actions",
      header: "",
      enableSorting: false,
      enableHiding: false,
      cell: (c) => (
        <span data-testid={`conn-details-${c.row.original.connection_key}`} onClick={(e) => e.stopPropagation()}>
          <Button size="sm" variant="secondary" onClick={() => setOpenKey(c.row.original.connection_key)}>
            {t("details")}
          </Button>
        </span>
      ),
    },
  ], [t, nameOf, healthOf, costOf, switchMut.isPending]);

  const open = rows.find((r) => r.connection_key === openKey) ?? null;

  if (data && !data.schema_ready) {
    return <p style={{ ...small, margin: 0 }}>{t("notReady")}</p>;
  }
  return (
    <div data-testid="llm-connections-tab" style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
      <p style={{ fontSize: 12, color: K.muted2, margin: 0 }}>{t("intro")}</p>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)" }}>
        <DataTable<Connection>
          data={rows}
          columns={columns}
          tableId="admin-llm-connections"
          getRowId={(r) => r.connection_key}
          loading={isLoading}
          error={isError ? (error instanceof Error ? error.message : t("loadError")) : null}
          onRetry={() => refetch()}
          onRowClick={(r) => setOpenKey(r.connection_key)}
          searchable
          searchPlaceholder={t("search")}
          pageSize={25}
          tableMinWidth={1100}
          emptyTitle={t("empty")}
        />
      </div>
      {open && (
        <ConnectionDrawer conn={open} health={healthOf.get(open.connection_key)} name={nameOf(open)}
                          onClose={() => setOpenKey(null)} t={t} cost={costOf.get(open.connection_key)}
                          costs={costs.data}
                          costsError={costs.isError ? (costs.error instanceof Error ? costs.error.message : t("costsLoadError")) : ""} />
      )}
      <ConfirmModal
        open={!!pending}
        onClose={() => { setPending(null); setSwitchError(""); }}
        onConfirm={() => pending && switchMut.mutate(pending)}
        title={pending ? (pending.enabled ? t("enableTitle", nameOf(pending.conn)) : t("disableTitle", nameOf(pending.conn))) : ""}
        body={pending?.enabled ? t("enableBody") : t("disableBody")}
        confirmLabel={pending?.enabled ? t("confirmOn") : t("confirmOff")}
        destructive={!pending?.enabled}
        busy={switchMut.isPending}
        error={switchError || null}
        testId="conn-switch-confirm"
      />
    </div>
  );
}
