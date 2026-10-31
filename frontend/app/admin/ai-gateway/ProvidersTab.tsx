"use client";
// app/admin/ai-gateway/ProvidersTab.tsx — AA-804 (UI part 1): the Providers tab of AI providers.
//
// One row per paid source from GET /api/admin/ai-gateway/providers (backend api/services/ai_gateway.py): every LLM
// connection, Jev, DataForSEO and Geoapify, in three kit Sections (LLM connections, Decisions, Data providers) above
// three summary tiles. Each row: status (+ why), health, spend today / this month, the budget (monthly per
// connection, the provider-wide daily cap), and the balance or today's credits. The overview is read-only.
// AA-804 UI part 2: two more views of the tab (`?view=`): Connections (ConnectionsPanel: switch a connection on or
// off, limits, monthly budget, test call; secret name and base URL read-only) and Jev credit (JevCreditPanel: the
// top-up ledger), both moved here from Settings. A Bedrock connection is named by its AWS account ID
// (lib/llmConnections connectionName + useAwsAccounts), never by a key.
// C4 (S262 wave 4): each row also says its access state, last probe and failure domain (registry v2, migration 322:
// from the row itself for Jev / DataForSEO / Geoapify, from GET /api/admin/llm-connections and the availability read
// for an LLM connection; a field the API does not send is not shown), and the two direct vendor APIs carry the
// "warm path" label (root ADR 0009 decision 2). Below the LLM connections: "Re-queue refused jobs" (POST
// /api/admin/job-runner/jobs/retry-llm-denied, ops.write): a dry run first, its numbers in a confirm dialog, then
// the real re-queue, one batch of 50 at a time (the server's apply cap), with "Check the next batch" after it.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw } from "lucide-react";
import { useMemo, useState } from "react";
import {
  Badge, Button, ConfirmModal, DataTable, DateText, ErrorState, K, KpiTile, Section, Tabs, apiGet, apiSend, mono,
  useT, useToast, type ColumnDef,
} from "../../_kit";
import {
  ERROR_CLASSES, PROVIDERS_API, PROVIDER_VIEWS, RETRY_LLM_DENIED_API, accessTone, groupRows, heldGroups,
  isAccessState, isReadOnlyRefusal, needsLook, nextRequeueSince, pct, registryFacts, requeueBody, statusTone,
  statusWordKey, usd, warmPathOf,
  type HeldGroup, type ProviderRow, type ProviderView, type ProvidersResp, type RegistryFacts,
  type RetryLlmDeniedResp,
} from "@/lib/aiGateway";
import { accountName } from "@/lib/awsAccounts";
import { connectionName } from "@/lib/llmConnections";
import { useAccountText, useAwsAccounts } from "../_components/useAwsAccounts";
import { ConnectionsPanel } from "./ConnectionsPanel";
import { JevCreditPanel } from "./JevCreditPanel";
import { ACCESS, M, REASON, STATUS } from "./messages";
import { useDomainText } from "./useDomainText";
import { useErrorClassText } from "./useErrorClassText";
import { useLlmConnections } from "./useLlmConnections";

export const PROVIDERS_KEY = ["ai-gateway", "providers"] as const;

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;

function Lines({ children }: { children: React.ReactNode }) {
  return <div style={{ display: "grid", gap: 2, minWidth: 0 }}>{children}</div>;
}

/** A provider row with the registry facts the overview found for it (C4). */
type Row = ProviderRow & { facts: RegistryFacts };

function ProviderCell({ row }: { row: Row }) {
  const t = useT(M);
  const dir = useAwsAccounts();
  const accountText = useAccountText();
  const domainText = useDomainText();
  const sub = row.group === "llm" && row.connection_key
    ? connectionName({ connection_key: row.connection_key, kind: row.provider, account_id: row.account_id },
      dir, accountText)
    : null;
  const warm = warmPathOf(row);
  // N4 (C4 polish): a connection without an account ID is named by its label in the failure domain, never its key
  const own = sub && sub !== row.connection_key ? sub : row.label;
  const domain = row.facts.failure_domain ? domainText(row.facts.failure_domain, row.group === "llm" ? own : null) : null;
  return (
    <Lines>
      <span style={{ fontWeight: 600, color: K.ink }}>{row.label}</span>
      {sub && <span style={{ fontFamily: mono, fontSize: 12, color: K.muted2 }}>{sub}</span>}
      <span style={small}>
        {row.data_scope === "aws_only" ? t("scopeAws") : row.data_scope === "vendor_direct" ? t("scopeVendor") : ""}
        {!row.enabled ? `${row.data_scope ? " · " : ""}${t("off")}` : ""}
      </span>
      {warm && (
        <span data-testid={`ai-gateway-warm-${row.key}`} title={t(warm === "anthropic" ? "warmAnthropicHelp" : "warmOpenaiHelp")}
          style={{ display: "inline-flex", marginTop: 2 }}>
          <Badge tone="purple" dot={false} style={{ whiteSpace: "nowrap" }}>
            {t(warm === "anthropic" ? "warmAnthropic" : "warmOpenai")}
          </Badge>
        </span>
      )}
      {domain && <span style={small} data-testid={`ai-gateway-domain-${row.key}`}>{t("failureDomain")} {domain}</span>}
    </Lines>
  );
}

function StatusCell({ row }: { row: Row }) {
  const t = useT(M);
  const ts = useT(STATUS);
  const tr = useT(REASON);
  const ta = useT(ACCESS);
  const access = row.facts.access_state;
  // F8: an LLM connection with no calls in the window says "No recent calls" (the Routing words); any other row,
  // and any row with a reason, keeps its own status (lib/aiGateway statusWordKey).
  const statusWord = ts(statusWordKey(row));
  return (
    <Lines>
      <span><Badge tone={statusTone(row.status)} style={{ whiteSpace: "nowrap" }}>{statusWord}</Badge></span>
      {row.status_reason && row.status_reason in REASON.en && (
        <span style={small}>{tr(row.status_reason as keyof typeof REASON.en)}</span>
      )}
      {access !== undefined && (
        <span style={{ ...small, color: accessTone(access) === "danger" ? K.danger : K.muted }}
          data-testid={`ai-gateway-access-${row.key}`}>
          {t("accessLabel")} {isAccessState(access) ? ta(access) : (access ?? ta("unknown"))}
        </span>
      )}
      {row.error && <span style={{ ...small, color: K.danger }}>{row.error}</span>}
    </Lines>
  );
}

function ProbeLine({ row }: { row: Row }) {
  const t = useT(M);
  if (row.facts.last_probe_at === undefined) return null;
  return row.facts.last_probe_at
    ? <span style={small}>{t("lastProbe")} <DateText value={row.facts.last_probe_at} relative /></span>
    : <span style={small}>{t("neverProbed")}</span>;
}

function HealthCell({ row }: { row: Row }) {
  const t = useT(M);
  const errorText = useErrorClassText();
  const h = row.health;
  if (!h) return <Lines><span style={small}>—</span><ProbeLine row={row} /></Lines>;
  if (row.group === "decide") {
    return (
      <Lines>
        <span style={small}>{t("jevCalls", h.calls ?? 0, h.errors ?? 0)}</span>
        {h.mean_latency_ms != null && <span style={small}>{t("meanLatency", h.mean_latency_ms)}</span>}
        <ProbeLine row={row} />
      </Lines>
    );
  }
  return (
    <Lines>
      {h.last_ok_at
        ? <span style={small}>{t("lastAnswer")} <DateText value={h.last_ok_at} relative /></span>
        : <span style={small}>{t("noCalls")}</span>}
      <ProbeLine row={row} />
      {h.last_error_class && (
        <span style={{ ...small, color: K.amber }}>{t("lastError", errorText(h.last_error_class) ?? "")}</span>
      )}
      {!!h.open_models && <span style={{ ...small, color: K.danger }}>{t("openModels", h.open_models)}</span>}
      {!h.open_models && !!h.failing_models && (
        <span style={{ ...small, color: K.amber }}>{t("failingModels", h.failing_models)}</span>
      )}
    </Lines>
  );
}

function BudgetCell({ row }: { row: Row }) {
  const t = useT(M);
  const m = row.monthly_budget;
  const d = row.daily_cap;
  if (!m && !d) return <span style={small}>{t("noCap")}</span>;
  return (
    <Lines>
      {m && (
        <span style={small}>
          {t("monthly")} <strong style={{ color: K.ink }}>{usd(m.month_spent_usd)}</strong>{" "}
          {t("ofCap", usd(m.monthly_budget_usd))} · {pct(m.used_pct)}
        </span>
      )}
      {d && (
        <span style={small}>
          {t("daily")} <strong style={{ color: K.ink }}>{usd(d.spent_today_usd)}</strong>{" "}
          {t("ofCap", usd(d.per_day_usd))} · {pct(d.used_pct)} · {d.hard_stop ? t("hardStop") : t("alertOnly")}
        </span>
      )}
    </Lines>
  );
}

function BalanceCell({ row }: { row: Row }) {
  const t = useT(M);
  const b = row.balance;
  const c = row.credits;
  if (c) {
    return (
      <Lines>
        <span style={{ fontFamily: mono, color: K.ink }}>{t("creditsToday", c.used_today, c.daily_limit)}</span>
        <span style={small}>{pct(c.used_pct)}</span>
      </Lines>
    );
  }
  if (!b) return <span style={small}>—</span>;
  if (b.usd == null) return <span style={small}>{t("notRead")}</span>;
  return (
    <Lines>
      <span>
        <strong style={{ fontFamily: mono, color: b.below_threshold ? K.danger : K.ink }}>{usd(b.usd, 2)}</strong>{" "}
        <span style={small}>{t("left")}{b.estimated ? ` (${t("estimated")})` : ""}</span>
      </span>
      <span style={small}>{t("threshold", usd(b.threshold_usd, 2))}</span>
      {b.as_of && <span style={small}>{t("asOf")} <DateText value={b.as_of} relative /></span>}
    </Lines>
  );
}

function SpendCell({ row, which }: { row: Row; which: "today" | "month" }) {
  const t = useT(M);
  const s = row.spend;
  if (!s) return <span style={small}>—</span>;
  if (which === "today") return <span style={{ fontFamily: mono }}>{usd(s.today_usd)}</span>;
  const live = row.live_calls_month;
  return (
    <Lines>
      <span style={{ fontFamily: mono, color: K.ink }}>{usd(s.month_usd)}</span>
      <span style={small}>{t("calls", s.month_calls)}{live != null ? ` · ${t("liveCalls", live)}` : ""}</span>
    </Lines>
  );
}

function ProviderTable({ id, rows, loading, empty }: {
  id: string; rows: Row[]; loading: boolean; empty?: string;
}) {
  const t = useT(M);
  const columns = useMemo<ColumnDef<Row, unknown>[]>(() => [
    { id: "provider", header: t("colProvider"), accessorKey: "label", cell: ({ row }) => <ProviderCell row={row.original} /> },
    { id: "status", header: t("colStatus"), accessorKey: "status", cell: ({ row }) => <StatusCell row={row.original} /> },
    { id: "health", header: t("colHealth"), enableSorting: false, cell: ({ row }) => <HealthCell row={row.original} /> },
    { id: "today", header: t("colToday"), accessorFn: (r) => r.spend?.today_usd ?? 0, meta: { align: "right" },
      cell: ({ row }) => <SpendCell row={row.original} which="today" /> },
    { id: "month", header: t("colMonth"), accessorFn: (r) => r.spend?.month_usd ?? 0, meta: { align: "right" },
      cell: ({ row }) => <SpendCell row={row.original} which="month" /> },
    { id: "budget", header: t("colBudget"), enableSorting: false, cell: ({ row }) => <BudgetCell row={row.original} /> },
    { id: "balance", header: t("colBalance"), enableSorting: false, cell: ({ row }) => <BalanceCell row={row.original} /> },
  ], [t]);
  return (
    <DataTable<Row>
      tableId={id} data={rows} columns={columns} getRowId={(r) => r.key} loading={loading}
      searchable={false} paginate={false} stickyHeader={false} tableMinWidth={980}
      emptyTitle={empty ?? "—"}
    />
  );
}

function ProvidersOverview() {
  const t = useT(M);
  const q = useQuery({
    queryKey: PROVIDERS_KEY,
    queryFn: () => apiGet<ProvidersResp>(PROVIDERS_API, { admin: true }),
    refetchInterval: 120_000,
    refetchIntervalInBackground: false,
  });
  // C4: the registry facts of the LLM connections (shared caches with the Connections view and Routing). Both reads
  // are optional: before migration 322 or on an error the lines are simply not shown.
  const conns = useLlmConnections();
  const avail = useQuery({
    queryKey: ["llm-availability", 24],
    queryFn: () => apiGet<{ connections?: { connection_key: string; failure_domain?: string | null }[] }>(
      "/api/admin/llm-availability?hours=24", { admin: true }),
    refetchInterval: 60_000,
  });
  const data = q.data;
  const groups = useMemo(() => {
    const withFacts = (data?.providers ?? []).map((r): Row => {
      if (r.group === "llm") {
        return { ...r, facts: registryFacts(r.connection_key, conns.data?.connections, avail.data?.connections) };
      }
      const facts: RegistryFacts = {};
      if ("failure_domain" in r) facts.failure_domain = r.failure_domain;
      if ("access_state" in r) facts.access_state = r.access_state;
      if ("last_probe_at" in r) facts.last_probe_at = r.last_probe_at;
      return { ...r, facts };
    });
    return groupRows(withFacts);
  }, [data, conns.data, avail.data]);
  const look = data ? needsLook(data.providers) : undefined;

  if (q.error) {
    return <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} onRetry={() => q.refetch()} />;
  }

  // minmax(0, 1fr): a DataTable with tableMinWidth must not widen the column (lessons: Frontend, S254).
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16 }}
      data-testid={data ? "ai-gateway-ready" : undefined}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(220px, 100%), 1fr))", gap: 12 }}>
        <KpiTile label={t("tileMonth")} value={data ? usd(data.totals.month_usd, 2) : undefined}
          hint={data ? t("tileMonthHint", data.month_start.slice(0, 10)) : undefined} />
        <KpiTile label={t("tileToday")} value={data ? usd(data.totals.today_usd, 2) : undefined}
          hint={t("tileTodayHint")} />
        <KpiTile label={t("tileLook")} value={look}
          valueTone={look ? "warning" : undefined}
          hint={data ? (look ? t("tileLookHint", data.providers.length) : t("tileAllOk")) : undefined} />
      </div>
      {data && data.errors.length > 0 && (
        <div role="status" style={{ ...small, color: K.danger }}>{t("sourceErrors")}</div>
      )}
      <Section title={t("groupLlm")} description={t("groupLlmHelp")} padded={false}>
        <ProviderTable id="ai-gateway-llm" rows={groups.llm} loading={!data} empty={t("emptyLlm")} />
        {data?.unattributed_llm && data.unattributed_llm.month_calls > 0 && (
          <div style={{ ...small, padding: "10px 16px", borderTop: `1px solid ${K.line}` }}>
            <strong style={{ color: K.ink }}>{t("unattributed")}</strong>{" "}
            <span style={{ fontFamily: mono }}>
              {usd(data.unattributed_llm.today_usd)} · {usd(data.unattributed_llm.month_usd)}
            </span>{" "}
            · {t("calls", data.unattributed_llm.month_calls)}. {t("unattributedHelp")}
          </div>
        )}
      </Section>
      <RequeueRefusedJobs />
      <Section title={t("groupDecide")} description={t("groupDecideHelp")} padded={false}>
        <ProviderTable id="ai-gateway-decide" rows={groups.decide} loading={!data} />
      </Section>
      <Section title={t("groupData")} description={t("groupDataHelp")} padded={false}>
        <ProviderTable id="ai-gateway-data" rows={groups.data} loading={!data} />
      </Section>
      <div style={small}>{t("traceHint")}</div>
    </div>
  );
}

// ── re-queue the jobs an LLM account block failed (AA-1018): dry run → confirm → real ──────────────────────────────
// One batch at a time (lib/aiGateway REQUEUE_BATCH = the server's apply cap): the dry run reads the same rows from the
// same `since` as the apply right after it, so "Re-queue N jobs" re-queues N. When more rows follow, "Check the next
// batch" runs a new dry run from where the last batch ended (nextRequeueSince).

/** One held group as people read it: connections named by account ID or vendor (never a key), classes in words. */
function useHeldText(): (g: HeldGroup) => string {
  const t = useT(M);
  const ta = useT(ACCESS);
  const errorText = useErrorClassText();
  const dir = useAwsAccounts();
  const accountText = useAccountText();
  const conns = useLlmConnections();
  return (g) => {
    const name = (key: string) => {
      const row = conns.data?.connections?.find((c) => c.connection_key === key);
      // a key the registry read did not return: accountName (account ID, vendor name or "Retired account"), never the key
      return row ? connectionName(row, dir, accountText) : accountName(key, dir, accountText);
    };
    const names = g.keys.length ? g.keys.map(name).join(", ") : t("requeueAConnection");
    const cls = g.classes.length
      ? g.classes.map((c) => (isAccessState(c) ? ta(c)
        : (ERROR_CLASSES as readonly string[]).includes(c) ? errorText(c) : t("requeueRefused")))
        .join(", ")
      : t("requeueRefused");
    switch (g.type) {
      case "connection": return t("requeueHeldConnection", g.jobs, names, cls);
      case "same_class": return t("requeueHeldSameClass", g.jobs, names, cls);
      case "kind_unknown": return t("requeueHeldKindUnknown", g.jobs, g.kind ?? "—");
      default: return t("requeueHeldKindRefuses", g.jobs, g.kind ?? "—");
    }
  };
}

/** What the last real re-queue did, shown under the card until the next check. */
interface LastRun { requeued: number; expected: number; next: string | null }

function RequeueRefusedJobs() {
  const t = useT(M);
  const toast = useToast();
  const qc = useQueryClient();
  const heldText = useHeldText();
  const [preview, setPreview] = useState<RetryLlmDeniedResp | null>(null);
  const [last, setLast] = useState<LastRun | null>(null);
  const [error, setError] = useState("");
  // N4 (C4 polish): the read-only E2E session gets 403 `e2e_read_only` on the dry run (a POST); say so in words
  const [readOnly, setReadOnly] = useState(false);
  const fail = (e: unknown) => {
    setReadOnly(isReadOnlyRefusal(e));
    setError(isReadOnlyRefusal(e) ? t("requeueReadOnly") : e instanceof Error ? e.message : String(e));
  };
  const run = (dryRun: boolean, since?: string | null) =>
    apiSend<RetryLlmDeniedResp>(RETRY_LLM_DENIED_API, { method: "POST", body: requeueBody(dryRun, since), admin: true });
  const dry = useMutation({
    mutationFn: (since: string | null) => run(true, since),
    onSuccess: (r) => { setError(""); setPreview(r); },
    onError: fail,
  });
  const real = useMutation({
    // the dry run's own window, so the apply reads the rows the dialog counted
    mutationFn: (p: RetryLlmDeniedResp) => run(false, p.since),
    onSuccess: (r, p) => {
      setPreview(null);
      setLast({ requeued: r.requeued, expected: p.eligible, next: nextRequeueSince(r) });
      toast.success(t("requeueDone", r.requeued));
      qc.invalidateQueries({ queryKey: ["llm-availability"] });
      qc.invalidateQueries({ queryKey: [...PROVIDERS_KEY] });
    },
    onError: fail,
  });
  const check = (since: string | null) => { setLast(null); dry.mutate(since); };
  const held = preview ? heldGroups(preview.held, preview.blocked_connections) : [];
  // nothing eligible in a full batch (held jobs fill it): the confirm button checks the next batch instead
  const nextFromPreview = preview && preview.eligible === 0 ? nextRequeueSince(preview) : null;
  const busy = dry.isPending || real.isPending;
  return (
    <Section title={t("requeueTitle")} description={t("requeueHelp")} testId="ai-gateway-requeue"
      actions={
        <Button size="sm" variant="secondary" disabled={busy} onClick={() => check(null)}
          testId="ai-gateway-requeue-dry-run">
          <RotateCcw size={13} /> {dry.isPending ? t("requeueChecking") : t("requeueButton")}
        </Button>
      }>
      {error
        ? readOnly
          ? <div role="status" style={small} data-testid="ai-gateway-requeue-read-only">{error}</div>
          : <div role="alert" style={{ ...small, color: K.danger }}>{error}</div>
        : last
          ? (
            <div style={{ display: "grid", gap: 6, justifyItems: "start" }} data-testid="ai-gateway-requeue-last">
              <span style={small}>{t("requeueLast", last.requeued, last.expected)}</span>
              {last.next && (
                <>
                  <span style={small}>{t("requeueLastMore")}</span>
                  <Button size="sm" variant="secondary" disabled={busy} onClick={() => check(last.next)}
                    testId="ai-gateway-requeue-next">
                    {t("requeueNext")}
                  </Button>
                </>
              )}
            </div>
          )
          : <div style={small}>{t("requeueIdle")}</div>}
      <ConfirmModal open={preview !== null} onClose={() => setPreview(null)} onConfirm={() => {
        if (preview && preview.eligible > 0) real.mutate(preview);
        else if (nextFromPreview) check(nextFromPreview);
        else setPreview(null);
      }}
        title={t("requeueConfirmTitle")} busy={busy} testId="ai-gateway-requeue-confirm"
        confirmLabel={preview && preview.eligible > 0 ? t("requeueConfirm", preview.eligible)
          : nextFromPreview ? t("requeueNext") : t("requeueNothing")}
        error={error || null}
        body={preview && (
          <div style={{ display: "grid", gap: 6, fontSize: 12.5, color: K.body }}>
            <span>{t("requeueMatched", preview.matched, preview.since.slice(0, 10))}</span>
            <span><strong style={{ color: K.ink }}>{t("requeueEligible", preview.eligible)}</strong></span>
            {preview.held_count > 0 && <span>{t("requeueHeld", preview.held_count)}</span>}
            {held.map((g) => (
              <span key={`${g.type}|${g.keys.join(",")}|${g.kind ?? ""}`} style={{ ...small, overflowWrap: "anywhere" }}>
                · {heldText(g)}
              </span>
            ))}
            {Object.keys(preview.blocked_connections).length > 0 && (
              <span style={small}>{t("requeueStillRefused", Object.keys(preview.blocked_connections).length)}</span>
            )}
            {preview.more && <span style={small}>{t("requeueMore", preview.limit)}</span>}
          </div>
        )} />
    </Section>
  );
}

const VIEW_LABEL = { overview: "viewOverview", connections: "viewConnections", "jev-credit": "viewJevCredit" } as const;

/** The Providers tab: the overview (default) or one of its two editing views, picked with underline tabs. */
export default function ProvidersTab({ view, onView }: { view: ProviderView; onView: (v: ProviderView) => void }) {
  const t = useT(M);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 16 }}>
      <Tabs variant="underline" ariaLabel={t("viewsLabel")} testId="ai-gateway-provider-views"
        tabs={PROVIDER_VIEWS.map((v) => ({ key: v, label: t(VIEW_LABEL[v]) }))}
        active={view} onChange={(k) => onView(k as ProviderView)} />
      {view === "overview" && <ProvidersOverview />}
      {view === "connections" && <ConnectionsPanel />}
      {view === "jev-credit" && <JevCreditPanel />}
    </div>
  );
}
