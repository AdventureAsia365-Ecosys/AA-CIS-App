"use client";
// app/admin/places/MergesTab.tsx — AA-847 Problem 2: the automatic duplicate-place merge (owner decision S246:
// duplicates are merged automatically; an admin does not merge by hand).
//   Countries — the allow-list (geo.place_dedupe_country): off → dry_run (the paid two-model check runs, report
//     only) → apply (writes the dry-run diff an admin approved, for the rule version in force). The last place_dedupe
//     run per country: planned merges, the model band, conflicts, the diff hash to approve.
//   Merge log — every merge (job and admin), newest first, with its reason; undo = dry run, then apply (clears
//     duplicate_of, restores the pointers, pins the pair "different" so the job never re-makes it).
// API: GET /admin/places/dedupe, POST /admin/places/dedupe/countries | /dedupe/run, GET /admin/places/merges,
// POST /admin/places/merges/{id}/undo.

import { useCallback, useEffect, useMemo, useState } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { GitMerge, Play, Undo2 } from "lucide-react";
import { A, mono, Card, SLabel, Badge, Btn, LoadingScreen, StatCard } from "../_components/adminUi";
import { DataTable, formatDateTime } from "../../_kit";

type CountryRun = {
  status: string | null; listed: string | null; usable: number; merges: number; llm_needed: number; possible: number;
  conflicts: number; defects: number; diff_size: number; diff_hash: string; outside?: number; breaker_limit?: number;
  applied?: number; band_pairs?: number; band_asked?: number; band_cached?: number; band_unasked?: number;
  embedding_promoted?: number;
  merges_planned: { merged_id: string; keeper_id: string; decided_by: string; score: number; reason: string }[];
};
type AllowRow = {
  country: string; mode: string; approved_diff_hash: string | null; approved_diff_size: number; rule_version: string;
  updated_by: string; updated_at: string | null;
};
type DedupeStatus = {
  rule_version: string; allow_list: Record<string, AllowRow>;
  last_run: null | { job_id: string; finished_at: string | null; mode: string; rule_version: string;
    model_calls: number | null; model_cost_usd: number | null; countries: Record<string, CountryRun> };
};
type Merge = {
  id: string; merged_id: string; merged_name: string; keeper_id: string; keeper_name: string; country: string;
  decided_by: string; rule_version: string; score: number | null; reason: string | null; km: number | null;
  repointed_counts: Record<string, number>; created_at: string | null; created_by: string; undone_at: string | null;
  undone_by: string | null; undo_reason: string | null;
};
type MergePage = { total: number; page: number; page_size: number; items: Merge[] };

const selectStyle = { padding: "6px 8px", borderRadius: 8, border: `1px solid ${A.line}`, fontSize: 13 };
const linkBtn = { border: 0, background: "none", color: A.accentDeep, cursor: "pointer", padding: 0, fontSize: 12 };
const STATUS_COLOR: Record<string, "green" | "amber" | "red" | "gray" | "blue"> = {
  applied: "green", dry_run: "blue", held_volume: "red", rule_version_changed: "amber",
};

async function post(path: string, body: unknown) {
  const r = await fetch(`/api/admin/places/${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(JSON.stringify(out.detail ?? r.status));
  return out;
}

export default function MergesTab({ onError }: { onError: (msg: string) => void }) {
  const [status, setStatus] = useState<DedupeStatus | null>(null);
  const [log, setLog] = useState<MergePage | null>(null);
  const [country, setCountry] = useState("");
  const [active, setActive] = useState("true");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [busy, setBusy] = useState<string | null>(null);
  const [undo, setUndo] = useState<{ merge: Merge; result: Record<string, unknown> } | null>(null);

  const loadStatus = useCallback(async () => {
    const r = await fetch("/api/admin/places/dedupe");
    if (!r.ok) { onError(`Dedupe status failed (${r.status})`); return; }
    setStatus(await r.json());
  }, [onError]);

  const loadLog = useCallback(async () => {
    const qs = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (country) qs.set("country", country);
    if (active) qs.set("active", active);
    const r = await fetch(`/api/admin/places/merges?${qs}`);
    if (!r.ok) { onError(`Merge log failed (${r.status})`); return; }
    setLog(await r.json());
  }, [country, active, page, pageSize, onError]);

  /* eslint-disable react-hooks/set-state-in-effect -- fetch on mount / filter change, same pattern as every admin page */
  useEffect(() => { loadStatus(); }, [loadStatus]);
  useEffect(() => { loadLog(); }, [loadLog]);
  /* eslint-enable react-hooks/set-state-in-effect */

  async function act(key: string, fn: () => Promise<unknown>) {
    setBusy(key);
    try { await fn(); } catch (e) { onError(`${key} failed: ${(e as Error).message}`); } finally { setBusy(null); }
  }

  const setMode = (c: string, mode: string, diffHash?: string) => act(`${c}:${mode}`, async () => {
    await post("dedupe/countries", { country: c, mode, diff_hash: diffHash ?? null });
    await loadStatus();
  });
  const runNow = (mode: string, c?: string) => act(`run:${mode}`, async () => {
    await post("dedupe/run", { mode, country: c ?? null });
    await loadStatus();
  });
  const runUndo = (m: Merge, dryRun: boolean) => act(`undo:${m.id}`, async () => {
    const out = await post(`merges/${m.id}/undo`, { dry_run: dryRun });
    setUndo(dryRun ? { merge: m, result: out } : null);
    if (!dryRun) await Promise.all([loadLog(), loadStatus()]);
  });

  const columns = useMemo<ColumnDef<Merge, unknown>[]>(() => [
    { id: "pair", header: "Merged → keeper", accessorFn: m => `${m.merged_name} → ${m.keeper_name}`,
      cell: ({ row }) => <>{row.original.merged_name} <span style={{ color: A.muted }}>→</span> {row.original.keeper_name}
        <div style={{ fontSize: 11, color: A.muted, fontFamily: mono }}>{row.original.merged_id.slice(0, 8)} → {row.original.keeper_id.slice(0, 8)}</div></> },
    { accessorKey: "country", header: "Country" },
    { accessorKey: "decided_by", header: "Decided by", cell: ({ row }) => <Badge color={row.original.decided_by === "admin" ? "purple" : row.original.decided_by === "llm" ? "blue" : "gray"}>{row.original.decided_by}</Badge> },
    { id: "why", header: "Why", accessorFn: m => m.reason ?? "",
      cell: ({ row }) => <span style={{ fontSize: 12, fontFamily: mono }}>{row.original.reason ?? "—"}
        {row.original.km != null && <span style={{ color: A.muted }}> · {row.original.km} km</span>}
        {row.original.score != null && <span style={{ color: A.muted }}> · {row.original.score}</span>}</span> },
    { id: "moved", header: "Rows moved", accessorFn: m => Object.values(m.repointed_counts).reduce((a, b) => a + b, 0),
      meta: { align: "right" } },
    { accessorKey: "created_at", header: "When", cell: ({ row }) => <span style={{ fontSize: 12 }}>{formatDateTime(row.original.created_at)}</span> },
    { id: "undo", header: "", accessorFn: m => m.undone_at ?? "",
      cell: ({ row }) => {
        const m = row.original;
        if (m.undone_at) return <span style={{ fontSize: 12, color: A.muted }}>undone {formatDateTime(m.undone_at)} · {m.undo_reason}</span>;
        if (m.decided_by === "admin") return <span style={{ fontSize: 12, color: A.muted }}>undo in Content validity</span>;
        return <button disabled={!!busy} style={linkBtn} onClick={() => runUndo(m, true)}><Undo2 size={12} /> undo…</button>;
      } },
  // eslint-disable-next-line react-hooks/exhaustive-deps -- runUndo only reads state setters and the loaders
  ], [busy]);

  if (!status) return <LoadingScreen msg="Loading the duplicate merge..." />;
  const run = status.last_run;
  const countries = Array.from(new Set([...Object.keys(status.allow_list), ...Object.keys(run?.countries ?? {})])).sort();

  return (
    <>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14, marginBottom: 18 }}>
        <StatCard label="Rule version" value={status.rule_version} icon={<GitMerge size={16} />}
                  sub={run ? `last run ${formatDateTime(run.finished_at)} (${run.mode})` : "no run yet"} />
        <StatCard label="Countries on apply" value={String(Object.values(status.allow_list).filter(a => a.mode === "apply").length)}
                  sub={`${Object.keys(status.allow_list).length} on the allow-list`} />
        <StatCard label="Model check, last run" value={run?.model_calls != null ? String(run.model_calls) : "—"}
                  sub={run?.model_cost_usd != null ? `$${run.model_cost_usd.toFixed(4)}` : "calls"} />
        <StatCard label="Merges in the log" value={log ? String(log.total) : "—"} sub={active === "true" ? "active" : "all"} />
      </div>

      <Card style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <SLabel>Countries</SLabel>
          <div style={{ display: "flex", gap: 8 }}>
            <Btn size="sm" disabled={!!busy} onClick={() => runNow("dry_run")}><Play size={12} /> Dry run now</Btn>
            <Btn size="sm" disabled={!!busy} onClick={() => runNow("apply")}><Play size={12} /> Run (apply where approved)</Btn>
          </div>
        </div>
        <div style={{ fontSize: 12, color: A.muted, margin: "4px 0 10px" }}>
          A country on the list gets the paid two-model check (GPT-6 Luna both ways, then Claude Haiku). Apply writes the
          approved dry-run diff; more than max(5, 1%) changes outside it hold the country and alert.
        </div>
        {countries.length === 0 && <div style={{ fontSize: 13, color: A.body }}>No run yet.</div>}
        {countries.map(c => {
          const r = run?.countries[c];
          const al = status.allow_list[c];
          return (
            <div key={c} style={{ borderTop: `1px solid ${A.line2}`, padding: "10px 0", fontSize: 13, color: A.body }}>
              <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                <strong style={{ minWidth: 110 }}>{c}</strong>
                <Badge color={al ? (al.mode === "apply" ? "green" : "blue") : "gray"}>{al ? al.mode : "off"}</Badge>
                {r?.status && <Badge color={STATUS_COLOR[r.status] ?? "gray"}>{r.status}</Badge>}
                {r && <span style={{ fontFamily: mono, fontSize: 12 }}>
                  {r.merges} merges · {r.band_pairs ?? 0} model-band pairs ({r.band_asked ?? 0} asked, {r.band_cached ?? 0} cached,
                  {" "}{r.band_unasked ?? 0} not asked) · {r.possible} possible · {r.conflicts} conflicts
                  {r.outside != null && ` · ${r.outside} outside the approval (limit ${r.breaker_limit})`}
                </span>}
                <span style={{ flex: 1 }} />
                {!al && <Btn size="sm" disabled={!!busy} onClick={() => setMode(c, "dry_run")}>Add (dry run)</Btn>}
                {al && r?.diff_hash && r.status !== "applied" && (
                  <Btn variant="primary" size="sm" disabled={!!busy}
                       onClick={() => { if (window.confirm(`Approve ${r.diff_size} changes for ${c} and switch it to apply?`)) setMode(c, "apply", r.diff_hash); }}>
                    Approve diff ({r.diff_size})
                  </Btn>
                )}
                {al && <Btn size="sm" disabled={!!busy} onClick={() => setMode(c, al.mode === "apply" ? "dry_run" : "off")}>
                  {al.mode === "apply" ? "Back to dry run" : "Remove"}</Btn>}
              </div>
              {r && r.merges_planned.length > 0 && (
                <details style={{ marginTop: 6 }}>
                  <summary style={{ cursor: "pointer", fontSize: 12, color: A.muted }}>planned merges (first {r.merges_planned.length}) · diff {r.diff_hash.slice(0, 10)}</summary>
                  {r.merges_planned.map(m => (
                    <div key={m.merged_id} style={{ fontSize: 12, fontFamily: mono }}>
                      {m.merged_id.slice(0, 8)} → {m.keeper_id.slice(0, 8)} · {m.decided_by} · {m.reason}
                    </div>
                  ))}
                </details>
              )}
            </div>
          );
        })}
      </Card>

      {undo && (
        <Card style={{ marginBottom: 16 }}>
          <SLabel>Undo — dry run</SLabel>
          <div style={{ fontSize: 13, color: A.body, marginBottom: 8 }}>
            {undo.merge.merged_name} → {undo.merge.keeper_name}: rows to restore{" "}
            <span style={{ fontFamily: mono }}>{JSON.stringify(undo.result.would_restore ?? {})}</span>. The pair is then pinned
            &quot;different&quot; and the job never merges it again.
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <Btn variant="danger" size="sm" disabled={!!busy}
                 onClick={() => { if (window.confirm("Undo this merge?")) runUndo(undo.merge, false); }}>Undo the merge</Btn>
            <Btn size="sm" onClick={() => setUndo(null)}>Close</Btn>
          </div>
        </Card>
      )}

      <Card style={{ marginBottom: 16, display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <SLabel>Merge log</SLabel>
        <select aria-label="Country" value={country} onChange={e => { setCountry(e.target.value); setPage(1); }} style={selectStyle}>
          <option value="">All countries</option>
          {countries.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        <select aria-label="State" value={active} onChange={e => { setActive(e.target.value); setPage(1); }} style={selectStyle}>
          <option value="true">Active</option>
          <option value="false">Undone</option>
          <option value="">All</option>
        </select>
      </Card>
      {log && (
        <DataTable<Merge> tableId="admin-places-merges" data={log.items} columns={columns} getRowId={m => m.id}
                          serverPagination={{ pageIndex: log.page - 1, pageSize: log.page_size, total: log.total,
                                              onChange: (pi, ps) => { setPage(ps !== pageSize ? 1 : pi + 1); setPageSize(ps); } }}
                          enableCsv csvFilename="places-merges.csv"
                          emptyTitle="No merges" emptyDescription="The job has not merged anything for these filters." />
      )}
    </>
  );
}
