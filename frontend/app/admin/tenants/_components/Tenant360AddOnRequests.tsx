"use client";
// app/admin/tenants/_components/Tenant360AddOnRequests.tsx — AA-1036 R3 (root ADR 0019 d7; clarifications TL-7):
// Tenant 360 › Subscription › Add-on requests. Staff only; the agency asks from its app ("Request add-on"), the staff
// bell says so, and an admin or the tenant's account manager (customer_success, own book; clarifications TL-7 step 4)
// decides here.
//
// GET /api/admin/tenants/{id}/addon-requests (pending first). Approve: a plan of the request's product (+ seats for the
// CRM, + an optional note), POST …/{request_id}/approve, which enqueues the tenant_provision attach job; the drawer waits
// for it like "Add an add-on" (runSaga polls GET …/{request_id}, readable by the account manager too). An approval shows
// its setup: "Setting up" while the job runs, "Approved, setup failed" when it failed for good (the add-on is not on).
// Decline: a reason the agency sees, POST …/decline. Both audited.
// Kit only (Section, DataTable, Drawer, Field, Select, Input, Textarea, Badge, Button, Toast).

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge, Button, DataTable, DateText, Drawer, ErrorState, Field, Input, K, Section, Select, Textarea, useT, useToast,
  type ColumnDef,
} from "../../../_kit";
import {
  PLANS_KEY, addOnDecisionPath, addOnRequestPath, addOnRequestsKey, addOnRequestsPath, approveBody, requestShown,
  requestTone, runSaga, sagaFailed, sendJson, sortAddOnRequests, subscriptionKey,
  type AddOnRequestRow, type AddOnRequestsResp, type SubscriptionResp,
} from "@/lib/tenantPlans";
import { T360_SUB } from "./subscription.messages";

type Decision = { row: AddOnRequestRow; verb: "approve" | "decline" };

function DecisionDrawer({ tenantId, data, decision, onClose }: {
  tenantId: string; data: SubscriptionResp; decision: Decision; onClose: () => void;
}) {
  const t = useT(T360_SUB);
  const toast = useToast();
  const qc = useQueryClient();
  const { row, verb } = decision;
  const plans = data.add_on_plans.filter((p) => p.product === row.product);
  const [planKey, setPlanKey] = useState(plans[0]?.plan_key ?? "");
  const [seats, setSeats] = useState("");
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const approval = approveBody(planKey, seats, text);
  const reason = text.trim();
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: addOnRequestsKey(tenantId) });
    void qc.invalidateQueries({ queryKey: subscriptionKey(tenantId) });
    void qc.invalidateQueries({ queryKey: PLANS_KEY });
  };
  const run = useMutation({
    mutationFn: async () => {
      if (verb === "decline") {
        await sendJson(addOnDecisionPath(tenantId, row.request_id, "decline"), "POST", { reason });
        return null;
      }
      return runSaga(addOnDecisionPath(tenantId, row.request_id, "approve"), approval.body,
                     { pollPath: () => addOnRequestPath(tenantId, row.request_id) });
    },
    onSuccess: (job) => {
      refresh();
      if (job === null) { toast.success(t("declineDone")); onClose(); return; }
      if (job.status === "succeeded") { toast.success(t("approveDone")); onClose(); }
      else if (sagaFailed(job) && job.status !== "queued" && job.status !== "running") {
        setError(t("jobFailed", job.error ?? job.status));
      } else { toast.success(t("jobPending")); onClose(); }
    },
    onError: (e) => setError(t("failed", e instanceof Error ? e.message : String(e))),
  });
  const product = t(`product_${row.product}`);
  const canSubmit = verb === "approve" ? approval.body !== null : reason.length > 0 && reason.length <= 500;
  return (
    <Drawer open onClose={onClose} width={480}
      title={verb === "approve" ? t("approveTitle", product) : t("declineTitle", product)}
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={onClose} disabled={run.isPending}>{t("close")}</Button>
          <Button variant={verb === "approve" ? "primary" : "danger"} disabled={!canSubmit || run.isPending}
            testId={`addon-request-${verb}-submit`} onClick={() => { setError(""); run.mutate(); }}>
            {run.isPending ? t("working") : verb === "approve" ? t("submitApprove") : t("submitDecline")}
          </Button>
        </div>
      }>
      <div data-testid="addon-request-drawer" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {row.note && (
          <Field label={t("colNote")}>
            <p style={{ margin: 0, fontSize: 13, color: K.ink, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
              {row.note}
            </p>
          </Field>
        )}
        {verb === "approve" && (
          <>
            <Field label={t("fieldPlan")} error={approval.error === "plan" ? t("err_plan") : undefined}>
              <Select value={planKey} disabled={run.isPending} onChange={(e) => setPlanKey(e.target.value)}
                options={plans.map((p) => ({ value: p.plan_key, label: `${p.name} (${p.plan_key})` }))} />
            </Field>
            {row.product === "crm" && (
              <Field label={t("fieldSeats")} hint={t("fieldSeatsHint")}
                error={approval.error === "seats" ? t("err_seats") : undefined}>
                <Input inputMode="numeric" value={seats} disabled={run.isPending}
                  onChange={(e) => setSeats(e.target.value)} />
              </Field>
            )}
          </>
        )}
        <Field label={verb === "approve" ? t("fieldNoteOptional") : t("fieldDeclineReason")}
          required={verb === "decline"}>
          <Textarea rows={3} maxLength={500} value={text} disabled={run.isPending}
            onChange={(e) => setText(e.target.value)} />
        </Field>
        {error && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{error}</span>}
      </div>
    </Drawer>
  );
}

export function Tenant360AddOnRequests({ tenantId, data, canDecide }: {
  tenantId: string; data: SubscriptionResp; canDecide: boolean;
}) {
  const t = useT(T360_SUB);
  const q = useQuery({ queryKey: addOnRequestsKey(tenantId),
                       queryFn: () => sendJson<AddOnRequestsResp>(addOnRequestsPath(tenantId), "GET") });
  const [decision, setDecision] = useState<Decision | null>(null);
  const rows = useMemo(() => sortAddOnRequests(q.data?.items ?? []), [q.data]);

  const cols = useMemo<ColumnDef<AddOnRequestRow, unknown>[]>(() => [
    { accessorKey: "product", header: t("colProduct"), cell: (c) => t(`product_${c.row.original.product}`) },
    { accessorKey: "requested_at", header: t("colRequested"),
      cell: (c) => <DateText value={c.row.original.requested_at} /> },
    { accessorKey: "note", header: t("colNote"),
      cell: (c) => (
        <span style={{ color: c.row.original.note ? K.ink : K.muted, overflowWrap: "anywhere" }}>
          {c.row.original.note ?? t("none")}
        </span>
      ) },
    { accessorKey: "status", header: t("colStatus"),
      cell: (c) => {
        const shown = requestShown(c.row.original);
        return <Badge tone={requestTone(shown)}>{t(`req_${shown}`)}</Badge>;
      } },
    { id: "decision", header: t("colDecision"),
      cell: (c) => {
        const r = c.row.original;
        if (r.status === "pending") {
          if (!canDecide) return t("none");
          return (
            <span style={{ display: "inline-flex", gap: 6 }} onClick={(e) => e.stopPropagation()}
              data-testid={`addon-request-actions-${r.request_id}`}>
              <Button size="sm" variant="primary" onClick={() => setDecision({ row: r, verb: "approve" })}>
                {t("approve")}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setDecision({ row: r, verb: "decline" })}>
                {t("decline")}
              </Button>
            </span>
          );
        }
        return (
          <span style={{ display: "flex", flexDirection: "column", minWidth: 0, fontSize: 12 }}>
            {r.plan_key && <span style={{ color: K.ink }}>{r.plan_key}</span>}
            {r.decision_note && <span style={{ color: K.muted, overflowWrap: "anywhere" }}>{r.decision_note}</span>}
            {r.decided_at && <span style={{ color: K.muted }}><DateText value={r.decided_at} /></span>}
          </span>
        );
      } },
  ], [t, canDecide]);

  const pending = q.data?.pending ?? 0;
  return (
    <Section title={t("reqTitle")} description={t("reqHint")} padded={false} testId="tenant-addon-requests"
      actions={pending > 0 ? <Badge tone="warning">{t("reqPending", pending)}</Badge> : undefined}>
      {q.error ? (
        <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} onRetry={() => void q.refetch()} />
      ) : (
        <DataTable<AddOnRequestRow> tableId={`tenant-addon-requests-${tenantId}`} data={rows} columns={cols}
          getRowId={(r) => r.request_id} paginate={false} compact tableMinWidth={720} loading={q.isPending}
          emptyTitle={t("noRequests")} />
      )}
      {decision && (
        <DecisionDrawer tenantId={tenantId} data={data} decision={decision} onClose={() => setDecision(null)} />
      )}
    </Section>
  );
}
