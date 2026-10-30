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

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import { GitMerge, Play, Undo2 } from "lucide-react";
import {
  Button, DataTable, useFormat, useT, Badge, Eyebrow, K, KpiTile, LoadingScreen, Section, mono, type Tone,
} from "../../_kit";
import { PL } from "./messages";

type CountryRun = {
  status: string | null; listed: string | null; usable: number; merges: number; llm_needed: number; possible: number;
  conflicts: number; defects: number; diff_size: number; diff_hash: string; outside?: number; breaker_limit?: number;
  applied?: number; band_pairs?: number; band_asked?: number; band_cached?: number; band_unasked?: number;
  embedding_promoted?: number;
  // S252: counts; the API adds capped detail lists (the job stores full lists).
  conflicts_detail?: { keeper_id: string; destination_id: string; clash: { a: string; b: string; reason: string } | null }[];
  defects_detail?: { a: string; b: string; reason: string }[];
  merges_planned: { merged_id: string; keeper_id: string; decided_by: string; score: number; reason: string }[];
};
// A count field may arrive as a list from an older API build; never render a raw list (React crashes on objects).
const count = (x: unknown): number => (Array.isArray(x) ? x.length : typeof x === "number" ? x : 0);

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

const selectStyle = { padding: "6px 8px", borderRadius: 8, border: `1px solid ${K.line}`, fontSize: 13 };
const linkBtn = { border: 0, background: "none", color: K.accentDeep, cursor: "pointer", padding: 0, fontSize: 12 };
const STATUS_COLOR: Record<string, Tone> = {
  applied: "success", dry_run: "info", held_volume: "danger", rule_version_changed: "warning",
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
  const t = useT(PL);
  const { formatDateTime } = useFormat();
  // The loaders read the translator through a ref, so a language switch does not refetch.
  const tRef = useRef(t);
  useEffect(() => { tRef.current = t; }, [t]);
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
    if (!r.ok) { onError(tRef.current("dedupeFailed", r.status)); return; }
    setStatus(await r.json());
  }, [onError]);

  const loadLog = useCallback(async () => {
    const qs = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (country) qs.set("country", country);
    if (active) qs.set("active", active);
    const r = await fetch(`/api/admin/places/merges?${qs}`);
    if (!r.ok) { onError(tRef.current("mergeLogFailed", r.status)); return; }
    setLog(await r.json());
  }, [country, active, page, pageSize, onError]);

  /* eslint-disable react-hooks/set-state-in-effect -- fetch on mount / filter change, same pattern as every admin page */
  useEffect(() => { loadStatus(); }, [loadStatus]);
  useEffect(() => { loadLog(); }, [loadLog]);
  /* eslint-enable react-hooks/set-state-in-effect */

  async function act(key: string, fn: () => Promise<unknown>) {
    setBusy(key);
    try { await fn(); } catch (e) { onError(t("actionFailed", key, (e as Error).message)); } finally { setBusy(null); }
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
    { id: "pair", header: t("mergedKeeper"), accessorFn: m => `${m.merged_name} → ${m.keeper_name}`,
      cell: ({ row }) => <>{row.original.merged_name} <span style={{ color: K.muted }}>→</span> {row.original.keeper_name}
        <div style={{ fontSize: 11, color: K.muted, fontFamily: mono }}>{row.original.merged_id.slice(0, 8)} → {row.original.keeper_id.slice(0, 8)}</div></> },
    { accessorKey: "country", header: t("country") },
    { accessorKey: "decided_by", header: t("decidedBy"), cell: ({ row }) => <Badge tone={row.original.decided_by === "admin" ? "purple" : row.original.decided_by === "llm" ? "info" : "neutral"}>{row.original.decided_by}</Badge> },
    { id: "why", header: t("why"), accessorFn: m => m.reason ?? "",
      cell: ({ row }) => <span style={{ fontSize: 12, fontFamily: mono }}>{row.original.reason ?? "—"}
        {row.original.km != null && <span style={{ color: K.muted }}> · {row.original.km} km</span>}
        {row.original.score != null && <span style={{ color: K.muted }}> · {row.original.score}</span>}</span> },
    { id: "moved", header: t("rowsMoved"), accessorFn: m => Object.values(m.repointed_counts).reduce((a, b) => a + b, 0),
      meta: { align: "right" } },
    { accessorKey: "created_at", header: t("when"), cell: ({ row }) => <span style={{ fontSize: 12 }}>{formatDateTime(row.original.created_at)}</span> },
    { id: "undo", header: "", accessorFn: m => m.undone_at ?? "",
      cell: ({ row }) => {
        const m = row.original;
        if (m.undone_at) return <span style={{ fontSize: 12, color: K.muted }}>{t("undone", formatDateTime(m.undone_at), m.undo_reason ?? "")}</span>;
        if (m.decided_by === "admin") return <span style={{ fontSize: 12, color: K.muted }}>{t("undoInValidity")}</span>;
        return <button disabled={!!busy} style={linkBtn} onClick={() => runUndo(m, true)}><Undo2 size={12} /> {t("undoLink")}</button>;
      } },
  // eslint-disable-next-line react-hooks/exhaustive-deps -- runUndo only reads state setters and the loaders
  ], [busy, t, formatDateTime]);

  if (!status) return <LoadingScreen message={t("loadingMerge")} />;
  const run = status.last_run;
  const countries = Array.from(new Set([...Object.keys(status.allow_list), ...Object.keys(run?.countries ?? {})])).sort();

  return (
    <>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14, marginBottom: 18 }}>
        <KpiTile label={t("ruleVersion")} value={status.rule_version} icon={<GitMerge size={16} />}
                  hint={run ? t("lastRun", formatDateTime(run.finished_at), run.mode) : t("noRunYet")} />
        <KpiTile label={t("countriesOnApply")} value={String(Object.values(status.allow_list).filter(a => a.mode === "apply").length)}
                  hint={t("onAllowList", Object.keys(status.allow_list).length)} />
        <KpiTile label={t("modelCheck")} value={run?.model_calls != null ? String(run.model_calls) : "—"}
                  hint={run?.model_cost_usd != null ? `$${run.model_cost_usd.toFixed(4)}` : t("calls")} />
        <KpiTile label={t("mergesInLog")} value={log ? String(log.total) : "—"} hint={active === "true" ? t("active") : t("all")} />
      </div>

      <Section style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <Eyebrow>{t("countries")}</Eyebrow>
          <div style={{ display: "flex", gap: 8 }}>
            <Button size="sm" disabled={!!busy} onClick={() => runNow("dry_run")}><Play size={12} /> {t("dryRunNow")}</Button>
            <Button size="sm" disabled={!!busy} onClick={() => runNow("apply")}><Play size={12} /> {t("runApply")}</Button>
          </div>
        </div>
        <div style={{ fontSize: 12, color: K.muted, margin: "4px 0 10px" }}>
          {t("mergeHelp")}
        </div>
        {countries.length === 0 && <div style={{ fontSize: 13, color: K.body }}>{t("noRun")}</div>}
        {countries.map(c => {
          const r = run?.countries[c];
          const al = status.allow_list[c];
          return (
            <div key={c} style={{ borderTop: `1px solid ${K.line2}`, padding: "10px 0", fontSize: 13, color: K.body }}>
              <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                <strong style={{ minWidth: 110 }}>{c}</strong>
                <Badge tone={al ? (al.mode === "apply" ? "success" : "info") : "neutral"}>{al ? al.mode : "off"}</Badge>
                {r?.status && <Badge tone={STATUS_COLOR[r.status] ?? "neutral"}>{r.status}</Badge>}
                {r && <span style={{ fontFamily: mono, fontSize: 12 }}>
                  {t("runLine", r.merges, r.band_pairs ?? 0, r.band_asked ?? 0, r.band_cached ?? 0, r.band_unasked ?? 0,
                    count(r.possible), count(r.conflicts), count(r.defects))}
                  {r.outside != null && t("outside", r.outside, String(r.breaker_limit))}
                </span>}
                <span style={{ flex: 1 }} />
                {!al && <Button size="sm" disabled={!!busy} onClick={() => setMode(c, "dry_run")}>{t("addDryRun")}</Button>}
                {al && r?.diff_hash && r.status !== "applied" && (
                  <Button variant="primary" size="sm" disabled={!!busy}
                       onClick={() => { if (window.confirm(t("confirmApprove", r.diff_size, c))) setMode(c, "apply", r.diff_hash); }}>
                    {t("approveDiff", r.diff_size)}
                  </Button>
                )}
                {al && <Button size="sm" disabled={!!busy} onClick={() => setMode(c, al.mode === "apply" ? "dry_run" : "off")}>
                  {al.mode === "apply" ? t("backToDryRun") : t("remove")}</Button>}
              </div>
              {r && (r.conflicts_detail?.length ?? 0) > 0 && (
                <details style={{ marginTop: 6 }}>
                  <summary style={{ cursor: "pointer", fontSize: 12, color: K.muted }}>{t("conflictsSummary", r.conflicts_detail!.length)}</summary>
                  {r.conflicts_detail!.map(k => (
                    <div key={`${k.keeper_id}-${k.destination_id}`} style={{ fontSize: 12, fontFamily: mono }}>
                      {t("keptApart", k.destination_id.slice(0, 8), k.keeper_id.slice(0, 8))}
                      {k.clash && ` · ${k.clash.a.slice(0, 8)} vs ${k.clash.b.slice(0, 8)}: ${k.clash.reason}`}
                    </div>
                  ))}
                </details>
              )}
              {r && (r.merges_planned?.length ?? 0) > 0 && (
                <details style={{ marginTop: 6 }}>
                  <summary style={{ cursor: "pointer", fontSize: 12, color: K.muted }}>{t("plannedSummary", r.merges_planned.length, (r.diff_hash ?? "").slice(0, 10))}</summary>
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
      </Section>

      {undo && (
        <Section style={{ marginBottom: 16 }}>
          <Eyebrow>{t("undoDryRun")}</Eyebrow>
          <div style={{ fontSize: 13, color: K.body, marginBottom: 8 }}>
            {t("undoBody", `${undo.merge.merged_name} → ${undo.merge.keeper_name}`)}{" "}
            <span style={{ fontFamily: mono }}>{JSON.stringify(undo.result.would_restore ?? {})}</span>{t("undoTail")}
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <Button variant="danger" size="sm" disabled={!!busy}
                 onClick={() => { if (window.confirm(t("confirmUndo"))) runUndo(undo.merge, false); }}>{t("undoMerge")}</Button>
            <Button size="sm" onClick={() => setUndo(null)}>{t("close")}</Button>
          </div>
        </Section>
      )}

      <Section style={{ marginBottom: 16 }} bodyStyle={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <Eyebrow>{t("mergeLog")}</Eyebrow>
        <select aria-label={t("country")} value={country} onChange={e => { setCountry(e.target.value); setPage(1); }} style={selectStyle}>
          <option value="">{t("allCountries")}</option>
          {countries.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        <select aria-label={t("state")} value={active} onChange={e => { setActive(e.target.value); setPage(1); }} style={selectStyle}>
          <option value="true">{t("stateActive")}</option>
          <option value="false">{t("stateUndone")}</option>
          <option value="">{t("stateAll")}</option>
        </select>
      </Section>
      {log && (
        <DataTable<Merge> tableId="admin-places-merges" data={log.items} columns={columns} getRowId={m => m.id}
                          serverPagination={{ pageIndex: log.page - 1, pageSize: log.page_size, total: log.total,
                                              onChange: (pi, ps) => { setPage(ps !== pageSize ? 1 : pi + 1); setPageSize(ps); } }}
                          enableCsv csvFilename="places-merges.csv"
                          emptyTitle={t("noMerges")} emptyDescription={t("noMergesDesc")} />
      )}
    </>
  );
}
