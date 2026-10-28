"use client";
// app/admin/writing-memory/ReleasesTab.tsx — AA-919 WM-22: Writing memory › Releases (app ADR 0010 decision 1).
//
// What it shows:
//   Writers    per writer: the memory switch (the kill switch), the active platform release, how many tenant
//              releases are active, and the month the release train is closed through. Turn memory on / off and
//              Close month are the two writer actions.
//   Releases   every release (server filters: writer, status): number, owner (Platform or the tenant's name, resolved
//              client-side from the shared tenant list: useTenants), status, content class and rule count,
//              train, the evidence verdicts (R0, Eval, Shadow), the reverify flag, who drafted / approved it, when it
//              was created / activated, and its prompt (the prompt register: the sha256 its R0 replay measured).
//   Drawer     one release: its evidence runs with their summary (counts and hashes only), the fingerprint, and
//              the actions its status allows: Attach evidence, Approve, Activate, Roll back, Reject. A release that
//              was ever active also has Check now: the post-activation check in DRY RUN (the writes since its
//              activation carry its rules hash, hold no QA trace and no other tenant's text; $0, no model call,
//              nothing moves), with the verdict and its counts.
// Every action is a backend route (api/routers/admin_writing_memory.py, ai.admin, a signed-in admin id); the buttons
// show only for a role with ai.admin (lib/rbac.ts, a hint: the backend re-checks every call). The API answers a
// refused action with its reason, which the toast prints.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, ShieldCheck } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import {
  Badge, Button, ConfirmModal, DataTable, Drawer, ErrorState, K, Modal, TableSkeleton, apiGet,
  formatDateTime, mono, useAdminIdentity, useToast, type ColumnDef,
} from "../../_kit";
import { can } from "@/lib/rbac";
import { TenantSelect, useTenants } from "../_components/TenantSelect";
import {
  CHECK_CODE_LABELS, EVIDENCE_NEEDED, RELEASE_STATUSES, WM_API, WRITERS, actorLabel, checkSentence, checkTone,
  evidenceOk, evidenceWord, isUuid, monthStart, owner, releaseActions, releaseTone, wmSend,
  type ActiveReleases, type ContentClass, type PostActivationReport, type Release, type ReleaseDetail, type ReleaseRun,
  type ReleasesPage, type Writer, type WriterSwitch,
} from "@/lib/writingMemory";

const SLOTS = [["r0", "R0"], ["eval", "Eval"], ["shadow", "Shadow"]] as const;

const inputStyle: React.CSSProperties = {
  width: "100%", fontSize: 13, padding: "6px 8px", borderRadius: 8, border: `1px solid ${K.line}`,
  background: K.card, color: K.ink, boxSizing: "border-box",
};
const selectStyle: React.CSSProperties = {
  fontSize: 12, padding: "6px 8px", borderRadius: 8, border: `1px solid ${K.line}`, background: K.card,
  color: K.ink, maxWidth: "100%", minWidth: 0,
};

function short(id: string | null | undefined, n = 8): string {
  return id ? id.slice(0, n) : "—";
}

function EvidenceBadges({ r }: { r: Release }) {
  const need = EVIDENCE_NEEDED[r.content_class] ?? [];
  return (
    <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
      {SLOTS.map(([slot, label]) => {
        const run = r.evidence?.[slot] ?? null;
        if (!run && !need.includes(slot)) return null;
        const word = evidenceWord(slot, run, r.content_class);
        const tone = evidenceOk(slot, run, r.content_class) ? "success"
          : run && !run.missing && !run.finished ? "warning" : run ? "danger" : "warning";
        return <Badge key={slot} tone={tone} dot={false}>{label} {word}</Badge>;
      })}
    </div>
  );
}

// ── writers: the memory switch and the train close ───────────────────────────────────────────────────────────────

function WritersPanel({ active, canAdmin }: { active: ActiveReleases | undefined; canAdmin: boolean }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [flip, setFlip] = useState<WriterSwitch | null>(null);
  const [closing, setClosing] = useState<Writer | null>(null);
  const [month, setMonth] = useState("");
  const [busy, setBusy] = useState(false);

  async function patch(writer: Writer, body: object, ok: string) {
    setBusy(true);
    const r = await wmSend<WriterSwitch>(`/writers/${writer}`, "PATCH", body);
    setBusy(false);
    if (r.ok) {
      toast.success(ok);
      setFlip(null);
      setClosing(null);
      setMonth("");
      qc.invalidateQueries({ queryKey: ["wm"] });
    } else toast.error(r.message);
  }

  const rows = (active?.writers ?? []).map((w) => {
    const mine = (active?.items ?? []).filter((r) => r.writer === w.writer);
    return { w, platform: mine.find((r) => r.scope === "platform") ?? null,
      tenants: mine.filter((r) => r.scope === "tenant").length };
  });
  const closeMonth = monthStart(month);
  type Row = (typeof rows)[number];
  const writerCols: ColumnDef<Row, unknown>[] = [
    { id: "writer", accessorFn: (x) => x.w.writer, header: "Writer",
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.w.writer}</span> },
    { id: "memory", accessorFn: (x) => x.w.memory_enabled, header: "Memory",
      cell: (c) => <Badge tone={c.row.original.w.memory_enabled ? "success" : "neutral"}>
        {c.row.original.w.memory_enabled ? "on" : "off"}</Badge> },
    { id: "platform", accessorFn: (x) => x.platform?.release_no ?? -1, header: "Platform release",
      cell: (c) => { const p = c.row.original.platform;
        return p ? <span>#{p.release_no}{p.reverify && <> <Badge tone="warning" dot={false}>reverify</Badge></>}</span>
          : "none active"; } },
    { id: "tenants", accessorFn: (x) => x.tenants, header: "Tenant releases" },
    { id: "train", accessorFn: (x) => x.w.train_closed_month ?? "", header: "Train closed through",
      cell: (c) => (c.row.original.w.train_closed_month ? c.row.original.w.train_closed_month.slice(0, 7) : "open") },
    { id: "actions", header: "", enableSorting: false,
      cell: (c) => { const w = c.row.original.w;
        return canAdmin ? (
          <div style={{ display: "flex", gap: 6, justifyContent: "flex-end", flexWrap: "wrap" }}
            onClick={(e) => e.stopPropagation()}>
            <Button size="sm" variant={w.memory_enabled ? "danger" : "secondary"} onClick={() => setFlip(w)}>
              {w.memory_enabled ? "Turn memory off" : "Turn memory on"}</Button>
            <Button size="sm" variant="ghost" onClick={() => setClosing(w.writer)}>Close month</Button>
          </div>
        ) : null; } },
  ];

  return (
    <section>
      <h2 style={{ fontSize: 15, margin: "0 0 4px" }}>Writers</h2>
      <div style={{ fontSize: 12.5, color: K.muted, marginBottom: 8 }}>
        Memory on: the writer reads its active release. Memory off: it writes as if there were no memory (it does not
        stop L0 rules). Turning memory on needs an active platform release.
      </div>
      <DataTable tableId="wm-writers" data={rows} columns={writerCols} getRowId={(x) => x.w.writer}
        paginate={false} tableMinWidth={760} compact emptyTitle="No writer row"
        emptyDescription="The four writer rows come with the release tables (CIS 287)." />
      <ConfirmModal open={!!flip} onClose={() => setFlip(null)} busy={busy} destructive={!!flip?.memory_enabled}
        title={flip?.memory_enabled ? `Turn memory off for ${flip.writer}?` : `Turn memory on for ${flip?.writer}?`}
        confirmLabel={flip?.memory_enabled ? "Turn memory off" : "Turn memory on"}
        onConfirm={() => flip && patch(flip.writer, { memory_enabled: !flip.memory_enabled },
          flip.memory_enabled ? `Memory off for ${flip.writer}: the next write ignores its release`
            : `Memory on for ${flip.writer}: the next write reads its active release`)}
        body={<div style={{ fontSize: 13, lineHeight: 1.5 }}>{flip?.memory_enabled
          ? "The next write of this writer ignores every release (platform and tenant) and writes as with no memory. "
            + "Active L0 rules still apply. No deploy is needed; turn it on again the same way."
          : "The next write of this writer reads its active platform release and the tenant's active release, if any."}
        </div>} />
      <Modal open={!!closing} onClose={() => setClosing(null)} title={`Close the release train of ${closing ?? ""}`}
        footer={<div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={() => setClosing(null)}>Cancel</Button>
          <Button variant="primary" disabled={!closeMonth || busy}
            onClick={() => closing && closeMonth && patch(closing, { train_closed_month: closeMonth },
              `The ${month} train of ${closing} is closed`)}>Close month</Button>
        </div>}>
        <div style={{ display: "grid", gap: 8, fontSize: 13, lineHeight: 1.5 }}>
          <div>No regular release of this month or an earlier one can be activated after this. Lexicon releases,
            emergencies and rollbacks are never blocked. A closed month cannot be reopened here.</div>
          <label>Month
            <input type="month" value={month} onChange={(e) => setMonth(e.target.value)} style={inputStyle} /></label>
        </div>
      </Modal>
    </section>
  );
}

// ── new draft ────────────────────────────────────────────────────────────────────────────────────────────────────

function NewDraftModal({ open, onClose, onCreated }: { open: boolean; onClose: () => void;
  onCreated: (id: string) => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [f, setF] = useState({ writer: "t9_social" as Writer, scope: "platform", tenant: "", rules: "", notes: "" });
  const [busy, setBusy] = useState(false);
  const ruleIds = f.rules.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  const bad = (f.scope === "tenant" && !isUuid(f.tenant)) || ruleIds.some((id) => !isUuid(id)) || ruleIds.length > 40;

  async function create() {
    setBusy(true);
    const r = await wmSend<Release>("/releases", "POST", {
      writer: f.writer, scope: f.scope, tenant_id: f.scope === "tenant" ? f.tenant.trim() : null, rule_ids: ruleIds,
      notes: f.notes.trim() || null,
    });
    setBusy(false);
    if (r.ok) {
      toast.success(`Draft #${r.data.release_no} created; attach its evidence next`);
      setF({ ...f, tenant: "", rules: "", notes: "" });
      qc.invalidateQueries({ queryKey: ["wm"] });
      onCreated(r.data.release_id);
    } else toast.error(r.message);
  }

  return (
    <Modal open={open} onClose={onClose} title="New release draft" width={520}
      footer={<div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={bad || busy} onClick={create}>Create draft</Button>
      </div>}>
      <div style={{ display: "grid", gap: 10, fontSize: 13 }}>
        <label>Writer
          <select value={f.writer} onChange={(e) => setF({ ...f, writer: e.target.value as Writer })} style={inputStyle}>
            {WRITERS.map((w) => <option key={w} value={w}>{w}</option>)}
          </select></label>
        <label>Owner
          <select value={f.scope} onChange={(e) => setF({ ...f, scope: e.target.value })} style={inputStyle}>
            <option value="platform">Platform (every tenant)</option>
            <option value="tenant">One tenant</option>
          </select></label>
        {f.scope === "tenant" && <TenantSelect value={f.tenant} required
          onChange={(tenant) => setF({ ...f, tenant })} />}
        <label>Rule ids (optional, one per line)
          <textarea value={f.rules} onChange={(e) => setF({ ...f, rules: e.target.value })} rows={3}
            style={{ ...inputStyle, fontFamily: mono, fontSize: 12 }} /></label>
        <label>Notes (optional)
          <input value={f.notes} maxLength={1000} onChange={(e) => setF({ ...f, notes: e.target.value })}
            style={inputStyle} /></label>
        <div style={{ color: K.muted, fontSize: 12 }}>
          No rule ids = an empty release (like release 0). A release with rules goes live only after its R2 eval and
          shadow runs pass and an admin approves it; no writer reads a lexicon-only release.</div>
      </div>
    </Modal>
  );
}

// ── one release ──────────────────────────────────────────────────────────────────────────────────────────────────

function RunCard({ label, run, id }: { label: string; run: ReleaseRun | null; id: string | null }) {
  if (!id) return (
    <div style={{ border: `1px dashed ${K.line}`, borderRadius: 8, padding: 8, color: K.muted, fontSize: 12.5 }}>
      {label}: no run attached</div>
  );
  if (!run) return (
    <div style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: 8, fontSize: 12.5, color: K.danger }}>
      {label}: run <span style={{ fontFamily: mono }}>{short(id)}</span> not found</div>
  );
  const s = run.summary || {};
  const facts: string[] = [];
  if (s.inputs !== undefined) facts.push(`${s.inputs} inputs`);
  if (s.byte_diffs !== undefined) facts.push(`${s.byte_diffs} byte diffs`);
  if (s.compared_with_pre !== undefined) facts.push(`${s.compared_with_pre} compared with the pre-deploy reference`);
  if (s.stored_hashes) facts.push(`${s.stored_hash_match ?? 0}/${s.stored_hashes} stored hashes reproduced`);
  if (s.errors) facts.push(`${s.errors} errors`);
  return (
    <div style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: 8, display: "grid", gap: 4, fontSize: 12.5 }}>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
        <b>{label}</b><Badge dot={false}>{run.run_kind}</Badge>
        <Badge tone={run.verdict === "pass" || run.verdict === "baseline" ? "success" : "danger"}>{run.verdict ?? "—"}</Badge>
        <span style={{ fontFamily: mono, color: K.muted }}>{short(run.id)}</span>
        <span style={{ color: K.muted }}>{run.finished_at ? formatDateTime(run.finished_at) : "not finished"}</span>
      </div>
      {facts.length > 0 && <div>{facts.join(" · ")}</div>}
      {s.results && <div style={{ color: K.muted }}>{Object.entries(s.results).map(([k, v]) => `${k} ${v}`).join(" · ")}</div>}
      {s.reasons && s.reasons.length > 0 && <div style={{ color: K.danger }}>{s.reasons.join("; ")}</div>}
      {s.prompt_sha256 && <div style={{ color: K.muted }}>prompt <span style={{ fontFamily: mono }}>
        {s.prompt_sha256.slice(0, 16)}</span>{s.git_sha ? ` · code ${s.git_sha.slice(0, 8)}` : ""}</div>}
    </div>
  );
}

function PostActivationCheck({ r }: { r: Release }) {
  const toast = useToast();
  const [report, setReport] = useState<PostActivationReport | null>(null);
  const [busy, setBusy] = useState(false);

  async function run() {
    setBusy(true);
    const res = await wmSend<PostActivationReport>(`/releases/${r.release_id}/post-activation-check`, "POST",
      { apply: false });
    setBusy(false);
    if (res.ok) setReport(res.data);
    else toast.error(res.message);
  }

  const codes = Object.entries(report?.violation_codes ?? {});
  return (
    <div style={{ display: "grid", gap: 6, fontSize: 12.5 }}>
      <h3 style={{ fontSize: 13, margin: 0 }}>Post-activation check</h3>
      <div style={{ color: K.muted, fontSize: 12 }}>Reads the writes made since this release went active: each must
        carry its rules hash and hold no QA trace and no other tenant&apos;s text. A dry run: no model call, nothing
        moves. The hourly check rolls a failing release back by itself.</div>
      <div><Button size="sm" variant="secondary" disabled={busy} onClick={run}>
        {busy ? "Checking…" : "Check now"}</Button></div>
      {report && (
        <div style={{ border: `1px solid ${K.line}`, borderRadius: 8, padding: 8, display: "grid", gap: 4 }}
          aria-live="polite">
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
            <Badge tone={checkTone(report.verdict)}>{report.verdict.replace("_", " ")}</Badge>
            <span style={{ color: K.muted }}>cost ${Number(report.cost_usd ?? 0).toFixed(2)}</span>
          </div>
          <div>{checkSentence(report)}</div>
          {report.verdict !== "not_applicable" && (
            <div style={{ color: K.muted }}>{report.checked} read · {report.carried ?? 0} carried ·{" "}
              {report.not_carried ?? 0} written without it · {report.not_rendered ?? 0} rules not rendered ·{" "}
              {report.fingerprint_mismatch ?? 0} hash mismatch</div>)}
          {codes.length > 0 && <div style={{ color: K.danger }}>{codes.map(([c, n]) =>
            `${CHECK_CODE_LABELS[c] ?? c}: ${n}`).join(" · ")}</div>}
          {(report.violating_refs ?? []).length > 0 && (
            <div style={{ display: "grid", gap: 2 }}>
              {(report.violating_refs ?? []).slice(0, 10).map((v) => (
                <div key={`${v.source}-${v.ref}`} style={{ overflowWrap: "anywhere" }}>
                  <span style={{ color: K.muted }}>{v.source}</span>{" "}
                  <span style={{ fontFamily: mono }}>{v.ref}</span>{" "}
                  {v.codes.map((c) => CHECK_CODE_LABELS[c] ?? c).join(", ")}</div>))}
            </div>)}
        </div>
      )}
    </div>
  );
}

function ReleaseDrawer({ releaseId, onClose, canAdmin, active, ownerOf }: { releaseId: string | null;
  onClose: () => void; canAdmin: boolean; active: ActiveReleases | undefined; ownerOf: (r: Release) => string }) {
  const toast = useToast();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["wm", "release", releaseId], enabled: !!releaseId,
    queryFn: () => apiGet<ReleaseDetail>(`${WM_API}/releases/${releaseId}`) });
  const [reason, setReason] = useState("");
  const [ref, setRef] = useState("");
  const [ev, setEv] = useState({ r0: "", eval: "", shadow: "" });
  const [confirm, setConfirm] = useState<"activate" | "rollback" | null>(null);
  const [busy, setBusy] = useState(false);
  const r = q.data;
  const allowed = r ? releaseActions(r) : null;

  function close() {
    setReason("");
    setRef("");
    setEv({ r0: "", eval: "", shadow: "" });
    onClose();
  }

  async function act(path: string, body: unknown, ok: (d: Release) => string) {
    if (!r) return;
    setBusy(true);
    const res = await wmSend<Release>(`/releases/${r.release_id}/${path}`, "POST", body);
    setBusy(false);
    setConfirm(null);
    if (res.ok) {
      toast.success(ok(res.data));
      setReason("");
      setRef("");
      setEv({ r0: "", eval: "", shadow: "" });
      qc.invalidateQueries({ queryKey: ["wm"] });
    } else toast.error(res.message);
  }

  // The evidence the active platform release of the same writer carries (release 0: its R0 replay + baseline), to
  // reuse for an empty release of the same writer.
  const platform = r ? (active?.items ?? []).find((a) => a.writer === r.writer && a.scope === "platform") : undefined;
  const need = r ? EVIDENCE_NEEDED[r.content_class as ContentClass] : [];
  const evBody = Object.fromEntries(Object.entries(ev).filter(([, v]) => v.trim())
    .map(([k, v]) => [`${k}_run_id`, v.trim()]));
  const evBad = Object.keys(evBody).length === 0 || Object.values(evBody).some((v) => !isUuid(v as string));

  return (
    <Drawer open={!!releaseId} onClose={close} width={640}
      title={r ? `Release #${r.release_no} · ${r.writer} · ${ownerOf(r)}` : "Release"}>
      {q.isLoading || !r ? (q.error ? <ErrorState message={(q.error as Error).message} onRetry={() => q.refetch()} />
        : <TableSkeleton rows={5} cols={2} />) : (
        <div style={{ display: "grid", gap: 14, fontSize: 13 }}>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            <Badge tone={releaseTone(r.status)}>{r.status}</Badge>
            <Badge dot={false}>{r.content_class}</Badge>
            <Badge dot={false}>{r.rule_ids.length} rules</Badge>
            <Badge dot={false}>train {r.train_kind}{r.train_month ? ` ${r.train_month.slice(0, 7)}` : ""}</Badge>
            {r.reverify && <Badge tone="warning">reverify: {r.reverify_reasons.join(", ") || "flagged"}</Badge>}
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 240px), 1fr))",
            gap: 6, color: K.muted, fontSize: 12.5 }}>
            <div>Drafted {formatDateTime(r.created_at)} by {actorLabel(r.created_by)}</div>
            {r.tenant_id && <div style={{ overflowWrap: "anywhere" }}>Owner <span style={{ color: K.ink }}>
              {ownerOf(r)}</span>
              <div style={{ fontFamily: mono, fontSize: 11.5 }}>{r.tenant_id}</div></div>}
            <div>Approved {r.approved_at ? `${formatDateTime(r.approved_at)} by ${actorLabel(r.approved_by)}` : "—"}</div>
            <div>Activated {r.activated_at ? formatDateTime(r.activated_at) : "—"}</div>
            <div>Retired {r.retired_at ? `${formatDateTime(r.retired_at)} (${r.retired_reason ?? ""})` : "—"}</div>
            {r.previous_release_id && <div>Previous release <span style={{ fontFamily: mono }}>
              {short(r.previous_release_id)}</span></div>}
            {r.approval_ref && <div style={{ overflowWrap: "anywhere" }}>Approval ref: {r.approval_ref}</div>}
            {r.rejected_reason && <div style={{ color: K.danger }}>Rejected by {actorLabel(r.rejected_by)}:{" "}
              {r.rejected_reason}</div>}
            {r.emergency_reason && <div>Emergency: {r.emergency_reason}</div>}
          </div>
          {r.notes && <div style={{ color: K.ink }}>{r.notes}</div>}

          <div style={{ display: "grid", gap: 6 }}>
            <h3 style={{ fontSize: 13, margin: 0 }}>Evidence</h3>
            <div style={{ color: K.muted, fontSize: 12 }}>A {r.content_class} release needs: {need.map((n) =>
              n === "r0" ? "an R0 replay" : n === "eval" ? (r.content_class === "empty"
                ? "an eval run (R2 pass or the golden-set baseline)" : "an R2 eval pass") : "a shadow pass").join(" and ")}.
            </div>
            {SLOTS.map(([slot, label]) => (
              <RunCard key={slot} label={label} run={r.runs?.[slot] ?? null}
                id={slot === "r0" ? r.r0_run_id : slot === "eval" ? r.eval_run_id : r.shadow_run_id} />))}
          </div>

          <div style={{ display: "grid", gap: 4, fontSize: 12.5, color: K.muted }}>
            <h3 style={{ fontSize: 13, margin: 0, color: K.ink }}>Fingerprint</h3>
            <div>Golden set {r.fingerprint?.golden_set ?? "—"} · gate taxonomy {String(r.fingerprint?.gate_taxonomy ?? "—")}</div>
            <div>Writer model {r.fingerprint?.writer_route?.model_id ?? "—"} · judge model{" "}
              {r.fingerprint?.judge_route?.model_id ?? "—"}</div>
            <div>Prompt <span style={{ fontFamily: mono }}>{r.fingerprint?.prompt_sha256
              ? r.fingerprint.prompt_sha256.slice(0, 16) : "—"}</span></div>
            {r.items_sha256 && <div>Rules hash <span style={{ fontFamily: mono }}>{r.items_sha256.slice(0, 16)}</span></div>}
          </div>

          {/* keyed by release: another release opened in the same drawer starts with no report */}
          {canAdmin && r.activated_at && <PostActivationCheck key={r.release_id} r={r} />}

          {canAdmin && allowed && (
            <div style={{ display: "grid", gap: 12, borderTop: `1px solid ${K.line}`, paddingTop: 12 }}>
              {allowed.evidence && (
                <div style={{ display: "grid", gap: 6 }}>
                  <h3 style={{ fontSize: 13, margin: 0 }}>Attach evidence</h3>
                  {need.map((slot) => (
                    <label key={slot} style={{ fontSize: 12.5 }}>{slot === "r0" ? "R0 run id" : slot === "eval"
                      ? "Eval run id" : "Shadow run id"}
                      <input value={ev[slot]} onChange={(e) => setEv({ ...ev, [slot]: e.target.value })}
                        style={{ ...inputStyle, fontFamily: mono, fontSize: 12 }} /></label>))}
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                    {platform && platform.release_id !== r.release_id && r.content_class === "empty" && (
                      <Button size="sm" variant="ghost"
                        onClick={() => setEv({ r0: platform.r0_run_id ?? "", eval: platform.eval_run_id ?? "",
                          shadow: platform.shadow_run_id ?? "" })}
                        title="Fill in the runs of this writer's active platform release (an empty release reuses them)">
                        Use the active platform release&apos;s runs</Button>)}
                    <Button size="sm" variant="primary" disabled={evBad || busy}
                      onClick={() => act("evidence", evBody, (d) => d.status === "evaluated"
                        ? "Evidence attached: the release is evaluated" : "Evidence attached; more runs are needed")}>
                      Attach evidence</Button>
                  </div>
                </div>
              )}
              {allowed.approve && (
                <div style={{ display: "grid", gap: 6 }}>
                  <h3 style={{ fontSize: 13, margin: 0 }}>Approve</h3>
                  <input placeholder="Approval reference (optional: a link to the decision)" value={ref}
                    maxLength={300} onChange={(e) => setRef(e.target.value)} style={inputStyle}
                    aria-label="Approval reference" />
                  <div><Button variant="primary" disabled={busy}
                    onClick={() => act("approve", ref.trim() ? { approval_ref: ref.trim() } : {},
                      () => "Approved by you; Activate puts it live")}>
                    <ShieldCheck size={14} /> Approve</Button></div>
                </div>
              )}
              {allowed.activate && (
                <div style={{ display: "grid", gap: 6 }}>
                  <div><Button variant="primary" disabled={!!r.activation || busy} onClick={() => setConfirm("activate")}
                    title={r.activation?.detail || "The writer's next write uses this release"}>Activate</Button></div>
                  {r.activation && <div style={{ color: K.muted, fontSize: 12 }}>Not activatable yet:{" "}
                    {r.activation.detail}</div>}
                </div>
              )}
              {(allowed.rollback || allowed.reject || r.status === "active") && (
                <div style={{ display: "grid", gap: 6 }}>
                  <input placeholder={allowed.rollback ? "Why roll it back?" : "Why reject it?"} value={reason}
                    onChange={(e) => setReason(e.target.value)} style={inputStyle} maxLength={400}
                    aria-label={allowed.rollback ? "Rollback reason" : "Reject reason"} />
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                    {allowed.rollback && <Button variant="danger" disabled={reason.trim().length < 3 || busy}
                      onClick={() => setConfirm("rollback")}>Roll back</Button>}
                    {allowed.reject && <Button variant="danger" disabled={reason.trim().length < 3 || busy}
                      onClick={() => act("reject", { reason: reason.trim() }, () => "Release rejected")}>Reject</Button>}
                  </div>
                  {r.status === "active" && !r.previous_release_id && <div style={{ color: K.muted, fontSize: 12 }}>
                    No previous release to roll back to: turn this writer&apos;s memory off instead (Writers).</div>}
                </div>
              )}
            </div>
          )}
          <ConfirmModal open={confirm === "activate"} onClose={() => setConfirm(null)} busy={busy}
            title={`Activate release #${r.release_no} of ${r.writer}?`} confirmLabel="Activate"
            onConfirm={() => act("activate", undefined, (d) => `Release #${d.release_no} is active`)}
            body={<div style={{ fontSize: 13, lineHeight: 1.5 }}>The current active release of this writer and owner
              is retired (superseded) and this one serves the next write. Roll back returns to it without a deploy.</div>} />
          <ConfirmModal open={confirm === "rollback"} onClose={() => setConfirm(null)} busy={busy} destructive
            title={`Roll back release #${r.release_no} of ${r.writer}?`} confirmLabel="Roll back"
            onConfirm={() => act("rollback", { reason: reason.trim() },
              (d) => `Rolled back: release #${d.release_no} is active again`)}
            body={<div style={{ fontSize: 13, lineHeight: 1.5 }}>This release is retired for good (a fix is a new
              release) and the previous one serves the next write. No deploy is needed.</div>} />
        </div>
      )}
    </Drawer>
  );
}

// ── the tab ──────────────────────────────────────────────────────────────────────────────────────────────────────

export default function ReleasesTab() {
  const toast = useToast();
  const qc = useQueryClient();
  const canAdmin = can(useAdminIdentity().role, "ai.admin");
  const tenants = useTenants();
  // Owner: "Platform", the tenant's name (else its slug), or "Tenant" + the short id when the list does not know it.
  const { byId, label } = tenants;
  const ownerOf = useCallback((r: Release) => owner(r.scope, r.tenant_id,
    r.tenant_id && byId.has(r.tenant_id) ? label(r.tenant_id) : undefined), [byId, label]);
  const [writer, setWriter] = useState("");
  const [status, setStatus] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [scanning, setScanning] = useState(false);
  const active = useQuery({ queryKey: ["wm", "releases-active"],
    queryFn: () => apiGet<ActiveReleases>(`${WM_API}/releases/active`) });
  const list = useQuery({ queryKey: ["wm", "releases", writer, status],
    queryFn: () => apiGet<ReleasesPage>(`${WM_API}/releases?limit=200${writer ? `&writer=${writer}` : ""}`
      + `${status ? `&status=${status}` : ""}`) });

  async function scan() {
    setScanning(true);
    const r = await wmSend<{ flagged: unknown[] }>("/releases/reverify-scan", "POST");
    setScanning(false);
    if (r.ok) {
      const n = Array.isArray(r.data.flagged) ? r.data.flagged.length : 0;
      toast[n ? "info" : "success"](n ? `${n} active releases flagged reverify` : "No active release needs a reverify");
      qc.invalidateQueries({ queryKey: ["wm"] });
    } else toast.error(r.message);
  }

  const cols = useMemo<ColumnDef<Release, unknown>[]>(() => [
    { accessorKey: "writer", header: "Writer",
      cell: (c) => <span style={{ fontFamily: mono }}>{c.row.original.writer}</span> },
    { id: "owner", accessorFn: (r) => ownerOf(r), header: "Owner",
      cell: (c) => { const r = c.row.original;
        return <span title={r.tenant_id ?? undefined} style={{ display: "inline-block", maxWidth: "100%" }}>
          <Badge tone={r.scope === "platform" ? "purple" : "info"}>
            {ownerOf(r)}</Badge></span>; } },
    { accessorKey: "release_no", header: "No.", cell: (c) => `#${c.row.original.release_no}` },
    { accessorKey: "status", header: "Status",
      cell: (c) => { const r = c.row.original;
        return <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
          <Badge tone={releaseTone(r.status)}>{r.status}</Badge>
          {r.reverify && <Badge tone="warning" dot={false}>reverify</Badge>}</div>; } },
    { id: "content", accessorFn: (r) => r.rule_ids.length, header: "Content",
      cell: (c) => <span style={{ fontSize: 12 }}>{c.row.original.content_class} · {c.row.original.rule_ids.length} rules</span> },
    { id: "evidence", header: "Evidence", enableSorting: false, cell: (c) => <EvidenceBadges r={c.row.original} /> },
    { id: "who", accessorFn: (r) => r.approved_by ?? "", header: "Drafted / approved by",
      cell: (c) => <span style={{ fontSize: 12 }}>{actorLabel(c.row.original.created_by)} /{" "}
        {actorLabel(c.row.original.approved_by)}</span> },
    { accessorKey: "created_at", header: "Created",
      cell: (c) => <span style={{ fontSize: 12, whiteSpace: "nowrap" }}>{formatDateTime(c.row.original.created_at)}</span> },
    { accessorKey: "activated_at", header: "Activated",
      cell: (c) => <span style={{ fontSize: 12, whiteSpace: "nowrap" }}>
        {c.row.original.activated_at ? formatDateTime(c.row.original.activated_at) : "—"}</span> },
    { id: "prompt", header: "Prompt", enableSorting: false,
      cell: (c) => <span style={{ fontFamily: mono, fontSize: 12, color: K.muted }}>
        {c.row.original.fingerprint?.prompt_sha256 ? c.row.original.fingerprint.prompt_sha256.slice(0, 12) : "—"}</span> },
  ], [ownerOf]);

  const total = list.data?.total ?? 0;
  const shown = list.data?.items.length ?? 0;

  // minmax(0, 1fr): a grid's implicit column is `auto`, so it grows to the widest item's min-content width. The
  // release table's min-content (tableMinWidth plus its nowrap cells) is wider than the page column at 1440, which
  // widened the column for both sections and pushed the Writers actions and the toolbar's Columns button off the
  // right edge. Capping the track at the column width keeps each table inside its card, scrolling there.
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 20 }}>
      {active.error ? <ErrorState message={(active.error as Error).message} onRetry={() => active.refetch()} />
        : active.isLoading ? <TableSkeleton rows={4} cols={5} /> : <WritersPanel active={active.data} canAdmin={canAdmin} />}
      <section>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 4 }}>
          <h2 style={{ fontSize: 15, margin: 0, flex: "1 1 auto" }}>Releases</h2>
          {canAdmin && <>
            <Button variant="ghost" onClick={scan} disabled={scanning}
              title="Flags the active releases whose golden set, model route or gate taxonomy changed since their evidence (they keep serving)">
              Reverify scan</Button>
            <Button variant="primary" onClick={() => setCreating(true)}><Plus size={14} /> New draft</Button>
          </>}
        </div>
        <div style={{ fontSize: 12.5, color: K.muted, marginBottom: 8 }}>
          draft → evaluated (evidence attached) → approved (one signed-in admin) → active. Activating retires the
          release before it; Roll back returns to it. Click a row for its evidence and actions.
          {shown < total ? ` Showing ${shown} of ${total}: narrow the filters.` : ""}
        </div>
        {list.error ? <ErrorState message={(list.error as Error).message} onRetry={() => list.refetch()} /> : (
          <DataTable tableId="wm-releases" data={list.data?.items ?? []} columns={cols} getRowId={(r) => r.release_id}
            loading={list.isLoading} onRowClick={(r) => setOpen(r.release_id)} tableMinWidth={1100}
            emptyTitle="No release here" emptyDescription="Release 0 of each writer is drafted by the release 0 tool."
            toolbarExtra={<div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <select value={writer} onChange={(e) => setWriter(e.target.value)} aria-label="Writer" style={selectStyle}>
                <option value="">Every writer</option>
                {WRITERS.map((w) => <option key={w} value={w}>{w}</option>)}
              </select>
              <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Release status"
                style={selectStyle}>
                <option value="">Every status</option>
                {RELEASE_STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>} />
        )}
      </section>
      <ReleaseDrawer releaseId={open} onClose={() => setOpen(null)} canAdmin={canAdmin} active={active.data}
        ownerOf={ownerOf} />
      <NewDraftModal open={creating} onClose={() => setCreating(false)}
        onCreated={(id) => { setCreating(false); setOpen(id); }} />
    </div>
  );
}

