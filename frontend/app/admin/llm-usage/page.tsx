"use client";
// app/admin/llm-usage/page.tsx — AA-505 LLM cost/quality monitoring: Tenant -> Model -> Stage

import { useState, useEffect, useCallback, useMemo } from "react";
import { Gauge, ChevronRight, ChevronDown } from "lucide-react";
import AdminSidebar from "../_components/AdminSidebar";
import { A, serif, sans, mono, Card, SLabel, Badge, Btn, Spinner, LoadingScreen, StatCard } from "../_components/adminUi";

// ── Types (matches GET /admin/llm-usage/tree) ──────────────────────────────────

interface Branch {
  tenant_id: string | null;
  tenant_label: string;
  model: string;
  stage: string;
  role: string;
  call_count: number;
  total_cost_usd: number;
  ok_count: number;
  ok_eligible_count: number;
  ok_rate: number | null;
  avg_atoms_extracted: number | null;
  avg_output_len_chars: number | null;
  // AA-493: calls in this branch that stopped at the token limit (stop_reason="max_tokens")
  // rather than finishing normally — 0 for branches with no such call, never negative.
  truncated_count: number;
  last_call_at: string | null;
}

const ROLE_COLOR: Record<string, "gray" | "gold" | "green"> = { writer: "gold", judge: "green", validate: "gray" };

function fmtUsd(n: number): string {
  return n < 0.01 && n > 0 ? `$${n.toFixed(6)}` : `$${n.toFixed(4)}`;
}

// A stage's own quality reading — whichever of the 3 shapes this stage actually logs (see
// docs/implementation-notes/AA-505.md "Tradeoffs" for why these aren't unified into one number).
function QualityCell({ b }: { b: Branch }) {
  if (b.ok_eligible_count > 0) {
    const pct = Math.round((b.ok_rate ?? 0) * 100);
    const color = pct >= 80 ? A.green : pct >= 50 ? A.amber : A.red;
    return (
      <span style={{ color, fontWeight: 600 }}>
        {pct}% <span style={{ color: A.muted2, fontWeight: 400 }}>({b.ok_count}/{b.ok_eligible_count})</span>
      </span>
    );
  }
  if (b.avg_atoms_extracted != null) {
    return <span style={{ color: A.ink3 }}>{b.avg_atoms_extracted.toFixed(1)} atoms/call</span>;
  }
  if (b.avg_output_len_chars != null) {
    return <span style={{ color: A.ink3 }}>{Math.round(b.avg_output_len_chars)} chars/call</span>;
  }
  return <span style={{ color: A.muted2 }}>—</span>;
}

// ── Tree ─────────────────────────────────────────────────────────────────────

function StageLeaf({ b }: { b: Branch }) {
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 10, padding: "8px 0 8px 44px",
      borderBottom: `1px solid ${A.line2}`,
    }}>
      <span style={{ fontFamily: mono, fontSize: 12, color: A.ink, minWidth: 150 }}>{b.stage}</span>
      <Badge color={ROLE_COLOR[b.role] ?? "gray"}>{b.role}</Badge>
      <span style={{ fontSize: 12, color: A.muted, minWidth: 70 }}>{b.call_count} calls</span>
      <span style={{ fontSize: 12, color: A.ink2, minWidth: 90, fontFamily: mono }}>{fmtUsd(b.total_cost_usd)}</span>
      <span style={{ fontSize: 12, minWidth: 140 }}><QualityCell b={b} /></span>
      {/* AA-493: a real, computed count — never shown for 0, so a branch with no truncated
          call carries no visual noise. */}
      {b.truncated_count > 0 && (
        <Badge color="amber">
          {b.truncated_count} truncated (max_tokens)
        </Badge>
      )}
      <span style={{ fontSize: 11, color: A.muted2, marginLeft: "auto" }}>
        {b.last_call_at ? new Date(b.last_call_at).toLocaleString() : "—"}
      </span>
    </div>
  );
}

function ModelBranch({ model, branches }: { model: string; branches: Branch[] }) {
  const [open, setOpen] = useState(false);
  const totalCost = branches.reduce((s, b) => s + b.total_cost_usd, 0);
  const totalCalls = branches.reduce((s, b) => s + b.call_count, 0);
  return (
    <div>
      <button onClick={() => setOpen(o => !o)} style={{
        display: "flex", alignItems: "center", gap: 8, width: "100%",
        padding: "8px 0 8px 22px", background: "none", border: "none", cursor: "pointer",
        borderBottom: `1px solid ${A.line2}`, textAlign: "left",
      }}>
        {open ? <ChevronDown size={14} color={A.muted} /> : <ChevronRight size={14} color={A.muted} />}
        <span style={{ fontFamily: mono, fontSize: 12.5, color: A.ink2, fontWeight: 600 }}>{model}</span>
        <span style={{ fontSize: 11.5, color: A.muted2 }}>{totalCalls} calls</span>
        <span style={{ fontSize: 11.5, color: A.ink3, marginLeft: "auto", fontFamily: mono }}>{fmtUsd(totalCost)}</span>
      </button>
      {open && branches.map(b => <StageLeaf key={b.stage} b={b} />)}
    </div>
  );
}

function TenantBranch({ label, branches }: { label: string; branches: Branch[] }) {
  const [open, setOpen] = useState(false);
  const byModel = useMemo(() => {
    const m = new Map<string, Branch[]>();
    for (const b of branches) {
      if (!m.has(b.model)) m.set(b.model, []);
      m.get(b.model)!.push(b);
    }
    return m;
  }, [branches]);
  const totalCost = branches.reduce((s, b) => s + b.total_cost_usd, 0);
  const totalCalls = branches.reduce((s, b) => s + b.call_count, 0);
  return (
    <Card style={{ padding: 0, overflow: "hidden" }}>
      <button onClick={() => setOpen(o => !o)} style={{
        display: "flex", alignItems: "center", gap: 8, width: "100%",
        padding: "14px 18px", background: "none", border: "none", cursor: "pointer", textAlign: "left",
      }}>
        {open ? <ChevronDown size={15} color={A.ink} /> : <ChevronRight size={15} color={A.ink} />}
        <span style={{ fontFamily: serif, fontSize: 14.5, color: A.ink, fontWeight: 500 }}>{label}</span>
        <span style={{ fontSize: 12, color: A.muted2 }}>{totalCalls} calls · {byModel.size} model(s)</span>
        <span style={{ fontSize: 13, color: A.ink2, marginLeft: "auto", fontFamily: mono, fontWeight: 600 }}>
          {fmtUsd(totalCost)}
        </span>
      </button>
      {open && (
        <div style={{ padding: "0 18px 10px" }}>
          {[...byModel.entries()].map(([model, bs]) => (
            <ModelBranch key={model} model={model} branches={bs} />
          ))}
        </div>
      )}
    </Card>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

const DAY_OPTIONS = [7, 30, 90];

export default function LlmUsagePage() {
  const [days, setDays] = useState(30);
  const [branches, setBranches] = useState<Branch[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback((d: number) => {
    setLoading(true);
    fetch(`/api/admin/llm-usage/tree?days=${d}`)
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(data => { setBranches(data.branches); setError(""); })
      .catch(() => setError("Failed to load usage data"))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(days); }, [days, load]);

  const byTenant = useMemo(() => {
    const m = new Map<string, Branch[]>();
    for (const b of branches ?? []) {
      if (!m.has(b.tenant_label)) m.set(b.tenant_label, []);
      m.get(b.tenant_label)!.push(b);
    }
    return m;
  }, [branches]);

  const totalCost = (branches ?? []).reduce((s, b) => s + b.total_cost_usd, 0);
  const totalCalls = (branches ?? []).reduce((s, b) => s + b.call_count, 0);
  const eligible = (branches ?? []).reduce((s, b) => s + b.ok_eligible_count, 0);
  const ok = (branches ?? []).reduce((s, b) => s + b.ok_count, 0);
  // AA-493 — real count of calls that stopped at the token limit, across every branch.
  const truncated = (branches ?? []).reduce((s, b) => s + b.truncated_count, 0);

  return (
    <div style={{ display: "flex", height: "100vh", background: A.bg, fontFamily: sans }}>
      <AdminSidebar />
      <main style={{ flex: 1, padding: "32px 36px", minWidth: 0, minHeight: 0, overflowY: "auto" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 24 }}>
          <div style={{ width: 36, height: 36, borderRadius: 9, background: `${A.red}15`, color: A.red, display: "grid", placeItems: "center" }}>
            <Gauge size={18} />
          </div>
          <div>
            <h1 style={{ fontFamily: serif, fontSize: 22, fontWeight: 500, color: A.ink, letterSpacing: "-0.02em", margin: 0 }}>
              LLM Usage
            </h1>
            <div style={{ fontSize: 11.5, color: A.muted2, marginTop: 2 }}>
              Cost + quality of every real LLM call, by Tenant → Model → Stage
            </div>
          </div>
          <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
            {DAY_OPTIONS.map(d => (
              <Btn key={d} size="sm" variant={days === d ? "primary" : "secondary"} onClick={() => setDays(d)}>
                {d} days
              </Btn>
            ))}
          </div>
        </div>

        {loading && <LoadingScreen msg="Loading usage…" />}

        {!loading && error && (
          <Card style={{ textAlign: "center", padding: 40 }}>
            <div style={{ color: A.red, marginBottom: 12 }}>{error}</div>
            <Btn variant="secondary" onClick={() => load(days)}>Retry</Btn>
          </Card>
        )}

        {!loading && !error && branches && branches.length === 0 && (
          <Card style={{ textAlign: "center", padding: 40 }}>
            <div style={{ color: A.muted, fontSize: 13 }}>
              No data yet — this will appear once there are real LLM calls in the last {days} days.
            </div>
          </Card>
        )}

        {!loading && !error && branches && branches.length > 0 && (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 14, marginBottom: 20 }}>
              <StatCard label="Total cost" value={fmtUsd(totalCost)} sub={`last ${days} days`} />
              <StatCard label="Total calls" value={String(totalCalls)} />
              <StatCard label="Tenant" value={String(byTenant.size)} />
              <StatCard label="Pass rate (gate/judge)"
                        value={eligible > 0 ? `${Math.round((ok / eligible) * 100)}%` : "—"}
                        sub={eligible > 0 ? `${ok}/${eligible} calls with a pass/fail signal` : "no stage measured yet"} />
              {/* AA-493 — stop_reason="max_tokens" tách được khỏi lượt hoàn tất bình thường,
                  lần đầu tiên đo được thật (trước đây field này bị vứt đi im lặng). */}
              <StatCard label="Truncated (max_tokens)"
                        value={String(truncated)}
                        sub={totalCalls > 0 ? `${Math.round((truncated / totalCalls) * 100)}% of total calls` : undefined} />
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {[...byTenant.entries()].map(([label, bs]) => (
                <TenantBranch key={label} label={label} branches={bs} />
              ))}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
