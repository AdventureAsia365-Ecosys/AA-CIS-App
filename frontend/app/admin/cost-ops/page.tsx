"use client";
// app/admin/cost-ops/page.tsx — AA-665 Cost Operations (was AA-622 "External Spend" at /admin/llm-usage, which now
// redirects here; ?tab= carries over). Real LLM + DataForSEO cost, viewable by TENANT and by ACCOUNT (both are
// first-class filter/group dimensions), model, stage; the AA-505/AA-617 LLM tree (account/fallback/tokens) + AA-618 DFS
// log. Tabs: Overview (+ AA-665 C3 month-end forecast vs budget, ForecastPanel), LLM, DataForSEO (+ C2 runs by job,
// DfsRunsPanel), AWS bill (the AA-623 Cost Explorer reconciliation), Jev (JevPanel, the Jev Decisions endpoints),
// Deep link: /admin/cost-ops?tab=<key>. AA-804 UI part 2: Budgets moved to AI providers › Budgets; the old
// `?tab=budgets` link is replaced in the browser by the new page (lib/aiGateway RETIRED_COST_OPS_TABS).
// AA-994: text in the UI language (messages.ts, panels.messages.ts); tenant labels, stages, roles, models, endpoints,
// markets and AWS service names from the API stay as sent.

import { Suspense, useState, useEffect, useCallback, useMemo, type CSSProperties } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { AlertTriangle, ChevronRight, ChevronDown, Cpu, Search, Wallet, Building2 } from "lucide-react";
import {
  ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid, Legend,
} from "recharts";
import DfsRunsPanel from "./DfsRunsPanel";
import ForecastPanel from "./ForecastPanel";
import JevPanel from "./JevPanel";
import { A, serif, sans, mono, Card, SLabel, Badge, LoadingScreen, StatCard, TH, TD, CHART_TOOLTIP } from "../_components/adminUi";
import { Button, Tabs, ScrollX, PageFrame, PageHeader, useFormat, useT } from "../../_kit";
import { useChartColors } from "../../_kit/useTheme";
import { useAccountText, useAwsAccounts } from "../_components/useAwsAccounts";
import {
  LIVE_ACCOUNTS, LLM_ACCOUNT, MAIN_ACCOUNT, RETIRED_ACCOUNT, accountLabel, accountName, isLiveAccount,
} from "@/lib/awsAccounts";
import { RETIRED_COST_OPS_TABS, retiredTabTarget } from "@/lib/aiGateway";
import { COST } from "./messages";

// ── Types ──────────────────────────────────────────────────────────────────

interface Branch {
  tenant_id: string | null;
  tenant_label: string;
  model: string;
  stage: string;
  role: string;
  account: string;
  provider: string;
  fallback_count: number;
  call_count: number;
  total_cost_usd: number;
  tokens_in_total: number;
  tokens_out_total: number;
  ok_count: number;
  ok_eligible_count: number;
  ok_rate: number | null;
  avg_atoms_extracted: number | null;
  avg_output_len_chars: number | null;
  truncated_count: number;
  last_call_at: string | null;
}

interface DfsBranch {
  endpoint: string;
  tenant_id: string | null;
  tenant_label: string;
  call_count: number;
  live_count: number;
  cache_hit_count: number;
  cache_hit_rate: number | null;
  total_cost_usd: number;
  keywords_total: number;
  last_call_at: string | null;
}
interface DfsSummary {
  total_calls: number; live_calls: number; cache_hits: number;
  total_cost_usd: number; cache_hit_rate: number | null;
}
interface DfsCountry {
  location_code: number | null; country: string;
  call_count: number; live_count: number; cache_hit_count: number;
  cache_hit_rate: number | null; total_cost_usd: number; keywords_total: number;
}
interface DailyPoint { day: string; source: "llm" | "dfs"; cost_usd: number; call_count: number; }
interface DfsBalance {  // AA-627 — latest stored DataForSEO account balance (not a live call)
  balance_usd: number | null; currency: string | null; threshold_usd: number;
  below_threshold: boolean; fetched_at: string | null; has_data: boolean;
}
interface CostExplorerAccount {  // AA-623 — one account's stored CE spend in the window
  // account_key: the internal key of a live account, or "retired" (AA-998 history), from the backend (AA-902);
  // null if unresolved. Shown as the account ID (lib/awsAccounts.ts), never as the key.
  account_id: string; account_key?: string | null; total_usd: number; bedrock_usd: number; other_usd: number;
}
interface CostExplorerService {
  account_id: string; account_key?: string | null; service: string; amount_usd: number; is_bedrock: boolean;
}
interface CostExplorerData {  // AA-623 — stored CE days for the page window (never a live call on page load)
  accounts: CostExplorerAccount[]; services: CostExplorerService[];
  total_usd: number | null; bedrock_usd: number | null; fetched_at: string | null; has_data: boolean;
  period_start: string; period_end_exclusive: string; covered_from?: string; covered_to?: string;
}
interface StageConfig {
  stage: string; role: string; provider: string; model_id: string; account_route: string | null;
}
interface CallRow {  // AA-622 fallback drill-down — GET /admin/llm-usage/calls
  id: string; tenant_id: string | null; stage: string; role: string; model: string;
  account: string | null; provider: string | null; fallback_used: boolean;
  tokens_in: number | null; tokens_out: number | null; cost_usd: number;
  stop_reason: string | null; created_at: string | null;
}

const ROLE_COLOR: Record<string, "gray" | "gold" | "green"> = { writer: "gold", judge: "green", validate: "gray" };
// AA-998: one LLM account. "retired" groups the older llm_call_log rows of the former fallback account (history
// only, shown without an ID). AWS accounts are named by their account ID (lib/awsAccounts.ts), never by the key.
const ACCOUNTS = [LLM_ACCOUNT, MAIN_ACCOUNT, "openai", "anthropic", "typesafe", RETIRED_ACCOUNT, "unknown"] as const;
const ACCOUNT_COLOR: Record<string, string> = {
  [LLM_ACCOUNT]: A.slate, [MAIN_ACCOUNT]: A.green, openai: A.ink3, anthropic: A.ink3, typesafe: A.accent,
  [RETIRED_ACCOUNT]: A.gold, unknown: A.muted,
};
// Buckets that are not an AWS account (message keys of messages.ts COST).
const OTHER_ACCOUNT_META: Record<
  string,
  {
    label: "acctOpenaiLong" | "acctAnthropicLong" | "acctJevLong" | "acctLegacyLong";
    short: "acctOpenai" | "acctAnthropic" | "acctJev" | "acctLegacy";
  }
> = {
  openai:  { label: "acctOpenaiLong", short: "acctOpenai" },
  anthropic: { label: "acctAnthropicLong", short: "acctAnthropic" },
  // AA-660 — Jev decisions (shared/llm_client/decide.py); details per stage in /admin/decisions.
  typesafe: { label: "acctJevLong", short: "acctJev" },
  unknown: { label: "acctLegacyLong", short: "acctLegacy" },
};

/** Account names in the UI language: `short` = the AWS account ID, "Retired account", or OpenAI / Jev / legacy;
 *  `long` = the name for the LLM estimate (every AWS line of it is Bedrock); `name` / `label` = lib/awsAccounts. */
function useAcctNames() {
  const t = useT(COST);
  const dir = useAwsAccounts();
  const text = useAccountText();
  return useMemo(() => ({
    short: (k: string) => (OTHER_ACCOUNT_META[k] ? t(OTHER_ACCOUNT_META[k].short) : accountName(k, dir, text)),
    long: (k: string) => {
      if (OTHER_ACCOUNT_META[k]) return t(OTHER_ACCOUNT_META[k].label);
      return isLiveAccount(k) ? t("acctBedrock", accountLabel(k, dir, text)) : t("acctRetiredBedrock", text.retired);
    },
    name: (k: string | null | undefined) => accountName(k, dir, text),
    label: (k: string | null | undefined) => accountLabel(k, dir, text),
  }), [t, dir, text]);
}
// AA-623 — Cost Explorer rows carry the same account key the token estimate uses (account_key, set by the backend
// since AA-902), so they line up with the estimated (token) breakdown. An unresolved account keeps its raw ID.
const ceKey = (r: { account_id: string; account_key?: string | null }) => r.account_key ?? r.account_id;
const ceName = (r: { account_id: string; account_key?: string | null }, name: (k: string) => string) =>
  r.account_key ? name(r.account_key) : r.account_id;
// Cost Explorer covers WHOLE accounts, so these labels must not say "Bedrock": the main account runs the
// infrastructure (ECS/RDS/ELB/ElastiCache...) and native Claude is blocked there — its Bedrock line is only
// Cohere Embed / Palmyra. accountLabel() adds the role ("main infrastructure" / "LLM").

// The `account` column is NULL for OpenAI calls (no AWS account) and for any row written before
// AA-617 added the column; both COALESCE to "unknown" in the backend. To keep OpenAI distinct from
// truly-unknown legacy rows, we key the account dimension off `provider` when account is unknown:
// provider="openai" -> "openai" bucket, otherwise "unknown". Real Bedrock rows carry a live account key; any other
// logged account is the AA-998 retired fallback account's history -> "retired". Gateway S6: a vendor-API row now
// stores its connection key ("openai-direct", "anthropic-direct") as the account; it is keyed by its provider too,
// never "retired".
function acctKey(b: { account: string; provider: string }): string {
  if (b.provider === "openai") return "openai";
  if (b.provider === "anthropic") return "anthropic";
  if (b.account === "unknown") {
    if (b.provider === "typesafe") return "typesafe";   // AA-660 — Jev
    return "unknown";
  }
  return LIVE_ACCOUNTS.includes(b.account) ? b.account : RETIRED_ACCOUNT;
}

function fmtUsd(n: number): string {
  return n < 0.01 && n > 0 ? `$${n.toFixed(6)}` : `$${n.toFixed(4)}`;
}
function fmtUsd2(n: number): string { return `$${n.toFixed(2)}`; }
function fmtInt(n: number): string { return n.toLocaleString("en-US"); }
function pct(n: number | null): string { return n == null ? "—" : `${Math.round(n * 100)}%`; }
function tenantKey(b: { tenant_id: string | null; tenant_label: string }): string {
  return b.tenant_id ?? b.tenant_label;  // platform/aa_internal rows have null id -> use label
}

const DAY_OPTIONS = [7, 30, 90];
const DATE_INPUT: CSSProperties = {
  fontSize: 12.5, padding: "4px 6px", borderRadius: 7, border: `1px solid ${A.line}`,
  background: A.card, color: A.ink2, fontFamily: sans,
};

// ── Shared quality cell ──────────────────────────────────────────────────────

function QualityCell({ b }: { b: Branch }) {
  const t = useT(COST);
  if (b.ok_eligible_count > 0) {
    const p = Math.round((b.ok_rate ?? 0) * 100);
    const color = p >= 80 ? A.green : p >= 50 ? A.amber : A.red;
    return (
      <span style={{ color, fontWeight: 600 }}>
        {p}% <span style={{ color: A.muted2, fontWeight: 400 }}>({b.ok_count}/{b.ok_eligible_count})</span>
      </span>
    );
  }
  if (b.avg_atoms_extracted != null) return <span style={{ color: A.ink3 }}>{t("atomsPerCall", b.avg_atoms_extracted.toFixed(1))}</span>;
  if (b.avg_output_len_chars != null) return <span style={{ color: A.ink3 }}>{t("charsPerCall", Math.round(b.avg_output_len_chars))}</span>;
  return <span style={{ color: A.muted2 }}>—</span>;
}

function AcctDot({ account }: { account: string }) {
  const names = useAcctNames();
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 11.5, color: A.muted }}>
      <span style={{ width: 7, height: 7, borderRadius: "50%", background: ACCOUNT_COLOR[account] ?? A.muted, flexShrink: 0 }} />
      {names.short(account)}
    </span>
  );
}

// ── Spend-by-tenant table (LLM) — the primary per-tenant view ──────────────

interface TenantRow {
  key: string; label: string; tenantId: string | null;
  cost: number; calls: number; tokens: number;
  fallback: number; truncated: number; okCount: number; okElig: number;
  byAccount: Record<string, number>;  // account -> cost
  models: Set<string>;
}

function buildTenantRows(branches: Branch[]): TenantRow[] {
  const m = new Map<string, TenantRow>();
  for (const b of branches) {
    const k = tenantKey(b);
    let r = m.get(k);
    if (!r) {
      r = { key: k, label: b.tenant_label, tenantId: b.tenant_id, cost: 0, calls: 0, tokens: 0, fallback: 0,
            truncated: 0, okCount: 0, okElig: 0, byAccount: {}, models: new Set() };
      m.set(k, r);
    }
    r.cost += b.total_cost_usd; r.calls += b.call_count;
    r.tokens += b.tokens_in_total + b.tokens_out_total;
    r.fallback += b.fallback_count; r.truncated += b.truncated_count;
    r.okCount += b.ok_count; r.okElig += b.ok_eligible_count;
    const ak = acctKey(b);
    r.byAccount[ak] = (r.byAccount[ak] ?? 0) + b.total_cost_usd;
    r.models.add(b.model);
  }
  return [...m.values()].sort((a, b) => b.cost - a.cost);
}

function TenantSpendTable({ branches, onFallbackClick }: {
  branches: Branch[];
  onFallbackClick: (tenantId: string | null, label: string) => void;
}) {
  const t = useT(COST);
  const names = useAcctNames();
  const rows = useMemo(() => buildTenantRows(branches), [branches]);
  const accountsPresent = useMemo(() => ACCOUNTS.filter(a => branches.some(b => acctKey(b) === a)), [branches]);
  if (rows.length === 0) return <div style={{ color: A.muted2, fontSize: 13, padding: "8px 0" }}>{t("noLlmInRange")}</div>;
  return (
    <ScrollX>
    <table style={{ width: "100%", borderCollapse: "collapse" }}>
      <thead><tr>
        <th style={TH}>{t("colTenant")}</th>
        <th style={{ ...TH, textAlign: "right" }}>{t("colCost")}</th>
        <th style={{ ...TH, textAlign: "right" }}>{t("colCalls")}</th>
        <th style={{ ...TH, textAlign: "right" }}>{t("colTokens")}</th>
        <th style={{ ...TH, textAlign: "right" }}>{t("colPerKTok")}</th>
        <th style={{ ...TH, textAlign: "right" }}>{t("colPass")}</th>
        <th style={{ ...TH, textAlign: "right" }}>{t("colFallback")}</th>
        {accountsPresent.map(a => <th key={a} style={{ ...TH, textAlign: "right" }}>{names.short(a)}</th>)}
      </tr></thead>
      <tbody>
        {rows.map(r => (
          <tr key={r.key}>
            <td style={{ ...TD }}>
              <span style={{ fontWeight: 600, color: A.ink2 }}>{r.label}</span>
              <span style={{ color: A.muted2, fontSize: 11, marginLeft: 6 }}>{t("models", r.models.size)}</span>
            </td>
            <td style={{ ...TD, textAlign: "right", fontFamily: mono, fontWeight: 600 }}>{fmtUsd(r.cost)}</td>
            <td style={{ ...TD, textAlign: "right" }}>{fmtInt(r.calls)}</td>
            <td style={{ ...TD, textAlign: "right" }}>{fmtInt(r.tokens)}</td>
            <td style={{ ...TD, textAlign: "right", fontFamily: mono, color: A.muted }}>
              {r.tokens > 0 ? fmtUsd((r.cost / r.tokens) * 1000) : "—"}
            </td>
            <td style={{ ...TD, textAlign: "right" }}>{r.okElig > 0 ? pct(r.okCount / r.okElig) : "—"}</td>
            <td style={{ ...TD, textAlign: "right" }}>
              {r.fallback > 0 ? (
                <button onClick={() => onFallbackClick(r.tenantId, r.label)} style={{
                  background: "none", border: "none", cursor: "pointer", color: A.amber,
                  fontFamily: sans, fontSize: 13, fontWeight: 600, textDecoration: "underline", padding: 0,
                }} title={t("viewFallback")}>
                  {pct(r.fallback / r.calls)}
                </button>
              ) : <span style={{ color: A.muted2 }}>{r.calls > 0 ? "0%" : "—"}</span>}
            </td>
            {accountsPresent.map(a => (
              <td key={a} style={{ ...TD, textAlign: "right", fontFamily: mono, color: r.byAccount[a] ? A.ink3 : A.muted2 }}>
                {r.byAccount[a] ? fmtUsd(r.byAccount[a]) : "—"}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
    </ScrollX>
  );
}

// ── LLM tree — leaf + generic collapsible group (works for either grouping order) ────────────

function StageLeaf({ b }: { b: Branch }) {
  const t = useT(COST);
  const { formatDateTime } = useFormat();
  const fbPct = b.call_count > 0 ? b.fallback_count / b.call_count : 0;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 0 8px 60px", borderBottom: `1px solid ${A.line2}` }}>
      <span style={{ fontFamily: mono, fontSize: 12, color: A.ink, minWidth: 150 }}>{b.stage}</span>
      <Badge color={ROLE_COLOR[b.role] ?? "gray"}>{b.role}</Badge>
      <AcctDot account={acctKey(b)} />
      <span style={{ fontSize: 12, color: A.muted2, minWidth: 110 }}>{b.tenant_label}</span>
      <span style={{ fontSize: 12, color: A.muted, minWidth: 60 }}>{fmtInt(b.call_count)}</span>
      <span style={{ fontSize: 12, color: A.ink2, minWidth: 84, fontFamily: mono }}>{fmtUsd(b.total_cost_usd)}</span>
      <span style={{ fontSize: 12, minWidth: 120 }}><QualityCell b={b} /></span>
      {b.fallback_count > 0 && <Badge color="amber">{t("fbBadge", Math.round(fbPct * 100))}</Badge>}
      {b.truncated_count > 0 && <Badge color="red">{t("truncBadge", b.truncated_count)}</Badge>}
      <span style={{ fontSize: 11, color: A.muted2, marginLeft: "auto" }}>
        {formatDateTime(b.last_call_at)}
      </span>
    </div>
  );
}

// mid-level node (model within account, or account within tenant, etc.)
function SubNode({ label, sublabel, branches, indent, dotColor }: {
  label: string; sublabel?: string; branches: Branch[]; indent: number; dotColor?: string;
}) {
  const t = useT(COST);
  const [open, setOpen] = useState(false);
  const cost = branches.reduce((s, b) => s + b.total_cost_usd, 0);
  const calls = branches.reduce((s, b) => s + b.call_count, 0);
  const tok = branches.reduce((s, b) => s + b.tokens_in_total + b.tokens_out_total, 0);
  const perK = tok > 0 ? (cost / tok) * 1000 : 0;
  return (
    <div>
      <button onClick={() => setOpen(o => !o)} style={{
        display: "flex", alignItems: "center", gap: 8, width: "100%",
        padding: `8px 0 8px ${indent}px`, background: "none", border: "none", cursor: "pointer",
        borderBottom: `1px solid ${A.line2}`, textAlign: "left",
      }}>
        {open ? <ChevronDown size={14} color={A.muted} /> : <ChevronRight size={14} color={A.muted} />}
        {dotColor && <span style={{ width: 8, height: 8, borderRadius: "50%", background: dotColor, flexShrink: 0 }} />}
        <span style={{ fontFamily: mono, fontSize: 12.5, color: A.ink2, fontWeight: 600 }}>{label}</span>
        {sublabel && <span style={{ fontSize: 11, color: A.muted2 }}>{sublabel}</span>}
        <span style={{ fontSize: 11.5, color: A.muted2 }}>{t("calls", fmtInt(calls))}</span>
        {tok > 0 && <span style={{ fontSize: 11, color: A.muted2, fontFamily: mono }}>{t("perK", fmtUsd(perK))}</span>}
        <span style={{ fontSize: 11.5, color: A.ink3, marginLeft: "auto", fontFamily: mono }}>{fmtUsd(cost)}</span>
      </button>
      {open && branches.map((b, i) => <StageLeaf key={`${b.stage}-${b.tenant_label}-${b.account}-${i}`} b={b} />)}
    </div>
  );
}

// top-level card group (account, or tenant, depending on the toggle)
function TopGroup({ label, sublabel, dotColor, branches, groupBy }: {
  label: string; sublabel: string; dotColor: string; branches: Branch[];
  groupBy: "account" | "tenant";
}) {
  const [open, setOpen] = useState(true);
  const t = useT(COST);
  const names = useAcctNames();
  const cost = branches.reduce((s, b) => s + b.total_cost_usd, 0);
  const calls = branches.reduce((s, b) => s + b.call_count, 0);
  // second-level key: if grouped by account -> break down by model; if by tenant -> by account
  const sub = useMemo(() => {
    const m = new Map<string, Branch[]>();
    for (const b of branches) {
      const k = groupBy === "account" ? b.model : acctKey(b);
      if (!m.has(k)) m.set(k, []);
      m.get(k)!.push(b);
    }
    return m;
  }, [branches, groupBy]);
  return (
    <Card style={{ padding: 0, overflow: "hidden" }}>
      <button onClick={() => setOpen(o => !o)} style={{
        display: "flex", alignItems: "center", gap: 10, width: "100%",
        padding: "14px 18px", background: "none", border: "none", cursor: "pointer", textAlign: "left",
      }}>
        {open ? <ChevronDown size={15} color={A.ink} /> : <ChevronRight size={15} color={A.ink} />}
        {dotColor && <span style={{ width: 9, height: 9, borderRadius: "50%", background: dotColor, flexShrink: 0 }} />}
        <span style={{ fontFamily: serif, fontSize: 14.5, color: A.ink, fontWeight: 500 }}>{label}</span>
        <span style={{ fontSize: 12, color: A.muted2 }}>{t("groupSub", sublabel, fmtInt(calls), sub.size, groupBy === "account")}</span>
        <span style={{ fontSize: 13, color: A.ink2, marginLeft: "auto", fontFamily: mono, fontWeight: 600 }}>{fmtUsd(cost)}</span>
      </button>
      {open && (
        <div style={{ padding: "0 18px 10px" }}>
          {[...sub.entries()].map(([k, bs]) => (
            <SubNode
              key={k}
              label={groupBy === "account" ? k : names.short(k)}
              dotColor={groupBy === "tenant" ? ACCOUNT_COLOR[k] : undefined}
              branches={bs}
              indent={38}
            />
          ))}
        </div>
      )}
    </Card>
  );
}

// ── Overview: horizontal bar ─────────────────────────────────────────────────

function Bar({ rows, total, colorOf }: {
  rows: { key: string; label: string; cost: number }[]; total: number; colorOf: (k: string) => string;
}) {
  const t = useT(COST);
  if (rows.length === 0) return <div style={{ color: A.muted2, fontSize: 13 }}>{t("noSpendYet")}</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {rows.map(r => {
        const w = total > 0 ? (r.cost / total) * 100 : 0;
        return (
          <div key={r.key}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
              <span style={{ color: A.ink2 }}>{r.label}</span>
              <span style={{ fontFamily: mono, color: A.ink3 }}>{fmtUsd(r.cost)} <span style={{ color: A.muted2 }}>({Math.round(w)}%)</span></span>
            </div>
            <div style={{ height: 8, background: A.line2, borderRadius: 999, overflow: "hidden" }}>
              <div style={{ width: `${w}%`, height: "100%", background: colorOf(r.key), borderRadius: 999 }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Fallback drill-down modal (AA-622) ───────────────────────────────────────
// Opened from a fallback% figure; fetches the actual calls that fell back
// (fallback_used=true) for the current tenant/day filter so admin can see which
// stage/model/account rotated and when — the "why is fallback high" answer.

function FallbackModal({ tenantId, tenantLabel, rangeQs, rangeLabel, onClose }: {
  tenantId: string | null; tenantLabel: string | null; rangeQs: string; rangeLabel: string; onClose: () => void;
}) {
  const t = useT(COST);
  const { formatDateTime } = useFormat();
  const [calls, setCalls] = useState<CallRow[] | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    const qs = new URLSearchParams(rangeQs);
    qs.set("fallback_used", "true");
    qs.set("limit", "200");
    if (tenantId) qs.set("tenant_id", tenantId);
    fetch(`/api/admin/llm-usage/calls?${qs.toString()}`)
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(d => setCalls(d.calls))
      .catch(() => setErr(t("loadFallbackError")));
  }, [tenantId, rangeQs, t]);
  return (
    <div onClick={onClose} style={{
      position: "fixed", inset: 0, background: "rgba(31,41,51,0.45)", zIndex: 1000,
      display: "flex", alignItems: "center", justifyContent: "center", padding: 24,
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        background: A.card, borderRadius: 12, width: "min(920px, 100%)", maxHeight: "80vh",
        overflow: "auto", boxShadow: "0 12px 40px rgba(0,0,0,0.2)",
      }}>
        <div style={{ display: "flex", alignItems: "center", padding: "16px 20px", borderBottom: `1px solid ${A.line}`, position: "sticky", top: 0, background: A.card }}>
          <div>
            <div style={{ fontFamily: serif, fontSize: 16, color: A.ink, fontWeight: 500 }}>{t("fallbackCalls")}</div>
            <div style={{ fontSize: 11.5, color: A.muted2 }}>
              {tenantLabel ? t("tenantIs", tenantLabel) : t("allTenants")} · {rangeLabel} · {t("fallbackSub")}
            </div>
          </div>
          <Button size="sm" variant="ghost" onClick={onClose} style={{ marginLeft: "auto" }}>{t("close")}</Button>
        </div>
        <div style={{ padding: "0 4px" }}>
          {err && <div style={{ color: A.red, fontSize: 13, padding: 24 }}>{err}</div>}
          {!err && calls == null && <LoadingScreen msg={t("loadingFallback")} />}
          {!err && calls != null && calls.length === 0 && (
            <div style={{ color: A.muted, fontSize: 13, padding: 32, textAlign: "center" }}>{t("noFallback")}</div>
          )}
          {!err && calls != null && calls.length > 0 && (
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr>
                <th style={TH}>{t("colWhen")}</th><th style={TH}>{t("colStage")}</th><th style={TH}>{t("colModel")}</th>
                <th style={TH}>{t("colAccount")}</th><th style={{ ...TH, textAlign: "right" }}>{t("colCost")}</th><th style={TH}>{t("colStop")}</th>
              </tr></thead>
              <tbody>
                {calls.map(c => (
                  <tr key={c.id}>
                    <td style={{ ...TD, fontSize: 12, color: A.muted }}>{formatDateTime(c.created_at)}</td>
                    <td style={{ ...TD, fontFamily: mono, fontSize: 12 }}>{c.stage}</td>
                    <td style={{ ...TD, fontFamily: mono, fontSize: 12 }}>{c.model}</td>
                    <td style={{ ...TD }}><AcctDot account={acctKey({ account: c.account ?? "unknown", provider: c.provider ?? "" })} /></td>
                    <td style={{ ...TD, textAlign: "right", fontFamily: mono }}>{fmtUsd(c.cost_usd)}</td>
                    <td style={{ ...TD, fontSize: 12, color: A.muted2 }}>{c.stop_reason ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════ PAGE ═══════════════════════════════════════════════

const TABS = [
  { key: "overview", label: "tabOverview" }, { key: "llm", label: "tabLlm" }, { key: "dfs", label: "tabDfs" },
  { key: "aws", label: "tabAws" }, { key: "jev", label: "tabJev" },
] as const;
// Tabs that load their own data (the page's spend fetch, its loading and error states do not apply).
const SELF_LOADING_TABS = new Set(["jev"]);

export default function CostOperationsPage() {
  // useSearchParams() needs a Suspense boundary in the App Router.
  return (
    <Suspense fallback={null}>
      <CostOperationsRouter />
    </Suspense>
  );
}

/** A retired tab's link goes to its new page (replace: no history entry for the old URL); any other renders. */
function CostOperationsRouter() {
  const router = useRouter();
  const target = retiredTabTarget(RETIRED_COST_OPS_TABS, useSearchParams().get("tab"));
  useEffect(() => {
    if (target) router.replace(target);
  }, [target, router]);
  return target ? null : <CostOperationsInner />;
}

function CostOperationsInner() {
  const t = useT(COST);
  const { formatDate, formatDateTime } = useFormat();
  const names = useAcctNames();
  const wanted = useSearchParams().get("tab");
  const [days, setDays] = useState(30);
  const [tab, setTab] = useState<string>(() => (TABS.some(x => x.key === wanted) ? wanted! : "overview"));
  const tabs = useMemo(() => TABS.map(x => ({ key: x.key, label: t(x.label) })), [t]);
  const [branches, setBranches] = useState<Branch[] | null>(null);
  const [dfs, setDfs] = useState<{ summary: DfsSummary; branches: DfsBranch[] } | null>(null);
  const [countries, setCountries] = useState<DfsCountry[] | null>(null);
  const [daily, setDaily] = useState<DailyPoint[] | null>(null);
  const [configs, setConfigs] = useState<StageConfig[] | null>(null);
  const [dfsBalance, setDfsBalance] = useState<DfsBalance | null>(null);  // AA-627
  const [costExplorer, setCostExplorer] = useState<CostExplorerData | null>(null);  // AA-623
  const [ceChecking, setCeChecking] = useState(false);  // AA-623 — manual "Refresh from AWS" in flight
  const [ceCheckMsg, setCeCheckMsg] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  // filters (apply to LLM + DFS)
  const [tenantFilter, setTenantFilter] = useState<string | null>(null);
  const [acctFilter, setAcctFilter] = useState<string | null>(null);
  const [groupBy, setGroupBy] = useState<"account" | "tenant">("tenant");
  // AA-622 fallback drill-down: {tenantId, label} when open (tenantId null = all tenants)
  const [fbModal, setFbModal] = useState<{ tenantId: string | null; label: string | null } | null>(null);

  // AA-601 part B — recharts stroke/fill are SVG attributes; resolve the theme colours to concrete
  // values for portability (Chromium resolves var() in SVG attrs, other engines/exports do not).
  const chart = useChartColors({
    gold: "--aa-gold", green: "--aa-green", line: "--aa-line", line2: "--aa-line2", muted: "--aa-muted",
  });

  // One time window for the whole page (LLM, DFS, trend, fallback drill-down AND Cost Explorer):
  // a rolling preset, or a custom from/to date range (UTC calendar days, "to" inclusive) — e.g.
  // "from the day X shipped". Both sides of the AWS-actual vs estimated comparison use it.
  // `days` of a custom window = its start to today (the Jev summary takes a rolling day count), set when applied.
  const [custom, setCustom] = useState<{ start: string; end: string; days: number } | null>(null);
  const [draftStart, setDraftStart] = useState("");
  const [draftEnd, setDraftEnd] = useState("");
  const rangeQs = custom ? `start=${custom.start}&end=${custom.end}` : `days=${days}`;
  const rangeLabel = custom ? `${custom.start} → ${custom.end}` : t("lastDays", days);
  const windowDays = custom ? custom.days : days;
  // Days the window spans (a custom window: start to end, inclusive), for the DFS runs time match (max 31 days).
  const windowSpanDays = custom
    ? Math.round((Date.parse(`${custom.end}T00:00:00Z`) - Date.parse(`${custom.start}T00:00:00Z`)) / 86400000) + 1
    : days;
  const applyCustom = () => {
    if (!draftStart) return;
    const end = draftEnd || new Date().toISOString().slice(0, 10);
    if (draftStart > end) return;
    const sinceStart = Math.ceil((Date.now() - new Date(`${draftStart}T00:00:00Z`).getTime()) / 86400000);
    setCustom({ start: draftStart, end, days: Math.max(1, sinceStart) });
  };

  const load = useCallback((qs: string) => {
    setLoading(true);
    Promise.all([
      fetch(`/api/admin/llm-usage/tree?${qs}`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
      fetch(`/api/admin/dfs-usage?${qs}`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
      fetch(`/api/admin/dfs-usage/by-country?${qs}`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
      fetch(`/api/admin/spend/daily?${qs}`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
      fetch(`/api/admin/llm-config`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
      // AA-627 — latest stored DFS balance (reads the daily snapshot, never a live DFS call).
      // Tolerate failure (older deploys / empty table) so the whole page still loads.
      fetch(`/api/admin/dfs-balance`).then(r => r.ok ? r.json() : null).catch(() => null),
      // AA-623 — stored AWS Cost Explorer days for the same window (reads only, never a live CE
      // call on page load — CE pricing costs ~$0.01/request). Same tolerate-failure pattern as dfs-balance.
      fetch(`/api/admin/cost-explorer?${qs}`).then(r => r.ok ? r.json() : null).catch(() => null),
    ])
      .then(([tree, dfsData, byCountry, dailyData, cfg, balance, ce]) => {
        setBranches(tree.branches);
        setDfs({ summary: dfsData.summary, branches: dfsData.branches });
        setCountries(byCountry.countries);
        setDaily(dailyData.points);
        setConfigs(cfg.stages);
        setDfsBalance(balance ?? null);
        setCostExplorer(ce ?? null);
        setError("");
      })
      .catch(() => setError(t("loadError")))
      .finally(() => setLoading(false));
  }, [t]);

  // AA-623 — manually trigger a fresh AWS Cost Explorer fetch (admin-secret gated on the BFF
  // proxy), then reload the stored snapshot. Mirrors the loading-flag + inline-message pattern
  // other admin pages use for POST actions (e.g. brand/page.tsx's activate button).
  const runCostExplorerCheck = useCallback(() => {
    setCeChecking(true);
    setCeCheckMsg("");
    fetch(`/api/admin/cost-explorer/check?${rangeQs}`, { method: "POST" })
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then((result: { row_count: number; errors: Record<string, string> }) => {
        const errCount = Object.keys(result.errors || {}).length;
        setCeCheckMsg(
          errCount > 0
            ? t("ceFetchedErrors", result.row_count, errCount)
            : t("ceFetched", result.row_count)
        );
        return fetch(`/api/admin/cost-explorer?${rangeQs}`).then(r => r.ok ? r.json() : null).catch(() => null);
      })
      .then((ce) => setCostExplorer(ce ?? null))
      .catch(() => setCeCheckMsg(t("ceRefreshFailed")))
      .finally(() => setCeChecking(false));
  }, [rangeQs, t]);
  // eslint-disable-next-line react-hooks/set-state-in-effect -- initial + range-change fetch, same pattern as every admin page
  useEffect(() => { load(rangeQs); }, [rangeQs, load]);

  const allBranches = useMemo(() => branches ?? [], [branches]);
  const allDfs = useMemo(() => dfs?.branches ?? [], [dfs]);

  // tenant options built from BOTH sources so a DFS-only tenant still appears
  const tenantOptions = useMemo(() => {
    const m = new Map<string, string>();
    for (const b of allBranches) m.set(tenantKey(b), b.tenant_label);
    for (const b of allDfs) m.set(tenantKey(b), b.tenant_label);
    return [...m.entries()].map(([key, label]) => ({ key, label })).sort((a, b) => a.label.localeCompare(b.label));
  }, [allBranches, allDfs]);
  const acctOptions = useMemo(() => ACCOUNTS.filter(a => allBranches.some(b => acctKey(b) === a)), [allBranches]);

  // filtered views
  const llm = useMemo(() => allBranches.filter(b =>
    (!tenantFilter || tenantKey(b) === tenantFilter) && (!acctFilter || acctKey(b) === acctFilter)
  ), [allBranches, tenantFilter, acctFilter]);
  const dfsRows = useMemo(() => allDfs.filter(b =>
    !tenantFilter || tenantKey(b) === tenantFilter
  ), [allDfs, tenantFilter]);

  // aggregates (respect filters)
  const llmCost = llm.reduce((s, b) => s + b.total_cost_usd, 0);
  const llmCalls = llm.reduce((s, b) => s + b.call_count, 0);
  const llmTokens = llm.reduce((s, b) => s + b.tokens_in_total + b.tokens_out_total, 0);
  const llmFallback = llm.reduce((s, b) => s + b.fallback_count, 0);
  const llmTruncated = llm.reduce((s, b) => s + b.truncated_count, 0);
  const okElig = llm.reduce((s, b) => s + b.ok_eligible_count, 0);
  const okCount = llm.reduce((s, b) => s + b.ok_count, 0);
  const dfsCost = dfsRows.reduce((s, b) => s + b.total_cost_usd, 0);
  const dfsCalls = dfsRows.reduce((s, b) => s + b.call_count, 0);
  const dfsLive = dfsRows.reduce((s, b) => s + b.live_count, 0);
  const dfsCacheHits = dfsRows.reduce((s, b) => s + b.cache_hit_count, 0);
  const dfsCacheRate = dfsCalls > 0 ? dfsCacheHits / dfsCalls : null;
  const totalSpend = llmCost + dfsCost;

  // Overview breakdowns
  const spendByAccount = useMemo(() => {
    const m = new Map<string, number>();
    for (const b of llm) { const ak = acctKey(b); m.set(ak, (m.get(ak) ?? 0) + b.total_cost_usd); }
    const rows = [...m.entries()].map(([key, cost]) => ({ key, label: names.long(key), cost }));
    if (dfsCost > 0) rows.push({ key: "dfs", label: t("dfsThirdParty"), cost: dfsCost });
    return rows.sort((a, b) => b.cost - a.cost);
  }, [llm, dfsCost, names, t]);
  // AA-623 — Cost Explorer per account, Bedrock vs infra, next to the token estimate for the same
  // account. Only Bedrock-on-AWS estimate is comparable: OpenAI/legacy rows never hit an AWS bill.
  const ceByAccount = useMemo(() => {
    const estByAcct = new Map<string, number>();
    for (const b of allBranches) {
      const k = acctKey(b);
      estByAcct.set(k, (estByAcct.get(k) ?? 0) + b.total_cost_usd);
    }
    return (costExplorer?.accounts ?? []).map(a => {
      const key = ceKey(a);
      // The account ID with its role; the retired account without its ID (AA-998); an unresolved key its raw ID.
      const label = a.account_key ? names.label(a.account_key) : a.account_id;
      return { ...a, key, label, estimated: estByAcct.get(key) ?? 0 };
    });
  }, [costExplorer, allBranches, names]);
  const bedrockEstimated = ceByAccount.reduce((s, r) => s + r.estimated, 0);
  const ceTopServices = useMemo(() => (costExplorer?.services ?? []).slice(0, 10), [costExplorer]);
  // Stored CE days only cover windows someone clicked "Refresh from AWS" for — flag gaps.
  const ceGap = useMemo(() => {
    if (!costExplorer?.has_data || !costExplorer.covered_from || !costExplorer.covered_to) return null;
    const lastWanted = new Date(new Date(costExplorer.period_end_exclusive).getTime() - 86400000).toISOString().slice(0, 10);
    const missingStart = costExplorer.covered_from > costExplorer.period_start;
    // CE finalizes a day ~24h late, so the day before the latest fetch is the most we can expect.
    const dayBeforeFetch = costExplorer.fetched_at
      ? new Date(new Date(costExplorer.fetched_at).getTime() - 86400000).toISOString().slice(0, 10)
      : lastWanted;
    const missingEnd = costExplorer.covered_to < (lastWanted < dayBeforeFetch ? lastWanted : dayBeforeFetch);
    return missingStart || missingEnd ? `${costExplorer.covered_from} → ${costExplorer.covered_to}` : null;
  }, [costExplorer]);
  const spendByTenant = useMemo(() => {
    const m = new Map<string, { label: string; cost: number }>();
    for (const b of llm) { const k = tenantKey(b); const c = m.get(k) ?? { label: b.tenant_label, cost: 0 }; c.cost += b.total_cost_usd; m.set(k, c); }
    for (const b of dfsRows) { const k = tenantKey(b); const c = m.get(k) ?? { label: b.tenant_label, cost: 0 }; c.cost += b.total_cost_usd; m.set(k, c); }
    return [...m.entries()].map(([key, v]) => ({ key, label: v.label, cost: v.cost })).sort((a, b) => b.cost - a.cost);
  }, [llm, dfsRows]);
  const stageRanking = useMemo(() => {
    const m = new Map<string, { cost: number; calls: number }>();
    for (const b of llm) { const c = m.get(b.stage) ?? { cost: 0, calls: 0 }; c.cost += b.total_cost_usd; c.calls += b.call_count; m.set(b.stage, c); }
    return [...m.entries()].map(([stage, v]) => ({ stage, ...v })).sort((a, b) => b.cost - a.cost).slice(0, 10);
  }, [llm]);

  // LLM tree grouped by the toggle
  const llmTree = useMemo(() => {
    const m = new Map<string, Branch[]>();
    for (const b of llm) {
      const k = groupBy === "account" ? acctKey(b) : tenantKey(b);
      if (!m.has(k)) m.set(k, []);
      m.get(k)!.push(b);
    }
    // sort groups by cost desc
    return [...m.entries()].sort((a, b) =>
      b[1].reduce((s, x) => s + x.total_cost_usd, 0) - a[1].reduce((s, x) => s + x.total_cost_usd, 0));
  }, [llm, groupBy]);

  const trend = useMemo(() => {
    const m = new Map<string, { day: string; llm: number; dfs: number }>();
    for (const p of daily ?? []) { const row = m.get(p.day) ?? { day: p.day, llm: 0, dfs: 0 }; row[p.source] = p.cost_usd; m.set(p.day, row); }
    return [...m.values()].sort((a, b) => a.day.localeCompare(b.day)).map(r => ({ ...r, label: r.day.slice(5) }));
  }, [daily]);

  // DFS per-tenant + per-endpoint tables
  const dfsByTenant = useMemo(() => {
    const m = new Map<string, { label: string; cost: number; calls: number; live: number; cache: number; kw: number }>();
    for (const b of dfsRows) {
      const k = tenantKey(b); const r = m.get(k) ?? { label: b.tenant_label, cost: 0, calls: 0, live: 0, cache: 0, kw: 0 };
      r.cost += b.total_cost_usd; r.calls += b.call_count; r.live += b.live_count; r.cache += b.cache_hit_count; r.kw += b.keywords_total;
      m.set(k, r);
    }
    return [...m.values()].sort((a, b) => b.cost - a.cost);
  }, [dfsRows]);

  const tenantLabelOf = (k: string | null) => k == null ? null : (tenantOptions.find(t => t.key === k)?.label ?? k);
  const filterActive = tenantFilter || acctFilter;

  return (
      <PageFrame>
        <PageHeader
          title={t("title")}
          description={t("description")}
          actions={
            <>
              {DAY_OPTIONS.map(d => (
                <Button key={d} size="sm" variant={!custom && days === d ? "primary" : "secondary"}
                     onClick={() => { setCustom(null); setDays(d); }}>{t("days", d)}</Button>
              ))}
              <span style={{ width: 1, height: 20, background: A.line, margin: "0 4px" }} />
              <input type="date" value={draftStart} onChange={e => setDraftStart(e.target.value)} aria-label={t("fromUtc")}
                     style={{ ...DATE_INPUT, borderColor: custom ? A.ink3 : A.line }} />
              <span style={{ fontSize: 12, color: A.muted }}>→</span>
              <input type="date" value={draftEnd} onChange={e => setDraftEnd(e.target.value)} aria-label={t("toUtc")}
                     style={{ ...DATE_INPUT, borderColor: custom ? A.ink3 : A.line }} />
              <Button size="sm" variant={custom ? "primary" : "secondary"} onClick={applyCustom}
                   disabled={!draftStart || (!!draftEnd && draftStart > draftEnd)}>{t("apply")}</Button>
            </>
          }
        />

        {/* Filters — tenant + account, apply to all tabs */}
        <Card style={{ marginBottom: 16, padding: "12px 16px" }}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 16, alignItems: "center" }}>
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <Building2 size={14} color={A.muted} />
              <span style={{ fontSize: 12, color: A.muted, marginRight: 4 }}>{t("tenant")}</span>
              <select value={tenantFilter ?? ""} onChange={e => setTenantFilter(e.target.value || null)}
                style={{ fontSize: 12.5, padding: "5px 8px", borderRadius: 7, border: `1px solid ${A.line}`, background: A.card, color: A.ink2, fontFamily: sans, cursor: "pointer" }}>
                <option value="">{t("allTenants")}</option>
                {tenantOptions.map(o => <option key={o.key} value={o.key}>{o.label}</option>)}
              </select>
            </div>
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <span style={{ fontSize: 12, color: A.muted, marginRight: 2 }}>{t("account")}</span>
              <Button size="sm" variant={acctFilter === null ? "primary" : "secondary"} onClick={() => setAcctFilter(null)}>{t("all")}</Button>
              {acctOptions.map(a => (
                <Button key={a} size="sm" variant={acctFilter === a ? "primary" : "secondary"} onClick={() => setAcctFilter(a)}>{names.short(a)}</Button>
              ))}
            </div>
            {filterActive && (
              <Button size="sm" variant="ghost" onClick={() => { setTenantFilter(null); setAcctFilter(null); }}>{t("clear")}</Button>
            )}
          </div>
        </Card>

        <div style={{ marginBottom: 20 }}>
          <Tabs tabs={tabs} active={tab} onChange={setTab} />
        </div>

        {/* AA-627 — low DFS balance banner (all tabs). The daily check writes the snapshot; this
            reflects the latest stored value, not a live call. */}
        {dfsBalance?.has_data && dfsBalance.below_threshold && (
          <Card style={{ marginBottom: 16, padding: "12px 16px", background: A.redTint, border: `1px solid ${A.red}` }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, color: A.red, fontSize: 13.5, fontWeight: 600 }}>
              <AlertTriangle size={16} style={{ flexShrink: 0 }} />
              <span>
                {t("dfsLowA", fmtUsd2(dfsBalance.balance_usd ?? 0), fmtUsd2(dfsBalance.threshold_usd))}
                {dfsBalance.fetched_at && (
                  <span style={{ fontWeight: 400, color: A.muted, marginLeft: 6 }}>
                    {t("asOf", formatDateTime(dfsBalance.fetched_at))}
                  </span>
                )}
              </span>
            </div>
          </Card>
        )}

        {/* AA-665 C1 — Jev spend and credit from the Jev Decisions endpoints; the page's window (max 90 days) applies. */}
        {tab === "jev" && <JevPanel days={windowDays} />}

        {!SELF_LOADING_TABS.has(tab) && loading && <LoadingScreen msg={t("loadingSpend")} />}
        {!SELF_LOADING_TABS.has(tab) && !loading && error && (
          <Card style={{ textAlign: "center", padding: 40 }}>
            <div style={{ color: A.red, marginBottom: 12 }}>{error}</div>
            <Button variant="secondary" onClick={() => load(rangeQs)}>{t("retry")}</Button>
          </Card>
        )}

        {!SELF_LOADING_TABS.has(tab) && !loading && !error && (
          <>
            {/* ═══════════ OVERVIEW ═══════════ */}
            {tab === "overview" && (
              <>
                {/* AA-665 C3 — this month's forecast vs budget (its own fetch; the date window does not apply). */}
                <ForecastPanel />
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 14, marginBottom: 20 }}>
                  <StatCard label={t("totalSpend")} value={fmtUsd2(totalSpend)} sub={t("totalSpendSub", rangeLabel, !!filterActive)} icon={<Wallet size={16} />} />
                  <StatCard label={t("llmCost")} value={fmtUsd2(llmCost)} sub={t("callsTok", fmtInt(llmCalls), fmtInt(llmTokens))} accent={A.gold} icon={<Cpu size={16} />} />
                  <StatCard label={t("dfsCostLong")} value={fmtUsd2(dfsCost)} sub={t("callsCache", fmtInt(dfsCalls), pct(dfsCacheRate))} accent={A.green} icon={<Search size={16} />} />
                  <StatCard label={t("tenantsActive")} value={fmtInt(spendByTenant.length)} sub={t("fbTrunc", fmtInt(llmFallback), fmtInt(llmTruncated))} accent={A.accent} icon={<Building2 size={16} />} />
                </div>

                <Card style={{ marginBottom: 20 }}>
                  <SLabel>{t("trend")}</SLabel>
                  {trend.length === 0 ? (
                    <div style={{ color: A.muted2, fontSize: 13, padding: "30px 0", textAlign: "center" }}>{t("noSpendIn", rangeLabel)}</div>
                  ) : (
                    <ResponsiveContainer width="100%" height={240}>
                      <AreaChart data={trend} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke={chart.line2} vertical={false} />
                        <XAxis dataKey="label" tick={{ fontSize: 11, fill: chart.muted }} stroke={chart.line} />
                        <YAxis tick={{ fontSize: 11, fill: chart.muted }} stroke={chart.line} tickFormatter={(v) => `$${v}`} />
                        <Tooltip {...CHART_TOOLTIP} formatter={(v) => fmtUsd(Number(v) || 0)} />
                        <Legend wrapperStyle={{ fontSize: 12 }} />
                        <Area type="monotone" dataKey="llm" name="LLM" stackId="1" stroke={chart.gold} fill={chart.gold} fillOpacity={0.33} />
                        <Area type="monotone" dataKey="dfs" name="DataForSEO" stackId="1" stroke={chart.green} fill={chart.green} fillOpacity={0.33} />
                      </AreaChart>
                    </ResponsiveContainer>
                  )}
                  <div style={{ fontSize: 11, color: A.muted2, marginTop: 6 }}>{t("trendNote")}</div>
                </Card>

                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 320px), 1fr))", gap: 20, marginBottom: 20 }}>
                  <Card>
                    <SLabel>{t("spendByTenant")}</SLabel>
                    <Bar rows={spendByTenant} total={spendByTenant.reduce((s, r) => s + r.cost, 0)} colorOf={() => A.accent} />
                  </Card>
                  <Card>
                    <SLabel>{t("spendByAccount")}</SLabel>
                    <Bar rows={spendByAccount} total={totalSpend} colorOf={(k) => k === "dfs" ? A.green : (ACCOUNT_COLOR[k] ?? A.muted)} />
                  </Card>
                </div>

                <Card style={{ marginBottom: 20 }}>
                  <SLabel>{t("topStages")}</SLabel>
                  {stageRanking.length === 0 ? (
                    <div style={{ color: A.muted2, fontSize: 13 }}>{t("noLlmYet")}</div>
                  ) : (
                    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                      {stageRanking.map(s => (
                        <div key={s.stage} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
                          <span style={{ fontFamily: mono, color: A.ink2, minWidth: 160 }}>{s.stage}</span>
                          <span style={{ color: A.muted2 }}>{t("calls", fmtInt(s.calls))}</span>
                          <span style={{ marginLeft: "auto", fontFamily: mono, color: A.ink3 }}>{fmtUsd(s.cost)}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </Card>

                <Card dark>
                  <SLabel light>{t("reconcile")}</SLabel>
                  <div style={{ fontSize: 12.5, color: "rgba(255,255,255,0.75)", lineHeight: 1.6 }}>
                    {t("reconcileA")} <code style={{ fontFamily: mono }}>llm_call_log</code> + <code style={{ fontFamily: mono }}>dfs_call_log</code>{" "}
                    {t("reconcileB")}<b>{names.name(LLM_ACCOUNT)}</b>{t("reconcileC")}{names.name(MAIN_ACCOUNT)}{t("reconcileD")}
                  </div>
                </Card>
              </>
            )}

            {/* ═══════════ AWS BILL (AA-665 C1: the AA-623 Cost Explorer panel, moved off the Overview) ═══════════ */}
            {tab === "aws" && (
              <>
                {/* AA-623 — AWS actual (Cost Explorer) vs estimated (token) reconciliation. Reads
                    the latest stored snapshot only (never a live CE call on page load — CE pricing
                    itself costs ~$0.01/request); "Refresh from AWS" triggers a fresh fetch on demand. */}
                <Card style={{ marginBottom: 20 }}>
                  <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", marginBottom: 10 }}>
                    <SLabel style={{ marginBottom: 0 }}>{t("awsVsEst")}</SLabel>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      {ceCheckMsg && <span style={{ fontSize: 11.5, color: A.muted2 }}>{ceCheckMsg}</span>}
                      <Button size="sm" variant="secondary" onClick={runCostExplorerCheck} disabled={ceChecking}>
                        {ceChecking ? t("refreshing") : t("refreshAws")}
                      </Button>
                    </div>
                  </div>

                  {!costExplorer?.has_data ? (
                    <div style={{ color: A.muted2, fontSize: 13, padding: "16px 0", textAlign: "center" }}>
                      {t("noCeData", rangeLabel)}
                    </div>
                  ) : (
                    <>
                      {ceGap && (
                        <div style={{ fontSize: 12, color: A.amber, marginBottom: 10, display: "flex", alignItems: "center", gap: 6 }}>
                          <AlertTriangle size={13} style={{ flexShrink: 0 }} /> {t("ceGap", ceGap)}
                        </div>
                      )}
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 14, marginBottom: 14 }}>
                        <StatCard
                          label={t("bedrockActual")}
                          value={fmtUsd2(costExplorer.bedrock_usd ?? 0)}
                          sub={costExplorer.fetched_at ? t("ceAsOf", formatDateTime(costExplorer.fetched_at)) : "CE"}
                          accent={A.gold}
                        />
                        <StatCard
                          label={t("bedrockEstimated")}
                          value={fmtUsd2(bedrockEstimated)}
                          sub={t("bedrockEstSub", rangeLabel)}
                          accent={A.ink3}
                        />
                        <StatCard
                          label={t("awsInfra")}
                          value={fmtUsd2((costExplorer.total_usd ?? 0) - (costExplorer.bedrock_usd ?? 0))}
                          sub={t("awsInfraSub", fmtUsd2(costExplorer.total_usd ?? 0))}
                          accent={A.muted}
                        />
                      </div>
                      <div style={{ overflowX: "auto" }}>
                        <table style={{ width: "100%", borderCollapse: "collapse" }}>
                          <thead><tr>
                            <th style={TH}>{t("colAccount")}</th>
                            <th style={{ ...TH, textAlign: "right" }}>{t("colBedrockActual")}</th>
                            <th style={{ ...TH, textAlign: "right" }}>{t("colBedrockEst")}</th>
                            <th style={{ ...TH, textAlign: "right" }}>{t("colInfra")}</th>
                            <th style={{ ...TH, textAlign: "right" }}>{t("colAccountTotal")}</th>
                          </tr></thead>
                          <tbody>
                            {ceByAccount.map(r => (
                              <tr key={r.key}>
                                <td style={TD}>{r.label}</td>
                                <td style={{ ...TD, textAlign: "right", fontFamily: mono }}>{fmtUsd2(r.bedrock_usd)}</td>
                                <td style={{ ...TD, textAlign: "right", fontFamily: mono, color: A.muted }}>{fmtUsd2(r.estimated)}</td>
                                <td style={{ ...TD, textAlign: "right", fontFamily: mono, color: A.muted }}>{fmtUsd2(r.other_usd)}</td>
                                <td style={{ ...TD, textAlign: "right", fontFamily: mono }}>{fmtUsd2(r.total_usd)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                      {ceTopServices.length > 0 && (
                        <details style={{ marginTop: 10 }}>
                          <summary style={{ fontSize: 12, color: A.muted, cursor: "pointer" }}>{t("topServices")}</summary>
                          <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 8 }}>
                            {ceTopServices.map(s => (
                              <div key={`${s.account_id}-${s.service}`} style={{ display: "flex", gap: 8, fontSize: 12 }}>
                                <span style={{ color: A.muted2, minWidth: 44, fontFamily: mono }}>{ceName(s, names.name)}</span>
                                <span style={{ color: s.is_bedrock ? A.gold : A.ink2 }}>{s.service}</span>
                                <span style={{ marginLeft: "auto", fontFamily: mono, color: A.ink3 }}>{fmtUsd2(s.amount_usd)}</span>
                              </div>
                            ))}
                          </div>
                        </details>
                      )}
                      <div style={{ fontSize: 11, color: A.muted2, marginTop: 8 }}>
                        {t("ceNoteA")}{names.name(MAIN_ACCOUNT)}{t("ceNoteB")}
                      </div>
                    </>
                  )}
                </Card>

              </>
            )}

            {/* ═══════════ LLM ═══════════ */}
            {tab === "llm" && (
              <>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 160px), 1fr))", gap: 14, marginBottom: 18 }}>
                  <StatCard label={t("llmCost")} value={fmtUsd2(llmCost)} sub={filterActive ? t("filtered") : rangeLabel} accent={A.gold} />
                  <StatCard label={t("colCalls")} value={fmtInt(llmCalls)} sub={t("tokens", fmtInt(llmTokens))} />
                  <StatCard label={t("passRate")} value={okElig > 0 ? pct(okCount / okElig) : "—"} sub={okElig > 0 ? `${okCount}/${okElig}` : t("noSignal")} />
                  <div onClick={() => llmFallback > 0 && setFbModal({ tenantId: tenantFilter, label: tenantLabelOf(tenantFilter) })}
                       style={{ cursor: llmFallback > 0 ? "pointer" : "default" }} title={llmFallback > 0 ? t("viewFallbackCalls") : undefined}>
                    <StatCard label={t("fallbackStat")} value={llmCalls > 0 ? pct(llmFallback / llmCalls) : "—"} sub={t("fallbackSubStat", fmtInt(llmFallback))} accent={A.amber} />
                  </div>
                  <StatCard label={t("truncated")} value={llmCalls > 0 ? pct(llmTruncated / llmCalls) : "—"} sub={t("truncatedSub", fmtInt(llmTruncated))} accent={A.red} />
                </div>

                <Card style={{ marginBottom: 18, padding: 0, overflow: "hidden" }}>
                  <div style={{ padding: "14px 18px" }}><SLabel style={{ marginBottom: 0 }}>{t("spendByTenant")}{acctFilter ? ` · ${names.short(acctFilter)}` : ""}</SLabel></div>
                  <div style={{ padding: "0 18px 16px" }}>
                    <TenantSpendTable branches={llm} onFallbackClick={(tid, label) => setFbModal({ tenantId: tid, label })} />
                  </div>
                </Card>

                <div style={{ display: "flex", gap: 6, marginBottom: 12, alignItems: "center" }}>
                  <span style={{ fontSize: 12, color: A.muted }}>{t("groupBy")}</span>
                  <Button size="sm" variant={groupBy === "tenant" ? "primary" : "secondary"} onClick={() => setGroupBy("tenant")}>{t("groupTenant")}</Button>
                  <Button size="sm" variant={groupBy === "account" ? "primary" : "secondary"} onClick={() => setGroupBy("account")}>{t("groupAccount")}</Button>
                </div>

                {llmTree.length === 0 ? (
                  <Card style={{ textAlign: "center", padding: 40 }}><div style={{ color: A.muted, fontSize: 13 }}>{t("noLlmFilter")}</div></Card>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                    {llmTree.map(([k, bs]) => {
                      const isAcct = groupBy === "account";
                      const label = isAcct ? names.long(k) : (bs[0]?.tenant_label ?? k);
                      const dot = isAcct ? (ACCOUNT_COLOR[k] ?? A.muted) : A.accent;
                      const sublabel = isAcct ? t("subAccount") : t("subTenant");
                      return <TopGroup key={k} label={label} sublabel={sublabel} dotColor={dot} branches={bs} groupBy={groupBy} />;
                    })}
                  </div>
                )}

                <Card style={{ marginTop: 20 }}>
                  <SLabel>{t("modelConfig")}</SLabel>
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 420px), 1fr))", gap: "6px 24px" }}>
                    {(configs ?? []).map(c => (
                      <div key={c.stage} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5, padding: "5px 0", borderBottom: `1px solid ${A.line2}` }}>
                        <span style={{ fontFamily: mono, color: A.ink2, minWidth: 150 }}>{c.stage}</span>
                        <Badge color={ROLE_COLOR[c.role] ?? "gray"}>{c.role}</Badge>
                        <span style={{ marginLeft: "auto", fontFamily: mono, color: A.ink3 }}>{c.model_id}{c.account_route ? ` · ${names.name(c.account_route)}` : ""}</span>
                      </div>
                    ))}
                  </div>
                </Card>
              </>
            )}

            {/* ═══════════ DFS ═══════════ */}
            {tab === "dfs" && (
              <>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 150px), 1fr))", gap: 14, marginBottom: 18 }}>
                  {/* AA-627 — account balance (remaining), distinct from cost (spent). Latest daily snapshot. */}
                  <StatCard
                    label={t("accountBalance")}
                    value={dfsBalance?.has_data ? fmtUsd2(dfsBalance.balance_usd ?? 0) : "—"}
                    sub={dfsBalance?.has_data && dfsBalance.fetched_at
                      ? t("asOfDay", formatDate(dfsBalance.fetched_at))
                      : t("noReading")}
                    accent={dfsBalance?.below_threshold ? A.red : A.green}
                  />
                  <StatCard label={t("dfsCost")} value={fmtUsd2(dfsCost)} sub={filterActive ? t("filtered") : rangeLabel} accent={A.green} />
                  <StatCard label={t("totalCalls")} value={fmtInt(dfsCalls)} />
                  <StatCard label={t("liveCalls")} value={fmtInt(dfsLive)} sub={t("realHttp")} accent={A.amber} />
                  <StatCard label={t("cacheHits")} value={fmtInt(dfsCacheHits)} accent={A.gold} />
                  <StatCard label={t("cacheHitRate")} value={pct(dfsCacheRate)} sub={t("dfsAttributable")} />
                </div>

                {/* AA-665 C2 — DataForSEO spend by the job that made the calls (not tenant-filtered). */}
                <DfsRunsPanel rangeQs={rangeQs} rangeLabel={rangeLabel} windowDays={windowSpanDays} />

                <Card style={{ padding: 0, overflow: "hidden", marginBottom: 20 }}>
                  <div style={{ padding: "14px 18px" }}><SLabel style={{ marginBottom: 0 }}>{t("spendByTenant")}</SLabel></div>
                  {dfsByTenant.length === 0 ? (
                    <div style={{ color: A.muted2, fontSize: 13, padding: "0 18px 24px" }}>{t("noDfsFilter")}</div>
                  ) : (
                    <ScrollX>
                    <table style={{ width: "100%", borderCollapse: "collapse" }}>
                      <thead><tr>
                        <th style={TH}>{t("colTenant")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colCost")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colCalls")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colLive")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colCache")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colCachePct")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colKeywords")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colPerKeyword")}</th>
                      </tr></thead>
                      <tbody>
                        {dfsByTenant.map((r, i) => (
                          <tr key={i}>
                            <td style={{ ...TD, fontWeight: 600, color: A.ink2 }}>{r.label}</td>
                            <td style={{ ...TD, textAlign: "right", fontFamily: mono, fontWeight: 600 }}>{fmtUsd(r.cost)}</td>
                            <td style={{ ...TD, textAlign: "right" }}>{fmtInt(r.calls)}</td>
                            <td style={{ ...TD, textAlign: "right" }}>{fmtInt(r.live)}</td>
                            <td style={{ ...TD, textAlign: "right", color: A.muted }}>{fmtInt(r.cache)}</td>
                            <td style={{ ...TD, textAlign: "right" }}>{pct(r.calls > 0 ? r.cache / r.calls : null)}</td>
                            <td style={{ ...TD, textAlign: "right" }}>{fmtInt(r.kw)}</td>
                            <td style={{ ...TD, textAlign: "right", fontFamily: mono, color: A.muted }}>{r.kw > 0 ? fmtUsd(r.cost / r.kw) : "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    </ScrollX>
                  )}
                </Card>

                <Card style={{ padding: 0, overflow: "hidden", marginBottom: 20 }}>
                  <div style={{ padding: "14px 18px" }}><SLabel style={{ marginBottom: 0 }}>{t("spendByEndpoint")}</SLabel></div>
                  {dfsRows.length === 0 ? (
                    <div style={{ color: A.muted2, fontSize: 13, padding: "0 18px 24px" }}>{t("noDfsFilter")}</div>
                  ) : (
                    <ScrollX>
                    <table style={{ width: "100%", borderCollapse: "collapse" }}>
                      <thead><tr>
                        <th style={TH}>{t("colEndpoint")}</th><th style={TH}>{t("colTenant")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colCost")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colLive")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colCache")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colCachePct")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colKeywords")}</th>
                      </tr></thead>
                      <tbody>
                        {dfsRows.map((b, i) => (
                          <tr key={i}>
                            <td style={{ ...TD, fontFamily: mono, fontSize: 12 }}>{b.endpoint}</td>
                            <td style={{ ...TD, color: A.muted }}>{b.tenant_label}</td>
                            <td style={{ ...TD, textAlign: "right", fontFamily: mono }}>{fmtUsd(b.total_cost_usd)}</td>
                            <td style={{ ...TD, textAlign: "right" }}>{fmtInt(b.live_count)}</td>
                            <td style={{ ...TD, textAlign: "right", color: A.muted }}>{fmtInt(b.cache_hit_count)}</td>
                            <td style={{ ...TD, textAlign: "right" }}>{pct(b.cache_hit_rate)}</td>
                            <td style={{ ...TD, textAlign: "right" }}>{fmtInt(b.keywords_total)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    </ScrollX>
                  )}
                </Card>

                <Card style={{ padding: 0, overflow: "hidden" }}>
                  <div style={{ padding: "14px 18px" }}><SLabel style={{ marginBottom: 0 }}>{t("spendByMarket")}</SLabel></div>
                  {(countries?.length ?? 0) === 0 ? (
                    <div style={{ color: A.muted2, fontSize: 13, padding: "0 18px 24px" }}>{t("noMarket")}</div>
                  ) : (
                    <ScrollX>
                    <table style={{ width: "100%", borderCollapse: "collapse" }}>
                      <thead><tr>
                        <th style={TH}>{t("colMarket")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colCost")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colCalls")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colLive")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colCachePct")}</th>
                        <th style={{ ...TH, textAlign: "right" }}>{t("colKeywords")}</th>
                      </tr></thead>
                      <tbody>
                        {countries!.map((c, i) => (
                          <tr key={i}>
                            <td style={{ ...TD }}>{c.country}</td>
                            <td style={{ ...TD, textAlign: "right", fontFamily: mono }}>{fmtUsd(c.total_cost_usd)}</td>
                            <td style={{ ...TD, textAlign: "right" }}>{fmtInt(c.call_count)}</td>
                            <td style={{ ...TD, textAlign: "right" }}>{fmtInt(c.live_count)}</td>
                            <td style={{ ...TD, textAlign: "right" }}>{pct(c.cache_hit_rate)}</td>
                            <td style={{ ...TD, textAlign: "right" }}>{fmtInt(c.keywords_total)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    </ScrollX>
                  )}
                  <div style={{ fontSize: 11, color: A.muted2, padding: "0 18px 14px" }}>{t("marketNote")}</div>
                </Card>
              </>
            )}
          </>
        )}

        {fbModal && (
          <FallbackModal tenantId={fbModal.tenantId} tenantLabel={fbModal.label} rangeQs={rangeQs} rangeLabel={rangeLabel} onClose={() => setFbModal(null)} />
        )}
      </PageFrame>
  );
}
