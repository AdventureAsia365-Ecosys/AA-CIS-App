"use client";
// app/admin/writing-memory/WritingMemory.tsx — AA-917 WM-14: Tenant output › Writing memory (app ADR 0010).
//
// Six tabs:
//   Signals     the writing signals (edits, reason codes, gate failures) as the miner sees them: totals, the month's
//               learning-loop spend against its $25 cap, the mining trigger, the clusters over threshold (basis,
//               support, open rule, masked samples), and the signal list with its fact status. A grey signal waits
//               for the admin's pass / fail.
//   Candidates  the rules the miner drafted: text, scope, support, lint result; a drawer with the provenance
//               (every signal, with its source), edit (the lint runs again) and reject (with a reason).
//   Releases    AA-919 WM-22 (ReleasesTab.tsx): the memory switch and train close per writer, every release with its
//               evidence verdicts, and a drawer with the release actions (attach evidence, approve, activate, roll
//               back, reject); New draft and Reverify scan above the list.
//   Exemplars   AA-921 WM-29 (ExemplarsTab.tsx): the pinned voice examples of T2 / T9 per owner, writer and channel,
//               the next-draft preview and the pin / unpin / never-use drawer.
//   Metrics     AA-922 WM-31 (MetricsTab.tsx): the weekly KPI report per writer, the soft stops and what the job did
//               (Keep), the expired rules, the project stop rule and the weekly disagreement queue.
//   Guide       how to use the page, in plain words (AA-919): guide.ts rendered by the kit GuidePanel, like every
//               admin page's Guide tab. The description's "New here? Read the Guide" link opens it.
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
// AA-994: every string comes from messages.ts (English default, Vietnamese when picked); the Guide from guide.ts /
// guide.vi.ts.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { PageFrame,
  Badge, Button, ConfirmModal, DataTable, Drawer, ErrorState, GuidePanel, K, PageHeader, StatusBadge, TableSkeleton,
  Tabs, apiGet, mono, useFormat, useLocalized, useT, useToast, type ColumnDef,
  Input, Select, Textarea,
  LinkButton,
} from "../../_kit";
import {
  L0_WRITERS, LINT_CHECKS, WM_API, factTone, lintCheck, r0Of, ruleTone, usd,
  type Cluster, type ClustersResponse, type Example, type FactStatus, type R0Result, type Rule, type RuleDetail,
  type RulesPage, type RuleStatus, type Signal, type SignalsPage, type Summary,
} from "@/lib/writingMemory";
import { LINT_LABEL_MESSAGES } from "@/lib/writingMemory.messages";
import { WRITING_MEMORY_GUIDE } from "./guide";
import { WRITING_MEMORY_GUIDE_VI } from "./guide.vi";
import { FACT_STATUS, M, RULE_STATUS } from "./messages";
import ExemplarsTab from "./ExemplarsTab";
import { X } from "./exemplars.messages";
import { MX } from "./metrics.messages";
import MetricsTab from "./MetricsTab";
import ReleasesTab from "./ReleasesTab";
import { useWmText } from "./useWmText";

const FACT_STATUSES: FactStatus[] = ["pending", "grey", "pass", "fail"];
const RULE_STATUSES: RuleStatus[] = ["candidate", "rejected", "approved", "active", "expired"];

// overflowWrap anywhere: a hint can hold one long token (the "automatic mining is off (<the env switch name>)" reason),
// which at 390 stuck 10 px out of its tile and gave the frame a sideways scroll on every tab (S260 F3).
function Stat({ label, value, hint }: { label: string; value: React.ReactNode; hint?: React.ReactNode }) {
  return (
    <div style={{ background: K.card, border: `1px solid ${K.line}`, borderRadius: 10, padding: "12px 14px",
      minWidth: "min(150px, 100%)", flex: "1 1 150px", overflowWrap: "anywhere" }}>
      <div style={{ fontSize: 11.5, color: K.muted }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 600, color: K.ink, marginTop: 2 }}>{value}</div>
      {hint && <div style={{ fontSize: 11.5, color: K.muted2, marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

function Pair({ ex }: { ex: Example }) {
  const t = useT(M);
  return (
    <div style={{ fontSize: 12, lineHeight: 1.45, display: "grid", gap: 2 }}>
      <div><span style={{ color: K.danger }}>{t("before")}</span> {ex.before || "—"}</div>
      {ex.after !== null && <div><span style={{ color: K.success }}>{t("after")}</span> {ex.after || t("cutText")}</div>}
      {ex.problem && <div style={{ color: K.muted }}>{t("gateProblem", ex.problem)}</div>}
    </div>
  );
}

function SummaryBar({ s }: { s: Summary }) {
  const t = useT(M);
  const fact = s.signals.fact_status || {};
  const trig = s.mine_trigger;
  return (
    <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 16 }}>
      <Stat label={t("statSignals")} value={s.signals.total}
        hint={t("factCounts", String(fact.pass ?? 0), String(fact.grey ?? 0), String(fact.fail ?? 0),
          String(fact.pending ?? 0))} />
      <Stat label={t("statCandidates")} value={Object.values(s.rules.candidate || {}).reduce((a, b) => a + b, 0)}
        hint={t("rejectedCount", Object.values(s.rules.rejected || {}).reduce((a, b) => a + b, 0))} />
      <Stat label={t("statSpend")} value={usd(s.budget.month_spent_usd)}
        hint={t("capLine", usd(s.budget.month_cap_usd), usd(s.budget.run_cap_usd))} />
      <Stat label={t("statNextRun")}
        value={trig.auto_enabled === false ? t("runOff") : trig.auto_refused ? t("runWaiting") : t("runDue")}
        hint={trig.auto_refused ?? t("newPass", trig.new_pass)} />
    </div>
  );
}

// ── Signals tab ──────────────────────────────────────────────────────────────────────────────────────────────────

function SignalsTab() {
  const toast = useToast();
  const qc = useQueryClient();
  const t = useT(M);
  const factLabel = useT(FACT_STATUS);
  const wm = useWmText();
  const { formatDateTime } = useFormat();
  const [fact, setFact] = useState<string>("");
  const clusters = useQuery({ queryKey: ["wm", "clusters"],
    queryFn: () => apiGet<ClustersResponse>(`${WM_API}/signals/clusters`) });
  const signals = useQuery({ queryKey: ["wm", "signals", fact],
    queryFn: () => apiGet<SignalsPage>(`${WM_API}/signals?limit=200${fact ? `&fact_status=${fact}` : ""}`) });

  const clusterCols = useMemo<ColumnDef<Cluster, unknown>[]>(() => [
    { id: "owner", accessorFn: (c) => wm.owner(c.scope, c.tenant_id), header: t("colScope"),
      cell: (c) => <Badge tone={c.row.original.scope === "platform" ? "purple" : "info"}>
        {wm.owner(c.row.original.scope, c.row.original.tenant_id)}</Badge> },
    { accessorKey: "writer", header: t("colWriter") },
    { accessorKey: "meaning", header: t("colWhatRepeats"),
      cell: (c) => <span title={c.row.original.label}>{c.row.original.meaning}</span> },
    { id: "support", accessorFn: (c) => c.support.signals, header: t("colSupport"),
      cell: (c) => { const s = c.row.original.support;
        return <span style={{ fontSize: 12 }}>{t("supportCounts", s.signals, s.pieces)}
          {s.tenants ? t("supportTenants", s.tenants) : ""}{s.reviewers ? t("supportReviewers", s.reviewers) : ""}
        </span>; } },
    { accessorKey: "basis", header: t("colBasis") },
    { id: "state", accessorFn: (c) => (c.has_open_rule ? "rule" : c.draftable ? "draftable" : "waiting"),
      header: t("colState"), cell: (c) => { const x = c.row.original;
        return x.has_open_rule ? <Badge tone="success">{t("hasRule")}</Badge>
          : x.draftable ? <Badge tone="warning">{t("nextRunDrafts")}</Badge> : <Badge>{t("needsThree")}</Badge>; } },
    { id: "sample", header: t("colSample"), enableSorting: false,
      cell: (c) => (c.row.original.samples[0] ? <Pair ex={c.row.original.samples[0]} /> : "—") },
  ], [t, wm]);

  const signalCols = useMemo<ColumnDef<Signal, unknown>[]>(() => {
    const setVerdict = async (id: string, status: "pass" | "fail") => {
      const r = await wm.send(`/signals/${id}/fact`, "PATCH", { status });
      if (r.ok) {
        toast.success(status === "pass" ? t("signalMarkedPass") : t("signalMarkedFail"));
        qc.invalidateQueries({ queryKey: ["wm"] });
      } else toast.error(r.message);
    };
    return [
    { accessorKey: "created_at", header: t("colWhen"),
      cell: (c) => <span style={{ fontSize: 12, whiteSpace: "nowrap" }}>{formatDateTime(c.row.original.created_at)}</span> },
    { id: "owner", accessorFn: (s) => wm.owner(s.scope, s.tenant_id), header: t("colScope") },
    { accessorKey: "writer", header: t("colWriter") },
    { id: "what", accessorFn: (s) => `${s.action} ${s.reason_code ?? s.gate ?? ""}`, header: t("colAction"),
      cell: (c) => { const s = c.row.original;
        return <span style={{ fontSize: 12 }}>{s.action} · {s.actor_type}{s.reason_code ? ` · ${s.reason_code}` : ""}
          {s.gate ? <span style={{ fontFamily: mono, color: K.muted }}> {s.gate}</span> : null}</span>; } },
    { id: "text", header: t("colBeforeAfter"), enableSorting: false,
      cell: (c) => <Pair ex={{ before: c.row.original.before_text ?? "", after: c.row.original.after_text,
        problem: null }} /> },
    { accessorKey: "fact_status", header: t("colFacts"),
      cell: (c) => { const s = c.row.original;
        return (
          <div style={{ display: "grid", gap: 4, justifyItems: "start" }} onClick={(e) => e.stopPropagation()}>
            <Badge tone={factTone(s.fact_status)}>{factLabel(s.fact_status)}</Badge>
            {s.fact_status === "grey" && (
              <div style={{ display: "flex", gap: 4 }}>
                <Button size="sm" onClick={() => setVerdict(s.signal_id, "pass")}>{t("markPass")}</Button>
                <Button size="sm" variant="danger" onClick={() => setVerdict(s.signal_id, "fail")}>{t("markFail")}</Button>
              </div>
            )}
          </div>
        ); } },
    ];
  }, [qc, toast, t, factLabel, wm, formatDateTime]);

  // minmax(0, 1fr), like ReleasesTab: an `auto` track would grow to the tables' min-content width and clip them.
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 20 }}>
      <section>
        <h2 style={{ fontSize: 15, margin: "0 0 4px" }}>{t("clustersTitle")}</h2>
        <div style={{ fontSize: 12.5, color: K.muted, marginBottom: 8 }}>{t("clustersHelp")}</div>
        {clusters.isLoading ? <TableSkeleton rows={4} cols={6} /> : clusters.error ? (
          <ErrorState message={(clusters.error as Error).message} onRetry={() => clusters.refetch()} />
        ) : (
          <DataTable tableId="wm-clusters" data={clusters.data?.clusters ?? []} columns={clusterCols}
            getRowId={(c) => `${c.scope}:${c.tenant_id ?? "-"}:${c.cluster_key}`} tableMinWidth={900}
            emptyTitle={t("clustersEmpty")}
            emptyDescription={t("clustersEmptyDesc", clusters.data?.pass_signals ?? 0)} />
        )}
      </section>
      <section>
        <h2 style={{ fontSize: 15, margin: "0 0 8px" }}>{t("signalsTitle")}</h2>
        {signals.error ? <ErrorState message={(signals.error as Error).message} onRetry={() => signals.refetch()} /> : (
          <DataTable tableId="wm-signals" data={signals.data?.items ?? []} columns={signalCols}
            getRowId={(s) => s.signal_id} loading={signals.isLoading} tableMinWidth={900}
            emptyTitle={t("signalsEmpty")} emptyDescription={t("signalsEmptyDesc")}
            toolbarExtra={
              <Select value={fact} onChange={(e) => setFact(e.target.value)} aria-label={t("factStatusAria")}
                style={{ fontSize: 12, padding: "6px 8px", borderRadius: 8, border: `1px solid ${K.line}`,
                  background: K.card, color: K.ink }}>
                <option value="">{t("everyFactStatus")}</option>
                {FACT_STATUSES.map((s) => <option key={s} value={s}>{factLabel(s)}</option>)}
              </Select>
            } />
        )}
      </section>
    </div>
  );
}

// ── Candidates tab ───────────────────────────────────────────────────────────────────────────────────────────────

function LintBadges({ rule }: { rule: Rule }) {
  const lintLabel = useT(LINT_LABEL_MESSAGES);
  return (
    <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
      {LINT_CHECKS.map((k) => {
        const c = lintCheck(rule, k);
        if (!c) return null;
        return <Badge key={k} tone={c.ok ? "success" : "danger"} dot={false}>
          <span title={c.detail || ""}>{lintLabel(k)}</span></Badge>;
      })}
    </div>
  );
}

function counts(m: Record<string, number>, none: string): string {
  const e = Object.entries(m);
  return e.length ? e.map(([g, n]) => `${g} ×${n}`).join(", ") : none;
}

function R0Panel({ r0, fresh }: { r0: R0Result; fresh: boolean }) {
  const t = useT(M);
  const { formatDateTime } = useFormat();
  const k = r0.recurrence.k;
  const have = r0.recurrence.have;
  const b = r0.baseline;
  return (
    <div style={{ display: "grid", gap: 4, fontSize: 12.5, lineHeight: 1.5 }}>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {r0.l0_applicable && <Badge tone={r0.l0_ok ? "success" : "danger"}>{r0.l0_ok ? t("l0Passed") : t("l0Failed")}</Badge>}
        <Badge tone={r0.r2_eligible ? "success" : "warning"}>{r0.r2_eligible ? t("recurs") : t("underK")}</Badge>
        {!fresh && <Badge tone="warning">{t("stale")}</Badge>}
        <Badge dot={false}>{formatDateTime(r0.computed_at)} · ${r0.cost_usd.toFixed(2)}</Badge>
      </div>
      {r0.l0_reason && <div style={{ color: K.danger }}>{r0.l0_reason}</div>}
      <div>{t("phraseLine", r0.phrase.before, r0.phrase.after, counts(r0.new_hard_stops, t("none")))}</div>
      <div>{t("textsLine", r0.pieces.fires, r0.pieces.texts, r0.pieces.pieces, r0.pieces.source_exempt)}
        {r0.pieces.errors ? t("textsErrors", r0.pieces.errors) : ""})</div>
      <div>{t("preventedLine", r0.prevented.total, r0.prevented.blocking, counts(r0.prevented.by_gate, t("none")))}</div>
      <div>{t("signalsMatched", r0.signals.matched, r0.signals.explained_edits, r0.signals.system,
        r0.recurrence.window_days, have.signals, have.pieces)}
        {k.tenants ? t("matchedTenants", have.tenants, k.tenants) : ""}
        {k.reviewers ? t("matchedReviewers", have.reviewers, k.reviewers) : ""}
        {t("kLine", k.signals, k.pieces, r0.recurrence.basis ?? t("none"))}</div>
      <div style={{ color: K.muted }}>{t("baseline")}{" "}
        {b.agreement === null ? t("noLedger")
          : t("agreement", (b.agreement * 100).toFixed(1), b.gate_checks, b.pieces_agree, b.pieces_compared)}</div>
    </div>
  );
}

function RuleDrawer({ ruleId, onClose }: { ruleId: string | null; onClose: () => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const t = useT(M);
  const ruleStatus = useT(RULE_STATUS);
  const factLabel = useT(FACT_STATUS);
  const wm = useWmText();
  const { formatDateTime } = useFormat();
  const q = useQuery({ queryKey: ["wm", "rule", ruleId], enabled: !!ruleId,
    queryFn: () => apiGet<RuleDetail>(`${WM_API}/candidates/${ruleId}`) });
  const [draft, setDraft] = useState<{ text: string; replacement: string; phrases: string } | null>(null);
  const [reason, setReason] = useState("");
  const rule = q.data;
  const editing = draft && rule;

  async function save() {
    if (!rule || !draft) return;
    const r = await wm.send<Rule>(`/candidates/${rule.rule_id}`, "PATCH", {
      rule_text: draft.text, replacement: draft.replacement || null,
      match_phrases: draft.phrases.split("\n").map((p) => p.trim()).filter(Boolean),
    });
    if (r.ok) {
      toast[r.data.status === "candidate" ? "success" : "info"](
        r.data.status === "candidate" ? t("savedLintPassed") : t("savedLintFailed"));
      setDraft(null);
      qc.invalidateQueries({ queryKey: ["wm"] });
    } else toast.error(r.message);
  }

  async function act(path: "r0" | "activate" | "rollback", body?: unknown) {
    if (!rule) return;
    const r = await wm.send<{ job_id?: string; status?: string }>(`/candidates/${rule.rule_id}/${path}`, "POST", body);
    if (r.ok) {
      toast.success(path === "r0" ? t("r0Queued", (r.data.job_id || "").slice(0, 8))
        : path === "activate" ? t("activated") : t("rolledBack"));
      setReason("");
      qc.invalidateQueries({ queryKey: ["wm"] });
    } else toast.error(r.message);
  }

  async function reject() {
    if (!rule) return;
    const r = await wm.send<Rule>(`/candidates/${rule.rule_id}/reject`, "POST", { reason });
    if (r.ok) {
      toast.success(t("ruleRejected"));
      setReason("");
      qc.invalidateQueries({ queryKey: ["wm"] });
    } else toast.error(r.message);
  }

  return (
    <Drawer open={!!ruleId} onClose={() => { setDraft(null); onClose(); }} width={620}
      title={rule ? `${rule.kind} · ${wm.owner(rule.scope, rule.tenant_id)} · ${rule.writer}` : t("rule")}>
      {q.isLoading || !rule ? <TableSkeleton rows={5} cols={2} /> : (
        <div style={{ display: "grid", gap: 14, fontSize: 13 }}>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            <Badge tone={ruleTone(rule.status)}>{ruleStatus(rule.status)}</Badge>
            <Badge>{t("tokens", rule.token_estimate)}</Badge>
            <Badge>{t("expires", formatDateTime(rule.expires_at))}</Badge>
            {rule.drafted_by && <Badge dot={false}>{rule.drafted_by}</Badge>}
          </div>
          {editing ? (
            <div style={{ display: "grid", gap: 8 }}>
              <label>{t("ruleField")}
                <Textarea value={draft.text} onChange={(e) => setDraft({ ...draft, text: e.target.value })} rows={3}
                  style={{ width: "100%", fontSize: 13 }} /></label>
              {rule.kind === "lexicon" && <label>{t("replacementField")}
                <Input value={draft.replacement} onChange={(e) => setDraft({ ...draft, replacement: e.target.value })}
                  style={{ width: "100%", fontSize: 13 }} /></label>}
              <label>{t("phrasesField")}
                <Textarea value={draft.phrases} onChange={(e) => setDraft({ ...draft, phrases: e.target.value })}
                  rows={3} style={{ width: "100%", fontSize: 13, fontFamily: mono }} /></label>
              <div style={{ display: "flex", gap: 8 }}>
                <Button variant="primary" onClick={save}>{t("saveAndLint")}</Button>
                <Button variant="ghost" onClick={() => setDraft(null)}>{t("cancel")}</Button>
              </div>
            </div>
          ) : (
            <div style={{ display: "grid", gap: 6 }}>
              <div style={{ fontSize: 15, color: K.ink }}>{rule.rule_text}
                {rule.replacement && <> → <b>{rule.replacement}</b></>}</div>
              <div style={{ color: K.muted, fontFamily: mono, fontSize: 12 }}>{t("matches", rule.match_phrases.join(" · "))}</div>
              <LintBadges rule={rule} />
              {rule.rejected_reason && <div style={{ color: K.danger }}>{t("rejectedBecause", rule.rejected_reason)}</div>}
            </div>
          )}
          <div style={{ color: K.muted, fontSize: 12.5 }}>
            {t("supportRule", rule.support_signals, rule.support_pieces)}
            {rule.support_tenants ? t("supportRuleTenants", rule.support_tenants) : ""}
            {rule.support_reviewers ? t("supportRuleReviewers", rule.support_reviewers) : ""} · {t("cluster")}{" "}
            <span style={{ fontFamily: mono }}>{rule.cluster_key}</span>
          </div>
          {rule.status === "candidate" && !editing && (
            <div style={{ display: "grid", gap: 8, borderTop: `1px solid ${K.line}`, paddingTop: 12 }}>
              <Button onClick={() => setDraft({ text: rule.rule_text, replacement: rule.replacement ?? "",
                phrases: rule.match_phrases.join("\n") })}>{t("edit")}</Button>
              <div style={{ display: "flex", gap: 8 }}>
                <Input placeholder={t("whyReject")} value={reason} onChange={(e) => setReason(e.target.value)}
                  style={{ flex: 1, fontSize: 13 }} aria-label={t("rejectAria")} />
                <Button variant="danger" disabled={reason.trim().length < 3} onClick={reject}>{t("reject")}</Button>
              </div>
            </div>
          )}
          <div style={{ display: "grid", gap: 8, borderTop: `1px solid ${K.line}`, paddingTop: 12 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <h3 style={{ fontSize: 13, margin: 0 }}>{t("r0Replay")}</h3>
              <Button variant="ghost" onClick={() => act("r0")} title={t("runR0Title")}>{t("runR0")}</Button>
              {rule.kind === "lexicon" && L0_WRITERS.includes(rule.writer) && rule.status !== "active" && (
                <Button variant="primary" disabled={!!rule.l0_activation} onClick={() => act("activate")}
                  title={rule.l0_activation?.detail || t("activateL0Title")}>
                  {t("activateL0")}</Button>)}
            </div>
            {rule.l0_activation && rule.kind === "lexicon" && rule.status !== "active" && (
              <div style={{ color: K.muted, fontSize: 12 }}>{t("notActivatable", rule.l0_activation.detail)}</div>)}
            {r0Of(rule) ? <R0Panel r0={r0Of(rule) as R0Result} fresh={rule.r0_fresh} />
              : <div style={{ color: K.muted, fontSize: 12.5 }}>{t("noR0")}</div>}
            <div style={{ color: K.muted2, fontSize: 11.5 }}>{t("r0Help")}</div>
            {rule.status === "active" && (
              <div style={{ display: "flex", gap: 8 }}>
                <Input placeholder={t("whyRollback")} value={reason} onChange={(e) => setReason(e.target.value)}
                  style={{ flex: 1, fontSize: 13 }} aria-label={t("rollbackAria")} />
                <Button variant="danger" disabled={reason.trim().length < 3}
                  onClick={() => act("rollback", { reason })}>{t("rollBack")}</Button>
              </div>
            )}
          </div>
          <div>
            <h3 style={{ fontSize: 13, margin: "6px 0" }}>{t("provenance", rule.provenance.length)}</h3>
            <div style={{ display: "grid", gap: 8 }}>
              {rule.provenance.map((s) => (
                <div key={s.signal_id} style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: 8 }}>
                  <div style={{ fontSize: 11.5, color: K.muted, marginBottom: 4 }}>
                    {formatDateTime(s.created_at)} · {s.action} · {s.source_table}{" "}
                    <span style={{ fontFamily: mono }}>{s.source_id.slice(0, 8)}</span> ·{" "}
                    <Badge tone={factTone(s.fact_status)} dot={false}>{factLabel(s.fact_status)}</Badge>
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
  const t = useT(M);
  const ruleStatus = useT(RULE_STATUS);
  const wm = useWmText();
  const { formatDateTime } = useFormat();
  const [status, setStatus] = useState("candidate");
  const [open, setOpen] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["wm", "rules", status],
    queryFn: () => apiGet<RulesPage>(`${WM_API}/candidates?limit=200${status ? `&status=${status}` : ""}`) });
  const cols = useMemo<ColumnDef<Rule, unknown>[]>(() => [
    { accessorKey: "created_at", header: t("colDrafted"),
      cell: (c) => <span style={{ fontSize: 12, whiteSpace: "nowrap" }}>{formatDateTime(c.row.original.created_at)}</span> },
    { id: "owner", accessorFn: (r) => wm.owner(r.scope, r.tenant_id), header: t("colScope"),
      cell: (c) => <Badge tone={c.row.original.scope === "platform" ? "purple" : "info"}>
        {wm.owner(c.row.original.scope, c.row.original.tenant_id)}</Badge> },
    { accessorKey: "writer", header: t("colWriter") },
    { accessorKey: "kind", header: t("colKind") },
    { accessorKey: "rule_text", header: t("colRule"),
      cell: (c) => <span>{c.row.original.rule_text}{c.row.original.replacement ? ` → ${c.row.original.replacement}` : ""}</span> },
    { accessorKey: "support_signals", header: t("colSupport") },
    { id: "lint", header: t("colLint"), enableSorting: false, cell: (c) => <LintBadges rule={c.row.original} /> },
    { accessorKey: "status", header: t("colStatus"),
      cell: (c) => <Badge tone={ruleTone(c.row.original.status)}>{ruleStatus(c.row.original.status)}</Badge> },
  ], [t, ruleStatus, wm, formatDateTime]);
  return (
    <>
      {q.error ? <ErrorState message={(q.error as Error).message} onRetry={() => q.refetch()} /> : (
        <DataTable tableId="wm-rules" data={q.data?.items ?? []} columns={cols} getRowId={(r) => r.rule_id}
          loading={q.isLoading} onRowClick={(r) => setOpen(r.rule_id)} tableMinWidth={900}
          emptyTitle={t("rulesEmpty")} emptyDescription={t("rulesEmptyDesc")}
          toolbarExtra={
            <Select value={status} onChange={(e) => setStatus(e.target.value)} aria-label={t("statusAria")}
              style={{ fontSize: 12, padding: "6px 8px", borderRadius: 8, border: `1px solid ${K.line}`,
                background: K.card, color: K.ink }}>
              <option value="">{t("everyStatus")}</option>
              {RULE_STATUSES.map((s) => <option key={s} value={s}>{ruleStatus(s)}</option>)}
            </Select>
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
  const t = useT(M);
  const tx = useT(X);
  const tm = useT(MX);
  const wm = useWmText();
  const { formatDateTime } = useFormat();
  const guide = useLocalized({ en: WRITING_MEMORY_GUIDE, vi: WRITING_MEMORY_GUIDE_VI });
  // Daily-use tabs first; the Guide tab last (the kit pattern for a page's Guide tab, app/_kit/guideContent.ts).
  const tabs = [{ key: "signals", label: t("tabSignals") }, { key: "candidates", label: t("tabCandidates") },
    { key: "releases", label: t("tabReleases") }, { key: "exemplars", label: tx("tabExemplars") },
    { key: "metrics", label: tm("tabMetrics") }, { key: "guide", label: t("tabGuide") }];
  const [tab, setTab] = useState("signals");
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmMine, setConfirmMine] = useState(false);
  const summary = useQuery({ queryKey: ["wm", "summary"], queryFn: () => apiGet<Summary>(`${WM_API}/summary`) });
  const budget = summary.data?.budget;

  async function run(kind: "backfill" | "fact_filter" | "mine" | "r0_baseline", dryRun = false) {
    setBusy(kind);
    const r = await wm.send<{ job_id: string }>("/run", "POST", dryRun ? { kind, dry_run: true } : { kind });
    setBusy(null);
    setConfirmMine(false);
    if (r.ok) toast.success(t("queued", r.data.job_id.slice(0, 8)));
    else toast.error(r.message);
    qc.invalidateQueries({ queryKey: ["wm"] });
  }

  return (
    <PageFrame>
      <PageHeader title={t("title")}
        breadcrumbs={[{ label: t("crumbTenantOutput") }, { label: t("title") }]}
        description={<>{t("description")}{" "}
          <LinkButton size="inherit" weight="regular" onClick={() => setTab("guide")} testId="wm-guide-link"
            style={{ textDecoration: "underline", textUnderlineOffset: 2 }}>
            {t("readGuide")}</LinkButton></>}
        actions={<>
          <Button variant="ghost" onClick={() => qc.invalidateQueries({ queryKey: ["wm"] })}>
            <RefreshCw size={14} /> {t("refresh")}</Button>
          <Button variant="ghost" onClick={() => run("backfill", true)} disabled={!!busy}
            title={t("backfillDryRunTitle")}>
            {t("backfillDryRun")}</Button>
          <Button onClick={() => run("backfill")} disabled={!!busy}>{t("backfillSignals")}</Button>
          <Button onClick={() => run("fact_filter")} disabled={!!busy}>{t("runFactFilter")}</Button>
          <Button variant="ghost" onClick={() => run("r0_baseline")} disabled={!!busy} title={t("r0BaselineTitle")}>
            {t("r0Baseline")}</Button>
          <Button variant="primary" onClick={() => setConfirmMine(true)} disabled={!!busy} title={t("mineRulesTitle")}>
            <Play size={14} /> {t("mineRules")}</Button>
        </>} />
      <ConfirmModal open={confirmMine} onClose={() => setConfirmMine(false)} onConfirm={() => run("mine")}
        busy={busy === "mine"} title={t("mineConfirmTitle")} confirmLabel={t("mineRules")}
        body={<div style={{ fontSize: 13, lineHeight: 1.5 }}>
          <p style={{ margin: "0 0 8px" }}>{t("mineBody", budget?.max_calls_per_run ?? 50, usd(budget?.run_cap_usd ?? 1))}</p>
          <p style={{ margin: 0 }}>{t("spentLead")}<strong>{usd(budget?.month_spent_usd ?? 0)}</strong>
            {t("spentRest", usd(budget?.month_cap_usd ?? 25))}</p>
        </div>} />
      {summary.data && <SummaryBar s={summary.data} />}
      {summary.data?.last_jobs && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 14, fontSize: 12, color: K.muted }}>
          {Object.entries(summary.data.last_jobs).map(([kind, j]) => (
            <span key={kind}>{kind}: <StatusBadge status={j.status} /> {formatDateTime(j.created_at)}</span>
          ))}
        </div>
      )}
      <div style={{ marginBottom: 14 }}><Tabs tabs={tabs} active={tab} onChange={setTab} /></div>
      {tab === "signals" ? <SignalsTab /> : tab === "candidates" ? <CandidatesTab />
        : tab === "releases" ? <ReleasesTab />
        : tab === "exemplars" ? <ExemplarsTab />
        : tab === "metrics" ? <MetricsTab />
        : <GuidePanel content={guide} testId="wm-guide" />}
    </PageFrame>
  );
}
