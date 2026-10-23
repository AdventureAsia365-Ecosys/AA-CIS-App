"use client";
// app/admin/data-erasure/ApplyPanel.tsx — AA-839: approve and apply one succeeded, unblocked DRY RUN.
//
// The page never builds an apply from free input: the body comes from the DRY RUN job (its id, fingerprint,
// subject, tenant id or traveller hash) plus what the operator types here (fingerprint prefix as a check, snapshot
// id, approver). The API refuses unless ERASURE_APPLY_ENABLED=true and the session is a verified Cognito staff
// session (403 with its reason, shown as-is); the job re-checks everything inside its transaction.

import { ShieldAlert } from "lucide-react";
import { useState } from "react";
import { Button, K, mono } from "../../_kit";
import {
  FINGERPRINT_CONFIRM_CHARS, applyBody, applyFormProblems, applyGate, jobSubject, suggestedSnapshotId,
  totalsSentence, type ApplyForm, type ErasureJob, type ErasureSettings,
} from "@/lib/dataErasure";
import { Notice, Section, hintStyle, inputStyle, labelStyle } from "./erasureUi";

export type SendResult = { ok: true; data: Record<string, unknown> } | { ok: false; status: number; message: string };

export default function ApplyPanel({ job, settings, now, send, onStarted }: {
  job: ErasureJob;
  settings: ErasureSettings | undefined;
  now: number;
  send: (body: Record<string, unknown>) => Promise<SendResult>;
  onStarted: (applyJobId: string) => void;
}) {
  const gate = applyGate(job, settings, now);
  const subject = jobSubject(job) ?? "tenant";
  const fp = job.result?.fingerprint ?? "";
  const [form, setForm] = useState<ApplyForm>({ fingerprintPrefix: "", snapshotId: "", approvedBy: "", confirmed: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const problems = gate.open ? applyFormProblems(fp, form) : [];
  const set = (patch: Partial<ApplyForm>) => setForm((f) => ({ ...f, ...patch }));
  const sentence = totalsSentence(job.result?.totals);

  const submit = async () => {
    if (!gate.open || problems.length || busy) return;
    setBusy(true);
    setError(null);
    const out = await send(applyBody(job, form));
    setBusy(false);
    if (!out.ok) {
      setError(out.status === 403 ? `The API refused the apply (403): ${out.message}` : out.message);
      return;
    }
    onStarted(String(out.data.job_id));
  };

  return (
    <Section title={<span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}><ShieldAlert size={15} /> Apply erasure</span>} testId="erasure-apply">
      {!gate.open ? (
        <Notice tone="warning" testId="erasure-apply-locked">
          <strong>Apply is not available for this DRY RUN.</strong>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {gate.reasons.map((r) => <li key={r}>{r}</li>)}
          </ul>
          {!settings?.apply_enabled && (
            <div style={{ marginTop: 6 }}>
              Turning it on is an approved Terraform change (env on the API and worker) followed by a deploy; it goes back off
              after the erasure (runbook section 4 steps 6 and 10).
            </div>
          )}
        </Notice>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <Notice tone="danger">
            This cannot be undone{subject === "tenant" ? " and the tenant can no longer be restored" : ""}. Before you start:
            Nghiệp approved this fingerprint, and a manual RDS snapshot exists (runbook: <span style={{ fontFamily: mono }}>
            aws rds create-db-snapshot …</span>). The apply runs once: if it fails, nothing is committed and you start again
            with a new DRY RUN. It needs a company sign-in (Cognito): any other session gets a 403 from the API, shown here.
          </Notice>
          {gate.warnings.map((w) => <Notice key={w} tone="warning">{w}</Notice>)}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
            <label style={labelStyle}>
              First {FINGERPRINT_CONFIRM_CHARS} characters of the fingerprint
              <input
                data-testid="erasure-apply-fp"
                value={form.fingerprintPrefix}
                onChange={(e) => set({ fingerprintPrefix: e.target.value })}
                maxLength={FINGERPRINT_CONFIRM_CHARS}
                autoComplete="off"
                spellCheck={false}
                style={{ ...inputStyle, fontFamily: mono }}
              />
              <span style={hintStyle}>Read it from the approval, not from this page.</span>
            </label>
            <label style={labelStyle}>
              RDS snapshot id
              <input
                data-testid="erasure-apply-snapshot"
                value={form.snapshotId}
                onChange={(e) => set({ snapshotId: e.target.value })}
                placeholder={suggestedSnapshotId(subject, now)}
                autoComplete="off"
                spellCheck={false}
                style={{ ...inputStyle, fontFamily: mono }}
              />
              <span style={hintStyle}>The manual snapshot taken for this erasure (status available).</span>
            </label>
            <label style={labelStyle}>
              Approved by
              <input
                data-testid="erasure-apply-approver"
                value={form.approvedBy}
                onChange={(e) => set({ approvedBy: e.target.value })}
                placeholder="Nghiep"
                maxLength={120}
                autoComplete="off"
                style={inputStyle}
              />
              <span style={hintStyle}>Who approved the fingerprint.</span>
            </label>
          </div>
          <label style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 13, color: K.ink, lineHeight: 1.5 }}>
            <input
              data-testid="erasure-apply-confirm"
              type="checkbox"
              checked={form.confirmed}
              onChange={(e) => set({ confirmed: e.target.checked })}
              style={{ marginTop: 3 }}
            />
            <span>
              I confirm erasing {subject === "tenant" ? "this tenant" : "this traveller"} from CIS: <strong>{sentence}</strong>{" "}
              row(s), exactly as approved.
            </span>
          </label>
          {problems.length > 0 && (
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: K.muted }}>
              {problems.map((p) => <li key={p}>{p}</li>)}
            </ul>
          )}
          {error && <Notice tone="danger" testId="erasure-apply-error">{error}</Notice>}
          <div>
            <Button variant="danger" onClick={submit} disabled={problems.length > 0 || busy}>
              {busy ? "Starting…" : "Apply erasure"}
            </Button>
          </div>
        </div>
      )}
    </Section>
  );
}
