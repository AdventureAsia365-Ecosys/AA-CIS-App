"use client";
// app/admin/llm-usage/page.tsx — AA-622 External Spend: LLM (account→model→stage→tenant) + DFS +
// Overview. Extends the AA-505/AA-617 LLM tree with account/fallback/tokens and the AA-618 DFS log.
// Path kept as /admin/llm-usage (middleware allowlist already admin-only — no new route).

import { useState, useEffect, useCallback, useMemo } from "react";
import {
  ChevronRight, ChevronDown, Cpu, Search, Wallet, TrendingUp,
} from "lucide-react";
import {
  ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid, Legend,
} from "recharts";
import AdminSidebar from "../_components/AdminSidebar";
import {
  A, serif, sans, mono, Card, SLabel, Badge, Btn, LoadingScreen, StatCard, TabBar, TH, TD,
  CHART_TOOLTIP,
} from "../_components/adminUi";

// ── Types ──────────────────────────────────────────────────────────────────

interface Branch {
  tenant_id: string | null;
  tenant_label: string;
  model: string;
  stage: string;
  role: string;
  account: string;            // AA-617: acc1/acc2/acc3/unknown
  provider: string;           // AA-617: claude/openai/unknown
  fallback_count: number;     // AA-617
  call_count: number;
  total_cost_usd: number;
  tokens_in_total: number;    // AA-622
  tokens_out_total: number;   // AA-622
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
interface StageConfig {
  stage: string; role: string; provider: string; model_id: string; account_route: string | null;
}

const ROLE_COLOR: Record<string, "gray" | "gold" | "green"> = { writer: "gold", judge: "green", validate: "gray" };
const ACCOUNT_META: Record<string, { label: string; color: string }> = {
  acc3:    { label: "acc3 · 786888028788", color: A.red },
  acc1:    { label: "acc1 · 867490540162", color: A.gold },
  acc2:    { label: "acc2 · 005097885195", color: A.green },
  unknown: { label: "OpenAI / legacy",      color: A.muted },
};

function fmtUsd(n: number): string {
  return n < 0.01 && n > 0 ? `$${n.toFixed(6)}` : `$${n.toFixed(4)}`;
}
function fmtUsd2(n: number): string { return `$${n.toFixed(2)}`; }
function fmtInt(n: number): string { return n.toLocaleString("en-US"); }
function pct(n: number | null): string { return n == null ? "—" : `${Math.round(n * 100)}%`; }

const DAY_OPTIONS = [7, 30, 90];

// ── Shared quality cell (unchanged from AA-505) ──────────────────────────────

function QualityCell({ b }: { b: Branch }) {
  if (b.ok_eligible_count > 0) {
    const p = Math.round((b.ok_rate ?? 0) * 100);
    const color = p >= 80 ? A.green : p >= 50 ? A.amber : A.red;
    return (
      <span style={{ color, fontWeight: 600 }}>
        {p}% <span style={{ color: A.muted2, fontWeight: 400 }}>({b.ok_count}/{b.ok_eligible_count})</span>
      </span>
    );
  }
  if (b.avg_atoms_extracted != null) return <span style={{ color: A.ink3 }}>{b.avg_atoms_extracted.toFixed(1)} atoms/call</span>;
  if (b.avg_output_len_chars != null) return <span style={{ color: A.ink3 }}>{Math.round(b.avg_output_len_chars)} chars/call</span>;
  return <span style={{ color: A.muted2 }}>—</span>;
}

// ═══════════════════════ LLM TAB — Account → Model → Stage → Tenant ═════════════════════════

function StageLeaf({ b }: { b: Branch }) {
  const fbPct = b.call_count > 0 ? b.fallback_count / b.call_count : 0;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 0 8px 60px", borderBottom: `1px solid ${A.line2}` }}>
      <span style={{ fontFamily: mono, fontSize: 12, color: A.ink, minWidth: 150 }}>{b.stage}</span>
      <Badge color={ROLE_COLOR[b.role] ?? "gray"}>{b.role}</Badge>
      <span style={{ fontSize: 12, color: A.muted2, minWidth: 120 }}>{b.tenant_label}</span>
      <span style={{ fontSize: 12, color: A.muted, minWidth: 64 }}>{fmtInt(b.call_count)} calls</span>
      <span style={{ fontSize: 12, color: A.ink2, minWidth: 84, fontFamily: mono }}>{fmtUsd(b.total_cost_usd)}</span>
      <span style={{ fontSize: 12, minWidth: 130 }}><QualityCell b={b} /></span>
      {b.fallback_count > 0 && <Badge color="amber">{Math.round(fbPct * 100)}% fallback</Badge>}
      {b.truncated_count > 0 && <Badge color="red">{b.truncated_count} truncated</Badge>}
      <span style={{ fontSize: 11, color: A.muted2, marginLeft: "auto" }}>
        {b.last_call_at ? new Date(b.last_call_at).toLocaleString() : "—"}
      </span>
    </div>
  );
}

function ModelBranch({ model, branches, configModel }: { model: string; branches: Branch[]; configModel?: string }) {
  const [open, setOpen] = useState(false);
  const totalCost = branches.reduce((s, b) => s + b.total_cost_usd, 0);
  const totalCalls = branches.reduce((s, b) => s + b.call_count, 0);
  const tokTotal = branches.reduce((s, b) => s + b.tokens_in_total + b.tokens_out_total, 0);
  const costPerK = tokTotal > 0 ? (totalCost / tokTotal) * 1000 : 0;
  return (
    <div>
      <button onClick={() => setOpen(o => !o)} style={{
        display: "flex", alignItems: "center", gap: 8, width: "100%",
        padding: "8px 0 8px 38px", background: "none", border: "none", cursor: "pointer",
        borderBottom: `1px solid ${A.line2}`, textAlign: "left",
      }}>
        {open ? <ChevronDown size={14} color={A.muted} /> : <ChevronRight size={14} color={A.muted} />}
        <span style={{ fontFamily: mono, fontSize: 12.5, color: A.ink2, fontWeight: 600 }}>{model}</span>
        {configModel && <Badge color={configModel === model ? "green" : "gray"}>config: {configModel}</Badge>}
        <span style={{ fontSize: 11.5, color: A.muted2 }}>{fmtInt(totalCalls)} calls</span>
        {tokTotal > 0 && <span style={{ fontSize: 11, color: A.muted2, fontFamily: mono }}>{fmtUsd(costPerK)}/1K tok</span>}
        <span style={{ fontSize: 11.5, color: A.ink3, marginLeft: "auto", fontFamily: mono }}>{fmtUsd(totalCost)}</span>
      </button>
      {open && branches.map((b, i) => <StageLeaf key={`${b.stage}-${b.tenant_label}-${i}`} b={b} />)}
    </div>
  );
}

function AccountGroup({ account, branches }: { account: string; branches: Branch[] }) {
  const [open, setOpen] = useState(true);
  const meta = ACCOUNT_META[account] ?? { label: account, color: A.muted };
  const byModel = useMemo(() => {
    const m = new Map<string, Branch[]>();
    for (const b of branches) { if (!m.has(b.model)) m.set(b.model, []); m.get(b.model)!.push(b); }
    return m;
  }, [branches]);
  const totalCost = branches.reduce((s, b) => s + b.total_cost_usd, 0);
  const totalCalls = branches.reduce((s, b) => s + b.call_count, 0);
  return (
    <Card style={{ padding: 0, overflow: "hidden" }}>
      <button onClick={() => setOpen(o => !o)} style={{
        display: "flex", alignItems: "center", gap: 10, width: "100%",
        padding: "14px 18px", background: "none", border: "none", cursor: "pointer", textAlign: "left",
      }}>
        {open ? <ChevronDown size={15} color={A.ink} /> : <ChevronRight size={15} color={A.ink} />}
        <span style={{ width: 9, height: 9, borderRadius: "50%", background: meta.color, flexShrink: 0 }} />
        <span style={{ fontFamily: serif, fontSize: 14.5, color: A.ink, fontWeight: 500 }}>{meta.label}</span>
        <span style={{ fontSize: 12, color: A.muted2 }}>{fmtInt(totalCalls)} calls · {byModel.size} model(s)</span>
        <span style={{ fontSize: 13, color: A.ink2, marginLeft: "auto", fontFamily: mono, fontWeight: 600 }}>{fmtUsd(totalCost)}</span>
      </button>
      {open && (
        <div style={{ padding: "0 18px 10px" }}>
          {[...byModel.entries()].map(([model, bs]) => (
            <ModelBranch key={model} model={model} branches={bs} configModel={undefined} />
          ))}
        </div>
      )}
    </Card>
  );
}

// ═══════════════════════ OVERVIEW helpers ═══════════════════════════════════

function AccountBar({ rows, total }: { rows: { key: string; cost: number }[]; total: number }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {rows.map(r => {
        const meta = ACCOUNT_META[r.key] ?? { label: r.key, color: A.muted };
        const w = total > 0 ? (r.cost / total) * 100 : 0;
        return (
          <div key={r.key}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
              <span style={{ color: A.ink2 }}>{meta.label}</span>
              <span style={{ fontFamily: mono, color: A.ink3 }}>{fmtUsd(r.cost)} <span style={{ color: A.muted2 }}>({Math.round(w)}%)</span></span>
            </div>
            <div style={{ height: 8, background: A.line2, borderRadius: 999, overflow: "hidden" }}>
              <div style={{ width: `${w}%`, height: "100%", background: meta.color, borderRadius: 999 }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ═══════════════════════ PAGE ═══════════════════════════════════════════════

export default function ExternalSpendPage() {
  const [days, setDays] = useState(30);
  const [tab, setTab] = useState("overview");
  const [branches, setBranches] = useState<Branch[] | null>(null);
  const [dfs, setDfs] = useState<{ summary: DfsSummary; branches: DfsBranch[] } | null>(null);
  const [countries, setCountries] = useState<DfsCountry[] | null>(null);
  const [daily, setDaily] = useState<DailyPoint[] | null>(null);
  const [configs, setConfigs] = useState<StageConfig[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback((d: number) => {
    setLoading(true);
    Promise.all([
      fetch(`/api/admin/llm-usage/tree?days=${d}`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
      fetch(`/api/admin/dfs-usage?days=${d}`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
      fetch(`/api/admin/dfs-usage/by-country?days=${d}`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
      fetch(`/api/admin/spend/daily?days=${d}`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
      fetch(`/api/admin/llm-config`).then(r => r.ok ? r.json() : Promise.reject(r.status)),
    ])
      .then(([tree, dfsData, byCountry, dailyData, cfg]) => {
        setBranches(tree.branches);
        setDfs({ summary: dfsData.summary, branches: dfsData.branches });
        setCountries(byCountry.countries);
        setDaily(dailyData.points);
        setConfigs(cfg.stages);
        setError("");
      })
      .catch(() => setError("Failed to load spend data"))
      .finally(() => setLoading(false));
  }, []);
  // eslint-disable-next-line react-hooks/set-state-in-effect -- initial + range-change fetch; load() sets loading state, same pattern as every other admin page
  useEffect(() => { load(days); }, [days, load]);

  // ── LLM aggregates ──
  const llm = useMemo(() => branches ?? [], [branches]);
  const llmCost = llm.reduce((s, b) => s + b.total_cost_usd, 0);
  const llmCalls = llm.reduce((s, b) => s + b.call_count, 0);
  const llmTokens = llm.reduce((s, b) => s + b.tokens_in_total + b.tokens_out_total, 0);
  const llmFallback = llm.reduce((s, b) => s + b.fallback_count, 0);
  const llmTruncated = llm.reduce((s, b) => s + b.truncated_count, 0);
  const okEligible = llm.reduce((s, b) => s + b.ok_eligible_count, 0);
  const okCount = llm.reduce((s, b) => s + b.ok_count, 0);

  const dfsCost = dfs?.summary?.total_cost_usd ?? 0;
  const totalSpend = llmCost + dfsCost;

  // ── spend by account (Overview) ──
  const byAccount = useMemo(() => {
    const m = new Map<string, number>();
    for (const b of llm) m.set(b.account, (m.get(b.account) ?? 0) + b.total_cost_usd);
    const rows = [...m.entries()].map(([key, cost]) => ({ key, cost })).sort((a, b) => b.cost - a.cost);
    if (dfsCost > 0) rows.push({ key: "dfs", cost: dfsCost });
    return rows;
  }, [llm, dfsCost]);

  // ── stage cost ranking (Overview) — the lever the whole epic is about ──
  const stageRanking = useMemo(() => {
    const m = new Map<string, { cost: number; calls: number }>();
    for (const b of llm) {
      const cur = m.get(b.stage) ?? { cost: 0, calls: 0 };
      cur.cost += b.total_cost_usd; cur.calls += b.call_count;
      m.set(b.stage, cur);
    }
    return [...m.entries()].map(([stage, v]) => ({ stage, ...v })).sort((a, b) => b.cost - a.cost).slice(0, 10);
  }, [llm]);

  // ── LLM grouped by account (LLM tab) ──
  const [acctFilter, setAcctFilter] = useState<string | null>(null);
  const byAccountTree = useMemo(() => {
    const m = new Map<string, Branch[]>();
    for (const b of llm) {
      if (acctFilter && b.account !== acctFilter) continue;
      if (!m.has(b.account)) m.set(b.account, []);
      m.get(b.account)!.push(b);
    }
    return m;
  }, [llm, acctFilter]);

  // ── daily trend pivot (Overview) ──
  const trend = useMemo(() => {
    const m = new Map<string, { day: string; llm: number; dfs: number }>();
    for (const p of daily ?? []) {
      const row = m.get(p.day) ?? { day: p.day, llm: 0, dfs: 0 };
      row[p.source] = p.cost_usd;
      m.set(p.day, row);
    }
    return [...m.values()].sort((a, b) => a.day.localeCompare(b.day))
      .map(r => ({ ...r, label: r.day.slice(5) }));  // MM-DD
  }, [daily]);

  const acctOptions = useMemo(() => [...new Set(llm.map(b => b.account))], [llm]);

  return (
    <div style={{ display: "flex", height: "100vh", background: A.bg, fontFamily: sans }}>
      <AdminSidebar />
      <main style={{ flex: 1, padding: "32px 36px", minWidth: 0, minHeight: 0, overflowY: "auto" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 20 }}>
          <div style={{ width: 36, height: 36, borderRadius: 9, background: `${A.red}15`, color: A.red, display: "grid", placeItems: "center" }}>
            <Wallet size={18} />
          </div>
          <div>
            <h1 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: A.ink, letterSpacing: "-0.02em", margin: 0 }}>
              External Spend
            </h1>
            <div style={{ fontSize: 11.5, color: A.muted2, marginTop: 2 }}>
              Real LLM + DataForSEO cost, by account · model · stage · tenant
            </div>
          </div>
          <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
            {DAY_OPTIONS.map(d => (
              <Btn key={d} size="sm" variant={days === d ? "primary" : "secondary"} onClick={() => setDays(d)}>{d} days</Btn>
            ))}
          </div>
        </div>

        <div style={{ marginBottom: 20 }}>
          <TabBar
            tabs={[{ key: "overview", label: "Overview" }, { key: "llm", label: "LLM" }, { key: "dfs", label: "DataForSEO" }]}
            active={tab} onChange={setTab}
          />
        </div>

        {loading && <LoadingScreen msg="Loading spend…" />}
        {!loading && error && (
          <Card style={{ textAlign: "center", padding: 40 }}>
            <div style={{ color: A.red, marginBottom: 12 }}>{error}</div>
            <Btn variant="secondary" onClick={() => load(days)}>Retry</Btn>
          </Card>
        )}

        {!loading && !error && (
          <>
            {/* ═══════════ OVERVIEW ═══════════ */}
            {tab === "overview" && (
              <>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 14, marginBottom: 20 }}>
                  <StatCard label="Total external spend" value={fmtUsd2(totalSpend)} sub={`last ${days} days · LLM + DFS`} icon={<Wallet size={16} />} />
                  <StatCard label="LLM cost" value={fmtUsd2(llmCost)} sub={`${fmtInt(llmCalls)} calls`} accent={A.gold} icon={<Cpu size={16} />} />
                  <StatCard label="DataForSEO cost" value={fmtUsd2(dfsCost)} sub={`${fmtInt(dfs?.summary?.total_calls ?? 0)} calls · ${pct(dfs?.summary?.cache_hit_rate ?? null)} cache hit`} accent={A.green} icon={<Search size={16} />} />
                  <StatCard label="LLM tokens" value={fmtInt(llmTokens)} sub={`${fmtInt(llmFallback)} fallback · ${fmtInt(llmTruncated)} truncated`} accent={A.red} icon={<TrendingUp size={16} />} />
                </div>

                <Card style={{ marginBottom: 20 }}>
                  <SLabel>Daily spend trend (LLM + DFS)</SLabel>
                  {trend.length === 0 ? (
                    <div style={{ color: A.muted2, fontSize: 13, padding: "30px 0", textAlign: "center" }}>No spend in the last {days} days.</div>
                  ) : (
                    <ResponsiveContainer width="100%" height={240}>
                      <AreaChart data={trend} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke={A.line2} vertical={false} />
                        <XAxis dataKey="label" tick={{ fontSize: 11, fill: A.muted }} stroke={A.line} />
                        <YAxis tick={{ fontSize: 11, fill: A.muted }} stroke={A.line} tickFormatter={(v) => `$${v}`} />
                        <Tooltip {...CHART_TOOLTIP} formatter={(v) => fmtUsd(Number(v) || 0)} />
                        <Legend wrapperStyle={{ fontSize: 12 }} />
                        <Area type="monotone" dataKey="llm" name="LLM" stackId="1" stroke={A.gold} fill={`${A.gold}55`} />
                        <Area type="monotone" dataKey="dfs" name="DataForSEO" stackId="1" stroke={A.green} fill={`${A.green}55`} />
                      </AreaChart>
                    </ResponsiveContainer>
                  )}
                </Card>

                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20, marginBottom: 20 }}>
                  <Card>
                    <SLabel>Spend by account</SLabel>
                    {byAccount.length === 0
                      ? <div style={{ color: A.muted2, fontSize: 13 }}>No spend yet.</div>
                      : <AccountBar rows={byAccount} total={totalSpend} />}
                  </Card>
                  <Card>
                    <SLabel>Top stages by cost</SLabel>
                    {stageRanking.length === 0 ? (
                      <div style={{ color: A.muted2, fontSize: 13 }}>No LLM calls yet.</div>
                    ) : (
                      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                        {stageRanking.map(s => (
                          <div key={s.stage} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
                            <span style={{ fontFamily: mono, color: A.ink2, minWidth: 150 }}>{s.stage}</span>
                            <span style={{ color: A.muted2 }}>{fmtInt(s.calls)} calls</span>
                            <span style={{ marginLeft: "auto", fontFamily: mono, color: A.ink3 }}>{fmtUsd(s.cost)}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </Card>
                </div>

                <Card dark>
                  <SLabel light>Reconcile note</SLabel>
                  <div style={{ fontSize: 12.5, color: "rgba(255,255,255,0.75)", lineHeight: 1.6 }}>
                    Figures are computed from per-call token/response cost logged in <code style={{ fontFamily: mono }}>llm_call_log</code> + <code style={{ fontFamily: mono }}>dfs_call_log</code>.
                    They are NOT the AWS invoice — Bedrock runs on satellite accounts (acc3 = <b>786888028788</b>, acc1 = 867490540162, acc2 = 005097885195);
                    DataForSEO is a 3rd-party API and never appears in AWS Cost Explorer. Cross-check against Cost Explorer by hand until a CE integration is built (tracked separately).
                  </div>
                </Card>
              </>
            )}

            {/* ═══════════ LLM ═══════════ */}
            {tab === "llm" && (
              <>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 14, marginBottom: 18 }}>
                  <StatCard label="LLM cost" value={fmtUsd2(llmCost)} sub={`last ${days} days`} accent={A.gold} />
                  <StatCard label="Calls" value={fmtInt(llmCalls)} />
                  <StatCard label="Pass rate" value={okEligible > 0 ? pct(okCount / okEligible) : "—"}
                            sub={okEligible > 0 ? `${okCount}/${okEligible} measured` : "no signal yet"} />
                  <StatCard label="Fallback" value={llmCalls > 0 ? pct(llmFallback / llmCalls) : "—"}
                            sub={`${fmtInt(llmFallback)} calls acc3→acc1/GPT`} accent={A.amber} />
                  <StatCard label="Truncated" value={llmCalls > 0 ? pct(llmTruncated / llmCalls) : "—"}
                            sub={`${fmtInt(llmTruncated)} at max_tokens`} accent={A.red} />
                </div>

                <div style={{ display: "flex", gap: 6, marginBottom: 14, alignItems: "center" }}>
                  <span style={{ fontSize: 12, color: A.muted }}>Account:</span>
                  <Btn size="sm" variant={acctFilter === null ? "primary" : "secondary"} onClick={() => setAcctFilter(null)}>All</Btn>
                  {acctOptions.map(a => (
                    <Btn key={a} size="sm" variant={acctFilter === a ? "primary" : "secondary"} onClick={() => setAcctFilter(a)}>
                      {ACCOUNT_META[a]?.label.split(" · ")[0] ?? a}
                    </Btn>
                  ))}
                </div>

                {byAccountTree.size === 0 ? (
                  <Card style={{ textAlign: "center", padding: 40 }}>
                    <div style={{ color: A.muted, fontSize: 13 }}>No LLM calls in the last {days} days.</div>
                  </Card>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                    {[...byAccountTree.entries()].map(([acct, bs]) => (
                      <AccountGroup key={acct} account={acct} branches={bs} />
                    ))}
                  </div>
                )}

                <Card style={{ marginTop: 20 }}>
                  <SLabel>Current model config (Settings → LLM Models)</SLabel>
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: "6px 24px" }}>
                    {(configs ?? []).map(c => (
                      <div key={c.stage} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5, padding: "5px 0", borderBottom: `1px solid ${A.line2}` }}>
                        <span style={{ fontFamily: mono, color: A.ink2, minWidth: 150 }}>{c.stage}</span>
                        <Badge color={ROLE_COLOR[c.role] ?? "gray"}>{c.role}</Badge>
                        <span style={{ marginLeft: "auto", fontFamily: mono, color: A.ink3 }}>
                          {c.model_id}{c.account_route ? ` · ${c.account_route}` : ""}
                        </span>
                      </div>
                    ))}
                  </div>
                </Card>
              </>
            )}

            {/* ═══════════ DFS ═══════════ */}
            {tab === "dfs" && (
              <>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 14, marginBottom: 18 }}>
                  <StatCard label="DFS cost" value={fmtUsd2(dfsCost)} sub={`last ${days} days`} accent={A.green} />
                  <StatCard label="Total calls" value={fmtInt(dfs?.summary?.total_calls ?? 0)} />
                  <StatCard label="Live calls" value={fmtInt(dfs?.summary?.live_calls ?? 0)} sub="real DFS HTTP" accent={A.amber} />
                  <StatCard label="Cache hits" value={fmtInt(dfs?.summary?.cache_hits ?? 0)} accent={A.gold} />
                  <StatCard label="Cache hit rate" value={pct(dfs?.summary?.cache_hit_rate ?? null)} sub="DFS-attributable (not Redis-global)" />
                </div>

                <Card style={{ padding: 0, overflow: "hidden", marginBottom: 20 }}>
                  <div style={{ padding: "14px 18px" }}><SLabel style={{ marginBottom: 0 }}>Spend by endpoint × tenant</SLabel></div>
                  {(dfs?.branches?.length ?? 0) === 0 ? (
                    <div style={{ color: A.muted2, fontSize: 13, padding: "0 18px 24px" }}>No DataForSEO calls in the last {days} days.</div>
                  ) : (
                    <table style={{ width: "100%", borderCollapse: "collapse" }}>
                      <thead><tr>
                        <th style={TH}>Endpoint</th><th style={TH}>Tenant</th>
                        <th style={{ ...TH, textAlign: "right" }}>Cost</th>
                        <th style={{ ...TH, textAlign: "right" }}>Live</th>
                        <th style={{ ...TH, textAlign: "right" }}>Cache</th>
                        <th style={{ ...TH, textAlign: "right" }}>Cache %</th>
                        <th style={{ ...TH, textAlign: "right" }}>Keywords</th>
                        <th style={{ ...TH, textAlign: "right" }}>$/keyword</th>
                      </tr></thead>
                      <tbody>
                        {dfs!.branches.map((b, i) => (
                          <tr key={i}>
                            <td style={{ ...TD, fontFamily: mono, fontSize: 12 }}>{b.endpoint}</td>
                            <td style={{ ...TD, color: A.muted }}>{b.tenant_label}</td>
                            <td style={{ ...TD, textAlign: "right", fontFamily: mono }}>{fmtUsd(b.total_cost_usd)}</td>
                            <td style={{ ...TD, textAlign: "right" }}>{fmtInt(b.live_count)}</td>
                            <td style={{ ...TD, textAlign: "right", color: A.muted }}>{fmtInt(b.cache_hit_count)}</td>
                            <td style={{ ...TD, textAlign: "right" }}>{pct(b.cache_hit_rate)}</td>
                            <td style={{ ...TD, textAlign: "right" }}>{fmtInt(b.keywords_total)}</td>
                            <td style={{ ...TD, textAlign: "right", fontFamily: mono, color: A.muted }}>
                              {b.keywords_total > 0 ? fmtUsd(b.total_cost_usd / b.keywords_total) : "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </Card>

                <Card style={{ padding: 0, overflow: "hidden" }}>
                  <div style={{ padding: "14px 18px" }}><SLabel style={{ marginBottom: 0 }}>Spend by target market</SLabel></div>
                  {(countries?.length ?? 0) === 0 ? (
                    <div style={{ color: A.muted2, fontSize: 13, padding: "0 18px 24px" }}>No market data yet.</div>
                  ) : (
                    <table style={{ width: "100%", borderCollapse: "collapse" }}>
                      <thead><tr>
                        <th style={TH}>Market</th>
                        <th style={{ ...TH, textAlign: "right" }}>Cost</th>
                        <th style={{ ...TH, textAlign: "right" }}>Calls</th>
                        <th style={{ ...TH, textAlign: "right" }}>Live</th>
                        <th style={{ ...TH, textAlign: "right" }}>Cache %</th>
                        <th style={{ ...TH, textAlign: "right" }}>Keywords</th>
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
                  )}
                </Card>
              </>
            )}
          </>
        )}
      </main>
    </div>
  );
}
