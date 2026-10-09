"use client";
// app/admin/llm-usage/BudgetsPanel.tsx — AA-665 (KAN-90 follow-up): the spend-budget settings
// screen. Reads/writes shared.spend_budget through GET/PUT /admin/budgets (AA-649). Each cap stops
// or only alerts according to its own row (#472): e.g. the DFS daily cap on `global` is alert-only
// while `job:segment_research`'s per-run cap still stops a run.

import { useCallback, useEffect, useState } from "react";
import { A, mono, Card, SLabel, Badge, Btn, TH, TD } from "../_components/adminUi";
import { formatDateTime } from "../../_kit";

interface BudgetRow {
  provider: string;
  scope: string;
  per_run_usd: number | null;
  per_day_usd: number | null;
  hard_stop: boolean;
  alert_pct: number;
  updated_at: string | null;
  updated_by: string | null;
}
interface Draft { per_run: string; per_day: string; hard_stop: boolean; alert_pct: string }

const PROVIDERS = ["dfs", "bedrock", "openai", "jev"] as const;
const PROVIDER_LABEL: Record<string, string> = { dfs: "DataForSEO", bedrock: "Bedrock", openai: "OpenAI", jev: "Jev" };

function toDraft(r: BudgetRow): Draft {
  return {
    per_run: r.per_run_usd == null ? "" : String(r.per_run_usd),
    per_day: r.per_day_usd == null ? "" : String(r.per_day_usd),
    hard_stop: r.hard_stop,
    alert_pct: String(r.alert_pct),
  };
}

/** "" = no cap (null); otherwise a non-negative number, or an error message. */
function parseCap(v: string): { value: number | null; error?: string } {
  if (v.trim() === "") return { value: null };
  const n = Number(v);
  if (!Number.isFinite(n) || n < 0) return { value: null, error: "must be a number ≥ 0 or empty" };
  return { value: n };
}

function scopeLabel(scope: string): string {
  return scope === "global" ? "All usage (global)" : `Job: ${scope.slice(4)}`;
}

const input = {
  width: 90, fontSize: 12.5, padding: "5px 7px", border: `1px solid ${A.line}`, borderRadius: 6,
  fontFamily: mono, background: A.card, color: A.ink,
} as const;

export default function BudgetsPanel() {
  const [rows, setRows] = useState<BudgetRow[] | null>(null);
  const [spent, setSpent] = useState<Record<string, number>>({});
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [newRow, setNewRow] = useState({ provider: "dfs", scope: "global" });

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/admin/budgets");
      if (!r.ok) throw new Error(String(r.status));
      const d: { budgets: BudgetRow[]; spent_today_usd: Record<string, number> } = await r.json();
      setRows(d.budgets);
      setSpent(d.spent_today_usd ?? {});
      setDrafts(Object.fromEntries(d.budgets.map(b => [`${b.provider}/${b.scope}`, toDraft(b)])));
    } catch (e) {
      setMsg({ kind: "err", text: `Could not load budgets (${e})` });
      setRows(prev => prev ?? []);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch, same pattern as every admin page
    load();
  }, [load]);

  async function save(provider: string, scope: string, d: Draft) {
    const run = parseCap(d.per_run), day = parseCap(d.per_day);
    const pctN = Number(d.alert_pct);
    if (run.error || day.error) { setMsg({ kind: "err", text: `Per run / per day ${run.error ?? day.error}` }); return; }
    if (!Number.isInteger(pctN) || pctN < 1 || pctN > 100) { setMsg({ kind: "err", text: "Alert % must be a whole number 1–100" }); return; }
    const key = `${provider}/${scope}`;
    if (!window.confirm(`Save ${PROVIDER_LABEL[provider] ?? provider} · ${scopeLabel(scope)}?\n\n` +
      `Per run: ${run.value ?? "no cap"} · Per day: ${day.value ?? "no cap"} · ${d.hard_stop ? "STOPS at the cap" : "alert only"} · alert at ${pctN}%`)) return;
    setSaving(key);
    try {
      const r = await fetch(`/api/admin/budgets/${provider}/${encodeURIComponent(scope)}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ per_run_usd: run.value, per_day_usd: day.value, hard_stop: d.hard_stop, alert_pct: pctN }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(typeof body.detail === "string" ? body.detail : String(r.status));
      }
      setMsg({ kind: "ok", text: `Saved ${PROVIDER_LABEL[provider] ?? provider} · ${scopeLabel(scope)}. Runs that start from now use it.` });
      await load();
    } catch (e) {
      setMsg({ kind: "err", text: `Save failed: ${e}` });
    } finally {
      setSaving(null);
    }
  }

  function addRow() {
    const scope = newRow.scope.trim();
    if (scope !== "global" && !/^job:[a-z0-9_]+$/.test(scope)) {
      setMsg({ kind: "err", text: "Scope must be 'global' or 'job:<kind>' (e.g. job:segment_research)" });
      return;
    }
    const key = `${newRow.provider}/${scope}`;
    if (drafts[key]) { setMsg({ kind: "err", text: "That budget already exists — edit it in the table" }); return; }
    void save(newRow.provider, scope, { per_run: "", per_day: "", hard_stop: false, alert_pct: "80" });
  }

  if (rows === null) return <Card style={{ padding: 24, color: A.muted, fontSize: 13 }}>Loading budgets…</Card>;

  return (
    <>
      <Card style={{ padding: "14px 18px", marginBottom: 16, fontSize: 12.5, color: A.body, lineHeight: 1.55 }}>
        Budgets are checked <strong>before every paid call</strong> of a guarded job (today: Segment research —
        DataForSEO + Bedrock). Each cap follows its own row: <Badge color="red">stops</Badge> ends the run when the next
        call would pass the cap; <Badge color="amber">alert only</Badge> never stops and sends an admin alert at the
        alert %. A <em>job</em> row&apos;s per-run cap applies to each run of that job; daily caps count all of today&apos;s
        spend (UTC). A stopped run keeps what it already bought and can be re-run after raising the cap.
      </Card>

      {msg && (
        <div style={{ marginBottom: 14, padding: "10px 12px", borderRadius: 8, fontSize: 12.5,
                      background: msg.kind === "ok" ? A.greenSoft : A.redSoft, color: msg.kind === "ok" ? A.green : A.red,
                      border: `1px solid ${msg.kind === "ok" ? A.green : A.redBorder}` }}>
          {msg.text}
        </div>
      )}

      <Card style={{ padding: 0, overflow: "hidden", marginBottom: 16 }}>
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
            <thead>
              <tr>
                <th style={TH}>Provider</th>
                <th style={TH}>Applies to</th>
                <th style={TH}>Per run (USD)</th>
                <th style={TH}>Per day (USD)</th>
                <th style={TH}>At the cap</th>
                <th style={TH}>Alert at</th>
                <th style={{ ...TH, textAlign: "right" }}>Spent today</th>
                <th style={TH}>Last changed</th>
                <th style={TH} />
              </tr>
            </thead>
            <tbody>
              {rows.map(r => {
                const key = `${r.provider}/${r.scope}`;
                const d = drafts[key] ?? toDraft(r);
                const dirty = JSON.stringify(d) !== JSON.stringify(toDraft(r));
                const set = (p: Partial<Draft>) => setDrafts(prev => ({ ...prev, [key]: { ...d, ...p } }));
                const today = spent[r.provider] ?? 0;
                const dayCap = r.per_day_usd;
                const usedPct = dayCap ? (100 * today) / dayCap : null;
                return (
                  <tr key={key}>
                    <td style={{ ...TD, fontWeight: 600 }}>{PROVIDER_LABEL[r.provider] ?? r.provider}</td>
                    <td style={TD}>{scopeLabel(r.scope)}</td>
                    <td style={TD}><input aria-label={`${key} per run`} style={input} value={d.per_run} placeholder="no cap" onChange={e => set({ per_run: e.target.value })} /></td>
                    <td style={TD}><input aria-label={`${key} per day`} style={input} value={d.per_day} placeholder="no cap" onChange={e => set({ per_day: e.target.value })} /></td>
                    <td style={TD}>
                      <select aria-label={`${key} mode`} value={d.hard_stop ? "stop" : "alert"} onChange={e => set({ hard_stop: e.target.value === "stop" })}
                              style={{ ...input, width: 110, fontFamily: "inherit" }}>
                        <option value="stop">Stop</option>
                        <option value="alert">Alert only</option>
                      </select>
                    </td>
                    <td style={TD}><input aria-label={`${key} alert pct`} style={{ ...input, width: 56 }} value={d.alert_pct} onChange={e => set({ alert_pct: e.target.value })} />%</td>
                    <td style={{ ...TD, textAlign: "right", fontFamily: mono, whiteSpace: "nowrap" }}>
                      ${today.toFixed(4)}
                      {usedPct != null && (
                        <div style={{ fontSize: 11, color: usedPct >= r.alert_pct ? A.red : A.muted }}>{Math.round(usedPct)}% of daily</div>
                      )}
                    </td>
                    <td style={{ ...TD, color: A.muted, fontSize: 11.5 }}>
                      {r.updated_by ?? "—"}
                      {r.updated_at && <div>{formatDateTime(r.updated_at)}</div>}
                    </td>
                    <td style={{ ...TD, whiteSpace: "nowrap" }}>
                      <Btn size="sm" disabled={!dirty || saving === key} onClick={() => save(r.provider, r.scope, d)}>
                        {saving === key ? "Saving…" : "Save"}
                      </Btn>
                      {dirty && <Btn size="sm" variant="ghost" onClick={() => set(toDraft(r))}>Reset</Btn>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <Card style={{ padding: "14px 18px" }}>
        <SLabel>Add a budget</SLabel>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 12.5 }}>
          <select aria-label="new provider" value={newRow.provider} onChange={e => setNewRow({ ...newRow, provider: e.target.value })}
                  style={{ ...input, width: 130, fontFamily: "inherit" }}>
            {PROVIDERS.map(p => <option key={p} value={p}>{PROVIDER_LABEL[p]}</option>)}
          </select>
          <input aria-label="new scope" style={{ ...input, width: 220 }} value={newRow.scope}
                 onChange={e => setNewRow({ ...newRow, scope: e.target.value })} placeholder="global or job:<kind>" />
          <Btn size="sm" onClick={addRow}>Add</Btn>
          <span style={{ color: A.muted }}>Created with no caps and alert-only; set the caps in the table after adding.</span>
        </div>
      </Card>
    </>
  );
}
