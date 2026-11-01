"use client";
// app/admin/data-erasure/ApplyPanel.tsx — AA-839: approve and apply one succeeded, unblocked DRY RUN.
//
// The page never builds an apply from free input: the body comes from the DRY RUN job (its id, fingerprint,
// subject, tenant id or traveller hash) plus what the operator types here (fingerprint prefix as a check, snapshot
// id, approver). The API refuses unless ERASURE_APPLY_ENABLED=true and the session is a verified Cognito staff
// session (403 with its reason, shown as-is); the job re-checks everything inside its transaction.

import { ShieldAlert } from "lucide-react";
import { useState } from "react";
import { Button, K, mono, useT, Checkbox, Input } from "../../_kit";
import {
  FINGERPRINT_CONFIRM_CHARS, applyBody, applyFormProblems, applyGate, jobSubject, suggestedSnapshotId,
  totalsSentence, type ApplyForm, type ErasureJob, type ErasureSettings,
} from "@/lib/dataErasure";
import { Notice, Section, hintStyle, inputStyle, labelStyle, useErasureWords } from "./erasureUi";
import { ER } from "./messages";

export type SendResult = { ok: true; data: Record<string, unknown> } | { ok: false; status: number; message: string };

// Mount with key={job.id}: the form must start empty for every DRY RUN, so an approval typed for one subject can
// never carry over to another one (the fingerprint binds the subject since #961; this keeps the operator's intent too).
export default function ApplyPanel({ job, settings, now, history, subjectName, send, onStarted }: {
  job: ErasureJob;
  settings: ErasureSettings | undefined;
  now: number;
  /** Recent data_erasure jobs: an apply already queued on this DRY RUN locks the panel. */
  history: readonly ErasureJob[];
  /** "tenant Acme (acme) · <uuid>" or "traveller hash abcd1234…" (lib/dataErasure subjectLabel). */
  subjectName: string;
  send: (body: Record<string, unknown>) => Promise<SendResult>;
  onStarted: (applyJobId: string) => void;
}) {
  const t = useT(ER);
  const w = useErasureWords();
  const gate = applyGate(job, settings, now, history, w);
  const subject = jobSubject(job) ?? "tenant";
  const fp = job.result?.fingerprint ?? "";
  const [form, setForm] = useState<ApplyForm>({ fingerprintPrefix: "", snapshotId: "", approvedBy: "", confirmed: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const problems = gate.open ? applyFormProblems(fp, form, w) : [];
  const set = (patch: Partial<ApplyForm>) => setForm((f) => ({ ...f, ...patch }));
  const sentence = totalsSentence(job.result?.totals, w);

  const submit = async () => {
    if (!gate.open || problems.length || busy) return;
    setBusy(true);
    setError(null);
    const out = await send(applyBody(job, form));
    setBusy(false);
    if (!out.ok) {
      setError(out.status === 403 ? t("apiRefused", out.message) : out.message);
      return;
    }
    onStarted(String(out.data.job_id));
  };

  return (
    <Section title={<span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}><ShieldAlert size={15} /> {t("applyErasure")}</span>} testId="erasure-apply">
      {!gate.open ? (
        <Notice tone="warning" testId="erasure-apply-locked">
          <strong>{t("applyUnavailable")}</strong>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {gate.reasons.map((r) => <li key={r}>{r}</li>)}
          </ul>
          {!settings?.apply_enabled && (
            <div style={{ marginTop: 6 }}>
              {t("turnOnHow")}
            </div>
          )}
        </Notice>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <Notice tone="danger">
            {t("aboutToErase")}{" "}
            <strong data-testid="erasure-apply-subject" style={{ overflowWrap: "anywhere" }}>{subjectName}</strong>.{" "}
            {t("cannotUndo")}{subject === "tenant" ? t("tenantNoRestore") : ""}{t("beforeStart")}{" "}
            <span style={{ fontFamily: mono }}>aws rds create-db-snapshot …</span>{t("applyRunsOnce")}
          </Notice>
          {gate.warnings.map((w) => <Notice key={w} tone="warning">{w}</Notice>)}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
            <label style={labelStyle}>
              {t("firstChars", FINGERPRINT_CONFIRM_CHARS)}
              <Input
                data-testid="erasure-apply-fp"
                value={form.fingerprintPrefix}
                onChange={(e) => set({ fingerprintPrefix: e.target.value })}
                maxLength={FINGERPRINT_CONFIRM_CHARS}
                autoComplete="off"
                spellCheck={false}
                style={{ ...inputStyle, fontFamily: mono }}
              />
              <span style={hintStyle}>{t("readFromApproval")}</span>
            </label>
            <label style={labelStyle}>
              {t("snapshotId")}
              <Input
                data-testid="erasure-apply-snapshot"
                value={form.snapshotId}
                onChange={(e) => set({ snapshotId: e.target.value })}
                placeholder={suggestedSnapshotId(subject, now)}
                autoComplete="off"
                spellCheck={false}
                style={{ ...inputStyle, fontFamily: mono }}
              />
              <span style={hintStyle}>{t("snapshotHint")}</span>
            </label>
            <label style={labelStyle}>
              {t("approvedBy")}
              <Input
                data-testid="erasure-apply-approver"
                value={form.approvedBy}
                onChange={(e) => set({ approvedBy: e.target.value })}
                placeholder={t("approverPlaceholder")}
                maxLength={120}
                autoComplete="off"
                style={inputStyle}
              />
              <span style={hintStyle}>{t("approverHint")}</span>
            </label>
          </div>
          <label style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 13, color: K.ink, lineHeight: 1.5 }}>
            <Checkbox
              data-testid="erasure-apply-confirm"
             
              checked={form.confirmed}
              onChange={(e) => set({ confirmed: e.target.checked })}
              style={{ marginTop: 3 }}
            />
            <span>
              {t("confirmLead")} <strong style={{ overflowWrap: "anywhere" }}>{subjectName}</strong> {t("confirmFrom")}{" "}
              <strong>{sentence}</strong> {t("confirmTail")}
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
              {busy ? t("starting") : t("applyErasure")}
            </Button>
          </div>
        </div>
      )}
    </Section>
  );
}
