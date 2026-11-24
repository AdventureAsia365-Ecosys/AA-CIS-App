"use client";
// app/admin/platform/compliance/GovernanceTabs.tsx — AA-1173: the Exceptions and Reviews tabs of Platform › Compliance.
//
// Exceptions: the intro (the longest expiry comes from the setting cis.compliance.exception_max_days; whether the
// proposer may self-approve follows cis.settings.two_person_approval), the counts, the table (open first, then approved
// by nearest expiry) with Approve / Reject / Revoke (or Withdraw on one's own open proposal) in ConfirmModal dialogs,
// and the Propose form in a Modal. Reviews: the intro (cis.compliance.review_max_age_days), whether this month is
// recorded, the Record form (notes + actions; the API stores the page's numbers with them) and the history table.
//
// API (api/routers/admin_platform_compliance.py, compliance.read / compliance.write; writes need a company sign-in,
// the backend answers 403 otherwise): GET/POST /exceptions, POST /exceptions/{id}/approve|reject|revoke, GET/POST
// /reviews. Pure helpers in lib/compliance.ts. Kit only; write controls hidden in a read-only session (WriteOnly).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
  Badge, Button, ConfirmModal, DataTable, DateField, ErrorState, Field, Input, K, Modal, Section, Select, Textarea,
  WriteOnly, apiGet, apiSend, mono, useFormat, useT, useToast, type ColumnDef,
} from "../../../_kit";
import {
  EXCEPTIONS_PATH, REVIEWS_PATH, exceptionActions, exceptionTone, fmtPct, maxExpiry, riskTone, sortExceptions,
  type ExceptionRow, type ExceptionsResp, type ReviewRow, type ReviewsResp, type RiskLevel, type SubjectKind,
} from "@/lib/compliance";
import { GOVERNANCE } from "./governance.messages";

const KEY = ["platform-compliance"];
const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;
const grid = { display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 } as const;

function useGT() {
  return useT(GOVERNANCE);
}
type GT = ReturnType<typeof useGT>;

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function who(actor: string | null, viewer: string | null, t: GT): string {
  if (!actor) return "—";
  return actor === viewer ? t("you") : actor.replace(/^admin:/, "").slice(0, 8);
}

// ── exceptions ──────────────────────────────────────────────────────────────────────────────────────────────────────

type Decision = { row: ExceptionRow; action: "approve" | "reject" | "revoke" } | null;

function ProposeModal({ open, onClose, maxDays, t }: { open: boolean; onClose: () => void; maxDays: number; t: GT }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { formatDate } = useFormat();
  const latest = maxExpiry(todayIso(), maxDays);
  const [kind, setKind] = useState<SubjectKind>("control");
  const [ref, setRef] = useState("");
  const [title, setTitle] = useState("");
  const [reason, setReason] = useState("");
  const [compensating, setCompensating] = useState("");
  const [risk, setRisk] = useState<RiskLevel>("medium");
  const [expires, setExpires] = useState("");
  const send = useMutation({
    mutationFn: () => apiSend<ExceptionRow>(EXCEPTIONS_PATH, { body: {
      subject_kind: kind, subject_ref: ref.trim(), title: title.trim(), reason: reason.trim(),
      compensating: compensating.trim() || null, risk_level: risk, expires_on: expires } }),
    onSuccess: () => {
      toast.success(t("proposed"));
      setRef(""); setTitle(""); setReason(""); setCompensating(""); setExpires("");
      onClose();
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: KEY }),
  });
  const ready = ref.trim() && title.trim().length >= 3 && reason.trim().length >= 20 && expires && !send.isPending;
  return (
    <Modal open={open} onClose={send.isPending ? () => undefined : onClose} title={t("proposeTitle")} width={560}
      pinFooter testId="compliance-exception-form"
      footer={<span style={{ display: "inline-flex", gap: 8 }}>
        <Button variant="ghost" onClick={onClose} disabled={send.isPending}>{t("cancel")}</Button>
        <Button variant="primary" onClick={() => send.mutate()} disabled={!ready}
          testId="compliance-exception-submit">{t("submit")}</Button>
      </span>}>
      <div style={grid}>
        {send.error && <ErrorState message={(send.error as Error).message} />}
        <Field label={t("subjectKind")} required>
          <Select value={kind} onChange={(e) => setKind(e.target.value as SubjectKind)}
            options={(["control", "check", "vulnerability"] as const).map((k) => ({ value: k, label: t(`kind_${k}`) }))} />
        </Field>
        <Field label={t("subjectRef")} required hint={t("subjectRefHint")}>
          <Input value={ref} onChange={(e) => setRef(e.target.value)} maxLength={80} style={{ fontFamily: mono }} />
        </Field>
        <Field label={t("titleLabel")} required>
          <Input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={160} />
        </Field>
        <Field label={t("reason")} required hint={t("reasonHint")}>
          <Textarea value={reason} onChange={(e) => setReason(e.target.value)} maxLength={2000} rows={4} />
        </Field>
        <Field label={t("compensating")}>
          <Textarea value={compensating} onChange={(e) => setCompensating(e.target.value)} maxLength={2000} rows={2} />
        </Field>
        <Field label={t("risk")} required>
          <Select value={risk} onChange={(e) => setRisk(e.target.value as RiskLevel)}
            options={(["low", "medium", "high"] as const).map((r) => ({ value: r, label: t(`risk_${r}`) }))} />
        </Field>
        <Field label={t("expiresOn")} required hint={t("expiresHint", formatDate(latest))}>
          <DateField value={expires} onChange={setExpires} min={todayIso()} max={latest} aria-label={t("expiresOn")} />
        </Field>
      </div>
    </Modal>
  );
}

type ExceptionsView = { rows: ExceptionRow[]; viewer: string | null; twoPerson: boolean };

function exceptionsView(data: ExceptionsResp | undefined): ExceptionsView {
  if (!data) return { rows: [], viewer: null, twoPerson: true };
  return { rows: sortExceptions(data.exceptions), viewer: data.viewer, twoPerson: data.two_person_approval };
}

type Fmt = ReturnType<typeof useFormat>;

function ExceptionActions({ r, view, onDecide, t }: {
  r: ExceptionRow; view: ExceptionsView; onDecide: (d: Decision) => void; t: GT;
}) {
  const can = exceptionActions(r, view.viewer, view.twoPerson);
  const own = r.status === "proposed" && view.viewer === r.proposed_by;
  return (
    <WriteOnly>
      <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
        {can.approve && <Button size="sm" variant="primary" testId={`exception-approve-${r.exception_id}`}
          onClick={() => onDecide({ row: r, action: "approve" })}>{t("approve")}</Button>}
        {can.reject && <Button size="sm" variant="secondary"
          onClick={() => onDecide({ row: r, action: "reject" })}>{t("reject")}</Button>}
        {can.revoke && <Button size="sm" variant="ghost" testId={`exception-revoke-${r.exception_id}`}
          onClick={() => onDecide({ row: r, action: "revoke" })}>{own ? t("withdraw") : t("revoke")}</Button>}
      </span>
    </WriteOnly>
  );
}

function exceptionColumns(t: GT, fmt: Fmt, view: ExceptionsView,
  onDecide: (d: Decision) => void): ColumnDef<ExceptionRow, unknown>[] {
  const { formatDate, formatDateTime } = fmt;
  return [
    { id: "id", header: t("colId"), accessorKey: "exception_id",
      cell: ({ row: { original: r } }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{r.exception_id}</span> },
    { id: "subject", header: t("colSubject"),
      cell: ({ row: { original: r } }) => <span style={{ display: "grid", gap: 2 }}>
        <span style={{ fontFamily: mono, fontSize: 12 }}>{r.subject_ref}</span>
        <span style={small}>{t(`kind_${r.subject_kind}`)}</span></span> },
    { id: "title", header: t("colTitle"), accessorKey: "title",
      cell: ({ row: { original: r } }) => <span title={r.reason}>{r.title}</span> },
    { id: "risk", header: t("colRisk"), accessorKey: "risk_level",
      cell: ({ row: { original: r } }) => <Badge tone={riskTone(r.risk_level)}>{t(`risk_${r.risk_level}`)}</Badge> },
    { id: "state", header: t("colState"), accessorKey: "state",
      cell: ({ row: { original: r } }) => <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
        <Badge tone={exceptionTone(r.state)}>{t(`state_${r.state}`)}</Badge>
        {r.self_approved && <Badge tone="warning" dot={false}>{t("selfApproved")}</Badge>}</span> },
    { id: "expires", header: t("colExpires"), accessorKey: "expires_on",
      cell: ({ row: { original: r } }) => <span style={small}>{formatDate(r.expires_on)}
        {r.state === "approved" ? ` · ${t("daysLeft", r.days_left)}` : ""}</span> },
    { id: "proposed", header: t("colProposed"),
      cell: ({ row: { original: r } }) => <span style={small}>{who(r.proposed_by, view.viewer, t)} ·{" "}
        {formatDateTime(r.proposed_at)}</span> },
    { id: "decided", header: t("colDecided"),
      cell: ({ row: { original: r } }) => <span style={small}>{who(r.revoked_by ?? r.decided_by, view.viewer, t)}
        {r.revoke_note || r.decision_note ? ` · ${r.revoke_note ?? r.decision_note}` : ""}</span> },
    { id: "actions", header: t("colActions"),
      cell: ({ row: { original: r } }) => <ExceptionActions r={r} view={view} onDecide={onDecide} t={t} /> },
  ];
}

const DECISION_TITLE = { approve: "approveTitle", reject: "rejectTitle", revoke: "revokeTitle" } as const;

// Mounted only while a decision is open (the parent renders it when `decision` is set).
function DecisionModal({ decision, onClose, t }: { decision: NonNullable<Decision>; onClose: () => void; t: GT }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { formatDate } = useFormat();
  const { row, action } = decision;
  const approve = action === "approve";
  const decide = useMutation({
    mutationFn: (note: string) =>
      apiSend<ExceptionRow>(`${EXCEPTIONS_PATH}/${row.exception_id}/${action}`, { body: { note: note || null } }),
    onSuccess: () => { toast.success(t("done")); onClose(); },
    onSettled: () => void qc.invalidateQueries({ queryKey: KEY }),
  });
  return (
    <ConfirmModal open onClose={() => { if (!decide.isPending) onClose(); }} title={t(DECISION_TITLE[action])}
      body={approve ? t("approveBody", row.subject_ref, formatDate(row.expires_on)) : `${row.subject_ref} — ${row.title}`}
      confirmLabel={t(action)} destructive={!approve}
      reason={{ label: approve ? t("noteLabel") : t("reasonRequired"), required: !approve, maxLength: 1000 }}
      busy={decide.isPending} error={decide.error ? (decide.error as Error).message : null}
      onConfirm={(note) => decide.mutate(note)} testId="compliance-exception-decision" />
  );
}

function ExceptionsSummary({ d, t }: { d: ExceptionsResp; t: GT }) {
  const c = d.counts;
  return (
    <p style={{ ...small, margin: "0 0 8px" }}>
      {d.two_person_approval ? t("selfApprovalOff") : t("selfApprovalOn")}{" "}
      {t("counts", c.proposed ?? 0, c.approved ?? 0, c.expired ?? 0)}</p>
  );
}

export function ExceptionsTab() {
  const t = useGT();
  const fmt = useFormat();
  const [proposing, setProposing] = useState(false);
  const [decision, setDecision] = useState<Decision>(null);
  const q = useQuery({ queryKey: [...KEY, "exceptions"], queryFn: () => apiGet<ExceptionsResp>(EXCEPTIONS_PATH),
    staleTime: 30_000, retry: false });
  const d = q.data;
  const view = useMemo(() => exceptionsView(d), [d]);
  const columns = useMemo(() => exceptionColumns(t, fmt, view, setDecision), [t, fmt, view]);
  return (
    <div style={grid} data-testid="compliance-exceptions">
      {q.error && <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} onRetry={() => void q.refetch()} />}
      <Section title={t("tabExceptions")} description={d ? t("exceptionsIntro", d.max_days) : undefined}
        actions={<WriteOnly><Button size="sm" variant="primary" onClick={() => setProposing(true)} disabled={!d}
          testId="compliance-exception-propose">{t("propose")}</Button></WriteOnly>}>
        {d && <ExceptionsSummary d={d} t={t} />}
        <DataTable<ExceptionRow> tableId="compliance-exceptions" data={view.rows} columns={columns}
          getRowId={(r) => String(r.exception_id)} loading={q.isLoading} paginate={false} searchable
          tableMinWidth={1100} enableCsv csvFilename="compliance-exceptions.csv" emptyTitle={t("noExceptions")} />
      </Section>
      {d && <ProposeModal open={proposing} onClose={() => setProposing(false)} maxDays={d.max_days} t={t} />}
      {decision && <DecisionModal key={`${decision.row.exception_id}-${decision.action}`} decision={decision}
        onClose={() => setDecision(null)} t={t} />}
    </div>
  );
}

// ── reviews ─────────────────────────────────────────────────────────────────────────────────────────────────────────

function scoresLine(r: ReviewRow): string {
  return (r.summary.frameworks ?? []).map((f) => `${f.framework} ${fmtPct(f.weighted_pct)}`).join(" · ") || "—";
}

function reviewColumns(t: GT, fmt: Fmt): ColumnDef<ReviewRow, unknown>[] {
  return [
    { id: "period", header: t("colPeriod"), accessorKey: "period",
      cell: ({ row: { original: r } }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{r.period.slice(0, 7)}</span> },
    { id: "reviewer", header: t("colReviewer"), accessorKey: "reviewed_by",
      cell: ({ row: { original: r } }) => <span style={small}>{r.reviewed_by.replace(/^admin:/, "").slice(0, 8)}</span> },
    { id: "reviewed", header: t("colReviewed"), accessorKey: "reviewed_at",
      cell: ({ row: { original: r } }) => <span style={small}>{fmt.formatDateTime(r.reviewed_at)}</span> },
    { id: "scores", header: t("colScores"), cell: ({ row: { original: r } }) => <span style={small}>{scoresLine(r)}</span> },
    { id: "notes", header: t("colNotes"), accessorKey: "notes",
      cell: ({ row: { original: r } }) => <span style={{ ...small, whiteSpace: "pre-wrap" }}>{r.notes}
        {r.actions ? `\n→ ${r.actions}` : ""}</span> },
  ];
}

function RecordReviewModal({ period, onClose, t }: { period: string; onClose: () => void; t: GT }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { formatDate } = useFormat();
  const [notes, setNotes] = useState("");
  const [actions, setActions] = useState("");
  const record = useMutation({
    mutationFn: () => apiSend<ReviewRow>(REVIEWS_PATH, { body: { notes: notes.trim(),
      actions: actions.trim() || null } }),
    onSuccess: () => { toast.success(t("recorded")); onClose(); },
    onSettled: () => void qc.invalidateQueries({ queryKey: KEY }),
  });
  return (
    <Modal open onClose={record.isPending ? () => undefined : onClose} title={t("recordTitle")}
      width={560} pinFooter testId="compliance-review-form"
      footer={<span style={{ display: "inline-flex", gap: 8 }}>
        <Button variant="ghost" onClick={onClose} disabled={record.isPending}>{t("cancel")}</Button>
        <Button variant="primary" onClick={() => record.mutate()} testId="compliance-review-submit"
          disabled={notes.trim().length < 20 || record.isPending}>{t("recordReview")}</Button>
      </span>}>
      <div style={grid}>
        {record.error && <ErrorState message={(record.error as Error).message} />}
        <p style={{ ...small, margin: 0 }}>{t("snapshotNote")} {formatDate(period)}</p>
        <Field label={t("notes")} required hint={t("notesHint")}>
          <Textarea value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={4000} rows={5} />
        </Field>
        <Field label={t("actions")} hint={t("actionsHint")}>
          <Textarea value={actions} onChange={(e) => setActions(e.target.value)} maxLength={4000} rows={3} />
        </Field>
      </div>
    </Modal>
  );
}

function ReviewStatus({ d, t }: { d: ReviewsResp; t: GT }) {
  const month = d.current_period.slice(0, 7);
  return (
    <p style={{ ...small, margin: "0 0 8px" }}>
      <Badge tone={d.current_recorded ? "success" : "warning"}>
        {d.current_recorded ? t("currentRecorded", month) : t("currentMissing", month)}</Badge></p>
  );
}

export function ReviewsTab() {
  const t = useGT();
  const fmt = useFormat();
  const [open, setOpen] = useState(false);
  const q = useQuery({ queryKey: [...KEY, "reviews"], queryFn: () => apiGet<ReviewsResp>(REVIEWS_PATH),
    staleTime: 30_000, retry: false });
  const d = q.data;
  const columns = useMemo(() => reviewColumns(t, fmt), [t, fmt]);
  const reviews = useMemo(() => d?.reviews ?? [], [d]);
  return (
    <div style={grid} data-testid="compliance-reviews">
      {q.error && <ErrorState message={`${t("reviewsLoadError")}: ${(q.error as Error).message}`}
        onRetry={() => void q.refetch()} />}
      <Section title={t("recordTitle")} description={d ? t("reviewsIntro", d.max_age_days) : undefined}
        actions={<WriteOnly><Button size="sm" variant="primary" onClick={() => setOpen(true)}
          disabled={!d || d.current_recorded} testId="compliance-review-record">{t("recordReview")}</Button></WriteOnly>}>
        {d && <ReviewStatus d={d} t={t} />}
        <DataTable<ReviewRow> tableId="compliance-reviews" data={reviews} columns={columns}
          getRowId={(r) => String(r.review_id)} loading={q.isLoading} paginate={false} searchable={false}
          tableMinWidth={900} enableCsv csvFilename="compliance-reviews.csv" emptyTitle={t("noReviews")} />
      </Section>
      {open && d && <RecordReviewModal period={d.current_period} onClose={() => setOpen(false)} t={t} />}
    </div>
  );
}
