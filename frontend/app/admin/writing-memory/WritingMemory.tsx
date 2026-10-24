"use client";
// app/admin/writing-memory/WritingMemory.tsx — AA-917 WM-14: Tenant output › Writing memory (app ADR 0010).
//
// Two tabs:
//   Signals     the writing signals (edits, reason codes, gate failures) as the miner sees them: totals, the month's
//               learning-loop spend against its $25 cap, the mining trigger, the clusters over threshold (basis,
//               support, open rule, masked samples), and the signal list with its fact status. A grey signal waits
//               for the admin's pass / fail.
//   Candidates  the rules the miner drafted: text, scope, support, lint result; a drawer with the provenance
//               (every signal, with its source), edit (the lint runs again) and reject (with a reason).
// Header actions queue a job: backfill (or its dry run), fact filter, or a manual mining run (the paid one: a confirm
// step shows the month's spend against the cap first; the API refuses it with its reason when the trigger does not
// hold), or the R0 baseline (AA-918, $0: the gates re-run on stored T9 texts against their stored ledgers). Automatic
// runs are off unless the worker sets WRITING_MEMORY_AUTO_MINE.
// AA-918: the drawer runs R0 on a rule ($0) and shows its result; a lexicon rule of a T9 writer whose R0 passed on its
// current text can be activated in one click (the L0 fast path: the next T9 write gets it) and rolled back in one
// click. Every other rule reaches a writer only through a release (AA-919).
//
// API through the BFF (/api/admin/writing-memory/*; admin only: lib/rbac.ts PAGE_RULES, API_READ_RULES and
// API_WRITE_RULES; backend tenant.read / ai.admin).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import {
  Badge, Button, ConfirmModal, DataTable, Drawer, ErrorState, K, PageHeader, StatusBadge, TableSkeleton, Tabs, apiGet,
  formatDateTime, mono, useToast, type ColumnDef,
} from "../../_kit";
import {
  L0_WRITERS, LINT_CHECKS, LINT_LABEL, WM_API, factTone, lintCheck, owner, r0Of, ruleTone, usd, wmSend,
  type Cluster, type ClustersResponse, type Example, type R0Result, type Rule, type RuleDetail, type RulesPage,
  type Signal, type SignalsPage, type Summary,
} from "@/lib/writingMemory";

const TABS = [{ key: "signals", label: "Signals" }, { key: "candidates", label: "Candidates" }];

function Stat({ label, value, hint }: { label: string; value: React.ReactNode; hint?: React.ReactNode }) {
  return (
    <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 10, padding: "12px 14px",
      minWidth: 150, flex: "1 1 150px" }}>
      <div style={{ fontSize: 11.5, color: K.muted }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 600, color: K.ink, marginTop: 2 }}>{value}</div>
      {hint && <div style={{ fontSize: 11.5, color: K.muted2, marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

function Pair({ ex }: { ex: Example }) {
  return (
    <div style={{ fontSize: 12, lineHeight: 1.45, display: "grid", gap: 2 }}>
      <div><span style={{ color: K.danger }}>before</span> {ex.before || "—"}</div>
      {ex.after !== null && <div><span style={{ color: K.success }}>after</span> {ex.after || "(cut)"}</div>}
      {ex.problem && <div style={{ color: K.muted }}>gate: {ex.problem}</div>}
    </div>
  );
}

function SummaryBar({ s }: { s: Summary }) {
  const fact = s.signals.fact_status || {};
  const t = s.mine_trigger;
  return (
    <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 16 }}>
      <Stat label="Signals" value={s.signals.total}
        hint={`${fact.pass ?? 0} pass · ${fact.grey ?? 0} grey · ${fact.fail ?? 0} fail · ${fact.pending ?? 0} pending`} />
      <Stat label="Candidates" value={Object.values(s.rules.candidate || {}).reduce((a, b) => a + b, 0)}
        hint={`${Object.values(s.rules.rejected || {}).reduce((a, b) => a + b, 0)} rejected`} />
      <Stat label="Learning-loop spend this month" value={usd(s.budget.month_spent_usd)}
        hint={`cap ${usd(s.budget.month_cap_usd)} · ${usd(s.budget.run_cap_usd)} / run`} />
      <Stat label="Next automatic run" value={t.auto_enabled === false ? "off" : t.auto_refused ? "waiting" : "due"}
        hint={t.auto_refused ?? `${t.new_pass} new pass signals`} />
    </div>
  );
}

// ── Signals tab ──────────────────────────────────────────────────────────────────────────────────────────────────

function SignalsTab() {
  const toast = useToast();
  const qc = useQueryClient();
  const [fact, setFact] = useState<string>("");
  const clusters = useQuery({ queryKey: ["wm", "clusters"],
    queryFn: () => apiGet<ClustersResponse>(`${WM_API}/signals/clusters`) });
  const signals = useQuery({ queryKey: ["wm", "signals", fact],
    queryFn: () => apiGet<SignalsPage>(`${WM_API}/signals?limit=200${fact ? `&fact_status=${fact}` : ""}`) });

  const clusterCols = useMemo<ColumnDef<Cluster, unknown>[]>(() => [
    { id: "owner", accessorFn: (c) => owner(c.scope, c.tenant_id), header: "Scope",
      cell: (c) => <Badge tone={c.row.original.scope === "platform" ? "purple" : "info"}>
        {owner(c.row.original.scope, c.row.original.tenant_id)}</Badge> },
    { accessorKey: "writer", header: "Writer" },
    { accessorKey: "meaning", header: "What repeats",
      cell: (c) => <span title={c.row.original.label}>{c.row.original.meaning}</span> },
    { id: "support", accessorFn: (c) => c.support.signals, header: "Support",
      cell: (c) => { const s = c.row.original.support;
        return <span style={{ fontSize: 12 }}>{s.signals} signals · {s.pieces} pieces
          {s.tenants ? ` · ${s.tenants} tenants` : ""}{s.reviewers ? ` · ${s.reviewers} reviewers` : ""}</span>; } },
    { accessorKey: "basis", header: "Basis" },
    { id: "state", accessorFn: (c) => (c.has_open_rule ? "rule" : c.draftable ? "draftable" : "waiting"),
      header: "State", cell: (c) => { const x = c.row.original;
        return x.has_open_rule ? <Badge tone="success">has a rule</Badge>
          : x.draftable ? <Badge tone="warning">next run drafts it</Badge> : <Badge>needs 3 signals</Badge>; } },
    { id: "sample", header: "Sample (masked)", enableSorting: false,
      cell: (c) => (c.row.original.samples[0] ? <Pair ex={c.row.original.samples[0]} /> : "—") },
  ], []);

  const signalCols = useMemo<ColumnDef<Signal, unknown>[]>(() => {
    const setVerdict = async (id: string, status: "pass" | "fail") => {
      const r = await wmSend(`/signals/${id}/fact`, "PATCH", { status });
      if (r.ok) {
        toast.success(`Signal marked ${status}`);
        qc.invalidateQueries({ queryKey: ["wm"] });
      } else toast.error(r.message);
    };
    return [
    { accessorKey: "created_at", header: "When",
      cell: (c) => <span style={{ fontSize: 12, whiteSpace: "nowrap" }}>{formatDateTime(c.row.original.created_at)}</span> },
    { id: "owner", accessorFn: (s) => owner(s.scope, s.tenant_id), header: "Scope" },
    { accessorKey: "writer", header: "Writer" },
    { id: "what", accessorFn: (s) => `${s.action} ${s.reason_code ?? s.gate ?? ""}`, header: "Action",
      cell: (c) => { const s = c.row.original;
        return <span style={{ fontSize: 12 }}>{s.action} · {s.actor_type}{s.reason_code ? ` · ${s.reason_code}` : ""}
          {s.gate ? <span style={{ fontFamily: mono, color: K.muted }}> {s.gate}</span> : null}</span>; } },
    { id: "text", header: "Before → after", enableSorting: false,
      cell: (c) => <Pair ex={{ before: c.row.original.before_text ?? "", after: c.row.original.after_text,
        problem: null }} /> },
    { accessorKey: "fact_status", header: "Facts",
      cell: (c) => { const s = c.row.original;
        return (
          <div style={{ display: "grid", gap: 4, justifyItems: "start" }} onClick={(e) => e.stopPropagation()}>
            <Badge tone={factTone(s.fact_status)}>{s.fact_status}</Badge>
            {s.fact_status === "grey" && (
              <div style={{ display: "flex", gap: 4 }}>
                <Button size="sm" onClick={() => setVerdict(s.signal_id, "pass")}>Pass</Button>
                <Button size="sm" variant="danger" onClick={() => setVerdict(s.signal_id, "fail")}>Fail</Button>
              </div>
            )}
          </div>
        ); } },
    ];
  }, [qc, toast]);

  return (
    <div style={{ display: "grid", gap: 20 }}>
      <section>
        <h2 style={{ fontSize: 15, margin: "0 0 4px" }}>Clusters over threshold</h2>
        <div style={{ fontSize: 12.5, color: K.muted, marginBottom: 8 }}>
          Tenant rule: ≥ 2 edits on ≥ 2 pieces. Platform rule from tenant edits: ≥ 3 tenants. A1: ≥ 2 reviewers, or a
          gate failing on ≥ 5% of the last 30 days&apos; pieces. Only pass signals count.
        </div>
        {clusters.isLoading ? <TableSkeleton rows={4} cols={6} /> : clusters.error ? (
          <ErrorState message={(clusters.error as Error).message} onRetry={() => clusters.refetch()} />
        ) : (
          <DataTable tableId="wm-clusters" data={clusters.data?.clusters ?? []} columns={clusterCols}
            getRowId={(c) => `${c.scope}:${c.tenant_id ?? "-"}:${c.cluster_key}`} tableMinWidth={900}
            emptyTitle="No cluster over threshold yet"
            emptyDescription={`${clusters.data?.pass_signals ?? 0} pass signals in the last 90 days.`} />
        )}
      </section>
      <section>
        <h2 style={{ fontSize: 15, margin: "0 0 8px" }}>Signals</h2>
        {signals.error ? <ErrorState message={(signals.error as Error).message} onRetry={() => signals.refetch()} /> : (
          <DataTable tableId="wm-signals" data={signals.data?.items ?? []} columns={signalCols}
            getRowId={(s) => s.signal_id} loading={signals.isLoading} tableMinWidth={900}
            emptyTitle="No signal" emptyDescription="Edits are captured on the edit paths; run the backfill for older ones."
            toolbarExtra={
              <select value={fact} onChange={(e) => setFact(e.target.value)} aria-label="Fact status"
                style={{ fontSize: 12, padding: "6px 8px", borderRadius: 8, border: `1px solid ${K.line}`,
                  background: K.card, color: K.ink }}>
                <option value="">Every fact status</option>
                {["pending", "grey", "pass", "fail"].map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            } />
        )}
      </section>
    </div>
  );
}

// ── Candidates tab ───────────────────────────────────────────────────────────────────────────────────────────────

function LintBadges({ rule }: { rule: Rule }) {
  return (
    <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
      {LINT_CHECKS.map((k) => {
        const c = lintCheck(rule, k);
        if (!c) return null;
        return <Badge key={k} tone={c.ok ? "success" : "danger"} dot={false}>
          <span title={c.detail || ""}>{LINT_LABEL[k]}</span></Badge>;
      })}
    </div>
  );
}

function counts(m: Record<string, number>): string {
  const e = Object.entries(m);
  return e.length ? e.map(([g, n]) => `${g} ×${n}`).join(", ") : "none";
}

function R0Panel({ r0, fresh }: { r0: R0Result; fresh: boolean }) {
  const k = r0.recurrence.k;
  const have = r0.recurrence.have;
  const b = r0.baseline;
  return (
    <div style={{ display: "grid", gap: 4, fontSize: 12.5, lineHeight: 1.5 }}>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {r0.l0_applicable && <Badge tone={r0.l0_ok ? "success" : "danger"}>L0 {r0.l0_ok ? "passed" : "failed"}</Badge>}
        <Badge tone={r0.r2_eligible ? "success" : "warning"}>{r0.r2_eligible ? "recurs (R2 eligible)" : "under k"}</Badge>
        {!fresh && <Badge tone="warning">stale: the rule changed after this run</Badge>}
        <Badge dot={false}>{formatDateTime(r0.computed_at)} · ${r0.cost_usd.toFixed(2)}</Badge>
      </div>
      {r0.l0_reason && <div style={{ color: K.danger }}>{r0.l0_reason}</div>}
      <div>Phrase in stored text: {r0.phrase.before} → {r0.phrase.after} after the rule · new hard stops:{" "}
        {counts(r0.new_hard_stops)}</div>
      <div>Would fire on {r0.pieces.fires} of {r0.pieces.pieces} pieces ({r0.pieces.texts} texts replayed,
        {" "}{r0.pieces.source_exempt} source-exempt{r0.pieces.errors ? `, ${r0.pieces.errors} errors` : ""})</div>
      <div>Past failures it would have prevented: {r0.prevented.total} ({r0.prevented.blocking} blocking) —
        {" "}{counts(r0.prevented.by_gate)}</div>
      <div>Signals matched: {r0.signals.matched} ({r0.signals.explained_edits} edits, {r0.signals.system} system) ·
        last {r0.recurrence.window_days} days: {have.signals} signals on {have.pieces} pieces
        {k.tenants ? `, ${have.tenants}/${k.tenants} tenants` : ""}
        {k.reviewers ? `, ${have.reviewers}/${k.reviewers} reviewers` : ""}
        {" "}(k = {k.signals} signals on {k.pieces} pieces, basis {r0.recurrence.basis ?? "none"})</div>
      <div style={{ color: K.muted }}>Baseline (rule off, stored ledgers):{" "}
        {b.agreement === null ? "no stored ledger"
          : `${(b.agreement * 100).toFixed(1)}% of ${b.gate_checks} gate checks agree; `
            + `${b.pieces_agree}/${b.pieces_compared} texts fully`}</div>
    </div>
  );
}

function RuleDrawer({ ruleId, onClose }: { ruleId: string | null; onClose: () => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["wm", "rule", ruleId], enabled: !!ruleId,
    queryFn: () => apiGet<RuleDetail>(`${WM_API}/candidates/${ruleId}`) });
  const [draft, setDraft] = useState<{ text: string; replacement: string; phrases: string } | null>(null);
  const [reason, setReason] = useState("");
  const rule = q.data;
  const editing = draft && rule;

  async function save() {
    if (!rule || !draft) return;
    const r = await wmSend<Rule>(`/candidates/${rule.rule_id}`, "PATCH", {
      rule_text: draft.text, replacement: draft.replacement || null,
      match_phrases: draft.phrases.split("\n").map((p) => p.trim()).filter(Boolean),
    });
    if (r.ok) {
      toast[r.data.status === "candidate" ? "success" : "info"](
        r.data.status === "candidate" ? "Saved; the lint passed" : "Saved; the lint failed, so the rule is rejected");
      setDraft(null);
      qc.invalidateQueries({ queryKey: ["wm"] });
    } else toast.error(r.message);
  }

  async function act(path: "r0" | "activate" | "rollback", body?: unknown) {
    if (!rule) return;
    const r = await wmSend<{ job_id?: string; status?: string }>(`/candidates/${rule.rule_id}/${path}`, "POST", body);
    if (r.ok) {
      toast.success(path === "r0" ? `R0 queued (job ${(r.data.job_id || "").slice(0, 8)}); reopen the rule when it ends`
        : path === "activate" ? "Active: the next T9 write gets this rule" : "Rolled back: the next write no longer gets it");
      setReason("");
      qc.invalidateQueries({ queryKey: ["wm"] });
    } else toast.error(r.message);
  }

  async function reject() {
    if (!rule) return;
    const r = await wmSend<Rule>(`/candidates/${rule.rule_id}/reject`, "POST", { reason });
    if (r.ok) {
      toast.success("Rule rejected");
      setReason("");
      qc.invalidateQueries({ queryKey: ["wm"] });
    } else toast.error(r.message);
  }

  return (
    <Drawer open={!!ruleId} onClose={() => { setDraft(null); onClose(); }} width={620}
      title={rule ? `${rule.kind} · ${owner(rule.scope, rule.tenant_id)} · ${rule.writer}` : "Rule"}>
      {q.isLoading || !rule ? <TableSkeleton rows={5} cols={2} /> : (
        <div style={{ display: "grid", gap: 14, fontSize: 13 }}>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            <Badge tone={ruleTone(rule.status)}>{rule.status}</Badge>
            <Badge>{rule.token_estimate} tokens</Badge>
            <Badge>expires {formatDateTime(rule.expires_at)}</Badge>
            {rule.drafted_by && <Badge dot={false}>{rule.drafted_by}</Badge>}
          </div>
          {editing ? (
            <div style={{ display: "grid", gap: 8 }}>
              <label>Rule (≤ 25 words)
                <textarea value={draft.text} onChange={(e) => setDraft({ ...draft, text: e.target.value })} rows={3}
                  style={{ width: "100%", fontSize: 13 }} /></label>
              {rule.kind === "lexicon" && <label>Replacement (empty = ban)
                <input value={draft.replacement} onChange={(e) => setDraft({ ...draft, replacement: e.target.value })}
                  style={{ width: "100%", fontSize: 13 }} /></label>}
              <label>Match phrases (one per line, ≤ 5)
                <textarea value={draft.phrases} onChange={(e) => setDraft({ ...draft, phrases: e.target.value })}
                  rows={3} style={{ width: "100%", fontSize: 13, fontFamily: mono }} /></label>
              <div style={{ display: "flex", gap: 8 }}>
                <Button variant="primary" onClick={save}>Save and lint</Button>
                <Button variant="ghost" onClick={() => setDraft(null)}>Cancel</Button>
              </div>
            </div>
          ) : (
            <div style={{ display: "grid", gap: 6 }}>
              <div style={{ fontSize: 15, color: K.ink }}>{rule.rule_text}
                {rule.replacement && <> → <b>{rule.replacement}</b></>}</div>
              <div style={{ color: K.muted, fontFamily: mono, fontSize: 12 }}>matches: {rule.match_phrases.join(" · ")}</div>
              <LintBadges rule={rule} />
              {rule.rejected_reason && <div style={{ color: K.danger }}>Rejected: {rule.rejected_reason}</div>}
            </div>
          )}
          <div style={{ color: K.muted, fontSize: 12.5 }}>
            Support: {rule.support_signals} signals on {rule.support_pieces} pieces
            {rule.support_tenants ? `, ${rule.support_tenants} tenants` : ""}
            {rule.support_reviewers ? `, ${rule.support_reviewers} reviewers` : ""} · cluster{" "}
            <span style={{ fontFamily: mono }}>{rule.cluster_key}</span>
          </div>
          {rule.status === "candidate" && !editing && (
            <div style={{ display: "grid", gap: 8, borderTop: `1px solid ${K.line}`, paddingTop: 12 }}>
              <Button onClick={() => setDraft({ text: rule.rule_text, replacement: rule.replacement ?? "",
                phrases: rule.match_phrases.join("\n") })}>Edit</Button>
              <div style={{ display: "flex", gap: 8 }}>
                <input placeholder="Why reject it?" value={reason} onChange={(e) => setReason(e.target.value)}
                  style={{ flex: 1, fontSize: 13 }} aria-label="Reject reason" />
                <Button variant="danger" disabled={reason.trim().length < 3} onClick={reject}>Reject</Button>
              </div>
            </div>
          )}
          <div style={{ display: "grid", gap: 8, borderTop: `1px solid ${K.line}`, paddingTop: 12 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <h3 style={{ fontSize: 13, margin: 0 }}>R0 replay</h3>
              <Button variant="ghost" onClick={() => act("r0")}
                title="Replays the rule over stored texts, gate ledgers and signals; no model call, $0">Run R0</Button>
              {rule.kind === "lexicon" && L0_WRITERS.includes(rule.writer) && rule.status !== "active" && (
                <Button variant="primary" disabled={!!rule.l0_activation} onClick={() => act("activate")}
                  title={rule.l0_activation?.detail || "The next T9 write of this owner gets the rule"}>
                  Activate (L0)</Button>)}
            </div>
            {rule.l0_activation && rule.kind === "lexicon" && rule.status !== "active" && (
              <div style={{ color: K.muted, fontSize: 12 }}>Not activatable yet: {rule.l0_activation.detail}</div>)}
            {r0Of(rule) ? <R0Panel r0={r0Of(rule) as R0Result} fresh={rule.r0_fresh} />
              : <div style={{ color: K.muted, fontSize: 12.5 }}>No R0 run on this rule yet.</div>}
            {rule.status === "active" && (
              <div style={{ display: "flex", gap: 8 }}>
                <input placeholder="Why roll it back?" value={reason} onChange={(e) => setReason(e.target.value)}
                  style={{ flex: 1, fontSize: 13 }} aria-label="Rollback reason" />
                <Button variant="danger" disabled={reason.trim().length < 3}
                  onClick={() => act("rollback", { reason })}>Roll back</Button>
              </div>
            )}
          </div>
          <div>
            <h3 style={{ fontSize: 13, margin: "6px 0" }}>Provenance ({rule.provenance.length})</h3>
            <div style={{ display: "grid", gap: 8 }}>
              {rule.provenance.map((s) => (
                <div key={s.signal_id} style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: 8 }}>
                  <div style={{ fontSize: 11.5, color: K.muted, marginBottom: 4 }}>
                    {formatDateTime(s.created_at)} · {s.action} · {s.source_table}{" "}
                    <span style={{ fontFamily: mono }}>{s.source_id.slice(0, 8)}</span> ·{" "}
                    <Badge tone={factTone(s.fact_status)} dot={false}>{s.fact_status}</Badge>
                  </div>
                  <Pair ex={{ before: s.before_text ?? "", after: s.after_text, problem: s.problem }} />
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </Drawer>
  );
}

function CandidatesTab() {
  const [status, setStatus] = useState("candidate");
  const [open, setOpen] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["wm", "rules", status],
    queryFn: () => apiGet<RulesPage>(`${WM_API}/candidates?limit=200${status ? `&status=${status}` : ""}`) });
  const cols = useMemo<ColumnDef<Rule, unknown>[]>(() => [
    { accessorKey: "created_at", header: "Drafted",
      cell: (c) => <span style={{ fontSize: 12, whiteSpace: "nowrap" }}>{formatDateTime(c.row.original.created_at)}</span> },
    { id: "owner", accessorFn: (r) => owner(r.scope, r.tenant_id), header: "Scope",
      cell: (c) => <Badge tone={c.row.original.scope === "platform" ? "purple" : "info"}>
        {owner(c.row.original.scope, c.row.original.tenant_id)}</Badge> },
    { accessorKey: "writer", header: "Writer" },
    { accessorKey: "kind", header: "Kind" },
    { accessorKey: "rule_text", header: "Rule",
      cell: (c) => <span>{c.row.original.rule_text}{c.row.original.replacement ? ` → ${c.row.original.replacement}` : ""}</span> },
    { accessorKey: "support_signals", header: "Support" },
    { id: "lint", header: "Lint", enableSorting: false, cell: (c) => <LintBadges rule={c.row.original} /> },
    { accessorKey: "status", header: "Status",
      cell: (c) => <Badge tone={ruleTone(c.row.original.status)}>{c.row.original.status}</Badge> },
  ], []);
  return (
    <>
      {q.error ? <ErrorState message={(q.error as Error).message} onRetry={() => q.refetch()} /> : (
        <DataTable tableId="wm-rules" data={q.data?.items ?? []} columns={cols} getRowId={(r) => r.rule_id}
          loading={q.isLoading} onRowClick={(r) => setOpen(r.rule_id)} tableMinWidth={900}
          emptyTitle="No rule here" emptyDescription="A mining run drafts one rule per cluster over threshold."
          toolbarExtra={
            <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status"
              style={{ fontSize: 12, padding: "6px 8px", borderRadius: 8, border: `1px solid ${K.line}`,
                background: K.card, color: K.ink }}>
              <option value="">Every status</option>
              {["candidate", "rejected", "approved", "active", "expired"].map((s) =>
                <option key={s} value={s}>{s}</option>)}
            </select>
          } />
      )}
      <RuleDrawer ruleId={open} onClose={() => setOpen(null)} />
    </>
  );
}

// ── page ─────────────────────────────────────────────────────────────────────────────────────────────────────────

export default function WritingMemory() {
  const toast = useToast();
  const qc = useQueryClient();
  const [tab, setTab] = useState("signals");
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmMine, setConfirmMine] = useState(false);
  const summary = useQuery({ queryKey: ["wm", "summary"], queryFn: () => apiGet<Summary>(`${WM_API}/summary`) });
  const budget = summary.data?.budget;

  async function run(kind: "backfill" | "fact_filter" | "mine" | "r0_baseline", dryRun = false) {
    setBusy(kind);
    const r = await wmSend<{ job_id: string }>("/run", "POST", dryRun ? { kind, dry_run: true } : { kind });
    setBusy(null);
    setConfirmMine(false);
    if (r.ok) toast.success(`Queued (job ${r.data.job_id.slice(0, 8)}); see Platform › Jobs`);
    else toast.error(r.message);
    qc.invalidateQueries({ queryKey: ["wm"] });
  }

  return (
    <div style={{ padding: "8px 0" }}>
      <PageHeader title="Writing memory"
        breadcrumbs={[{ label: "Tenant output" }, { label: "Writing memory" }]}
        description="What the writers could learn from edits and repeated gate failures. Rules are drafted here and
          reach a writer through an approved release; a lexicon rule proven by R0 can go live at once (L0)."
        actions={<>
          <Button variant="ghost" onClick={() => qc.invalidateQueries({ queryKey: ["wm"] })}>
            <RefreshCw size={14} /> Refresh</Button>
          <Button variant="ghost" onClick={() => run("backfill", true)} disabled={!!busy}
            title="Counts what the backfill would copy, per source and tenant; inserts nothing">
            Backfill (dry run)</Button>
          <Button onClick={() => run("backfill")} disabled={!!busy}>Backfill signals</Button>
          <Button onClick={() => run("fact_filter")} disabled={!!busy}>Run fact filter</Button>
          <Button variant="ghost" onClick={() => run("r0_baseline")} disabled={!!busy}
            title="R0 sanity check, $0: re-runs the gates on stored T9 texts and compares with their stored ledgers">
            R0 baseline</Button>
          <Button variant="primary" onClick={() => setConfirmMine(true)} disabled={!!busy}
            title="A paid run: at most 50 calls and $1, within the $25 month cap">
            <Play size={14} /> Mine rules</Button>
        </>} />
      <ConfirmModal open={confirmMine} onClose={() => setConfirmMine(false)} onConfirm={() => run("mine")}
        busy={busy === "mine"} title="Start a paid mining run?" confirmLabel="Mine rules"
        body={<div style={{ fontSize: 13, lineHeight: 1.5 }}>
          <p style={{ margin: "0 0 8px" }}>One run drafts a rule per cluster over threshold on the judge model: at most
            {" "}{budget?.max_calls_per_run ?? 50} calls and {usd(budget?.run_cap_usd ?? 1)}.</p>
          <p style={{ margin: 0 }}>Spent this month: <strong>{usd(budget?.month_spent_usd ?? 0)}</strong> of the
            {" "}{usd(budget?.month_cap_usd ?? 25)} cap. A run is refused when the month is over the cap or 3 runs
            already ran this month.</p>
        </div>} />
      {summary.data && <SummaryBar s={summary.data} />}
      {summary.data?.last_jobs && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 14, fontSize: 12, color: K.muted }}>
          {Object.entries(summary.data.last_jobs).map(([kind, j]) => (
            <span key={kind}>{kind}: <StatusBadge status={j.status} /> {formatDateTime(j.created_at)}</span>
          ))}
        </div>
      )}
      <div style={{ marginBottom: 14 }}><Tabs tabs={TABS} active={tab} onChange={setTab} /></div>
      {tab === "signals" ? <SignalsTab /> : <CandidatesTab />}
    </div>
  );
}
