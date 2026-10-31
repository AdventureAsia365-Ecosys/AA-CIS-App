"use client";
// app/admin/tenants/_components/Tenant360AiBudget.tsx — AA-1016 UI (S262 slice C4): the tenant's monthly AI budget on
// Tenant 360 › Cost. Staff only; a tenant never sees it.
//
// GET /api/admin/ai-gateway/tenant-budgets/{tenant_id} (spend.read): the budget row (or none) and this month's AI
// spend (llm_call_log of the tenant, shadow rows out: the same figure as the Cost tab). Edit opens a kit Drawer; Save
// asks to confirm and PUTs the whole row (spend.write, audited); "Remove the budget" DELETEs it (back to no budget,
// audited). Before CIS migration 321 the read says "not ready" and the tile explains why (no edit). The budget is
// stored and shown only: holding new jobs at the approval level comes with the gateway check (later slice).
// Kit only (KpiTile, Drawer, Field, Input, Select, Textarea, ConfirmModal, Toast).

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge, Button, ConfirmModal, DateText, Drawer, Field, Input, K, KpiTile, Select, Textarea, ToastProvider, apiGet,
  apiSend, useT, useToast,
} from "../../../_kit";
import {
  actorLabel, isReadOnlyRefusal, pct, sameTenantDraft, tenantBudgetBody, tenantBudgetDraftOf, tenantBudgetPath,
  tenantBudgetTone, usd,
  type TenantBudgetDraft, type TenantBudgetError, type TenantBudgetResp, type TenantBudgetState,
} from "@/lib/aiGateway";
import { T360_BUDGET } from "./tenant360Budget.messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;

const ERROR_KEY = {
  monthly: "err_monthly", alertPct: "err_alertPct", approvalPct: "err_approvalPct", order: "err_order",
  notes: "err_notes",
} as const satisfies Record<TenantBudgetError, keyof typeof T360_BUDGET.en>;

const STATE_KEY = {
  unlimited: "state_unlimited", ok: "state_ok", alert: "state_alert", approval: "state_approval", over: "state_over",
} as const satisfies Record<TenantBudgetState, keyof typeof T360_BUDGET.en>;

function BudgetEditor({ data, onClose }: { data: TenantBudgetResp; onClose: () => void }) {
  const t = useT(T360_BUDGET);
  const toast = useToast();
  const qc = useQueryClient();
  const start = useMemo(() => tenantBudgetDraftOf(data.budget), [data.budget]);
  const [draft, setDraft] = useState<TenantBudgetDraft>(start);
  const [confirm, setConfirm] = useState<"save" | "remove" | null>(null);
  const [apiError, setApiError] = useState("");
  const { body, error } = tenantBudgetBody(draft);
  const changed = !data.budget || !sameTenantDraft(draft, start);
  const set = (p: Partial<TenantBudgetDraft>) => setDraft((d) => ({ ...d, ...p }));
  const done = (msg: string) => {
    toast.success(msg);
    qc.invalidateQueries({ queryKey: ["tenant360", data.tenant_id, "ai-budget"] });
    qc.invalidateQueries({ queryKey: ["ai-gateway", "budgets"] });
    onClose();
  };
  const save = useMutation({
    mutationFn: () => apiSend(tenantBudgetPath(data.tenant_id), { method: "PUT", body, admin: true }),
    onSuccess: () => done(t("saved")),
    onError: (e) => {
      setConfirm(null);
      setApiError(isReadOnlyRefusal(e) ? t("readOnlySession") : e instanceof Error ? e.message : String(e));
    },
  });
  const remove = useMutation({
    mutationFn: () => apiSend(tenantBudgetPath(data.tenant_id), { method: "DELETE", admin: true }),
    onSuccess: () => done(t("removed")),
    onError: (e) => {
      setConfirm(null);
      setApiError(isReadOnlyRefusal(e) ? t("readOnlySession") : e instanceof Error ? e.message : String(e));
    },
  });
  const busy = save.isPending || remove.isPending;
  const err = (k: TenantBudgetError) => (error === k ? t(ERROR_KEY[k]) : undefined);
  const name = data.tenant_name ?? t("thisTenant");

  return (
    <Drawer open onClose={onClose} title={t("editTitle", name)} width={520}
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "space-between", flexWrap: "wrap" }}>
          <span>
            {data.budget && (
              <Button variant="ghost" onClick={() => setConfirm("remove")} disabled={busy}
                testId="tenant-ai-budget-remove">{t("remove")}</Button>
            )}
          </span>
          <span style={{ display: "inline-flex", gap: 8 }}>
            <Button variant="ghost" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
            <Button variant="primary" disabled={!body || !changed || busy} onClick={() => setConfirm("save")}
              testId="tenant-ai-budget-save">{save.isPending ? t("saving") : t("save")}</Button>
          </span>
        </div>
      }>
      <div data-testid="tenant-ai-budget-editor" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <p style={{ ...small, margin: 0 }}>{t("editIntro")}</p>
        <Field label={t("fieldMonthly")} hint={t("fieldMonthlyHint")} error={err("monthly")}>
          <Input inputMode="decimal" value={draft.monthly} placeholder={t("noAmount")} disabled={busy}
            onChange={(e) => set({ monthly: e.target.value })} data-testid="tenant-ai-budget-monthly" />
        </Field>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 12 }}>
          <Field label={t("fieldAlert")} hint={t("fieldAlertHint")} error={err("alertPct")}>
            <Input inputMode="numeric" value={draft.alert_pct} disabled={busy}
              onChange={(e) => set({ alert_pct: e.target.value })} />
          </Field>
          <Field label={t("fieldApproval")} hint={t("fieldApprovalHint")}
            error={err("approvalPct") ?? err("order")}>
            <Input inputMode="numeric" value={draft.approval_pct} disabled={busy}
              onChange={(e) => set({ approval_pct: e.target.value })} />
          </Field>
        </div>
        <Field label={t("fieldAtApproval")} hint={t("fieldAtApprovalHint")}>
          <Select value={draft.hard_stop ? "hold" : "alert"} disabled={busy}
            onChange={(e) => set({ hard_stop: e.target.value === "hold" })}
            options={[{ value: "alert", label: t("alertOnly") }, { value: "hold", label: t("holdJobs") }]} />
        </Field>
        <Field label={t("fieldNotes")} error={err("notes")}>
          <Textarea value={draft.notes} rows={3} maxLength={2000} disabled={busy}
            onChange={(e) => set({ notes: e.target.value })} />
        </Field>
        {apiError && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{apiError}</span>}
      </div>
      <ConfirmModal open={confirm === "save"} onClose={() => setConfirm(null)} onConfirm={() => save.mutate()}
        title={t("confirmSaveTitle", name)} confirmLabel={t("save")} busy={save.isPending}
        testId="tenant-ai-budget-confirm"
        body={body && (
          <span>{body.monthly_usd == null ? t("confirmNoAmount")
            : t("confirmSaveBody", usd(body.monthly_usd, 2), body.alert_pct, body.approval_pct,
              body.hard_stop ? t("holdJobs") : t("alertOnly"))}</span>
        )} />
      <ConfirmModal open={confirm === "remove"} onClose={() => setConfirm(null)} onConfirm={() => remove.mutate()}
        title={t("confirmRemoveTitle", name)} body={t("confirmRemoveBody")} confirmLabel={t("remove")}
        busy={remove.isPending} destructive testId="tenant-ai-budget-confirm-remove" />
    </Drawer>
  );
}

function AiBudgetTile({ tenantId }: { tenantId: string }) {
  const t = useT(T360_BUDGET);
  const [editing, setEditing] = useState(false);
  const q = useQuery({
    queryKey: ["tenant360", tenantId, "ai-budget"],
    queryFn: () => apiGet<TenantBudgetResp>(tenantBudgetPath(tenantId), { admin: true }),
    retry: false,
  });
  const d = q.data;
  const b = d?.budget ?? null;
  const value = !d ? undefined : !d.ready ? "—" : b?.monthly_usd == null ? t("noBudget") : usd(b.monthly_usd, 2);
  const hint = q.error ? t("loadError")
    : !d ? undefined
      : !d.ready ? t("notReady")
        : b && b.monthly_usd != null
          ? t("spentOf", usd(d.month_spent_usd), pct(b.used_pct))
          : t("spentNoBudget", usd(d.month_spent_usd));
  return (
    <div data-testid="tenant360-ai-budget"
      style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12, minWidth: 0, marginBottom: 10 }}>
      <div style={{ flex: "0 1 300px", minWidth: 0 }}>
        <KpiTile size="sm" label={t("tileLabel")} value={value} hint={hint}
          valueTone={b && (b.state === "approval" || b.state === "alert" || b.state === "over") ? "warning" : undefined} />
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", minWidth: 0 }}>
        {b && b.monthly_usd != null && (
          <Badge tone={tenantBudgetTone(b.state)} dot={false} style={{ whiteSpace: "nowrap" }}>
            {t(STATE_KEY[b.state] ?? "state_ok")}
          </Badge>
        )}
        {b && (
          <span style={small}>
            {actorLabel(b, t("staffMember"), (ref) => t("staffLogin", ref)) ?? "—"}{b.updated_at && <> · <DateText value={b.updated_at} relative /></>}
          </span>
        )}
        {d?.ready && (
          <span data-testid="tenant-ai-budget-edit" style={{ display: "inline-flex" }}>
            <Button size="sm" variant="secondary" onClick={() => setEditing(true)}>
              {b ? t("edit") : t("set")}
            </Button>
          </span>
        )}
      </div>
      {editing && d && <BudgetEditor data={d} onClose={() => setEditing(false)} />}
    </div>
  );
}

/** The AI budget tile with its own toast provider (Tenant 360 tabs mount without one). */
export function Tenant360AiBudget({ tenantId }: { tenantId: string }) {
  return (
    <ToastProvider>
      <AiBudgetTile tenantId={tenantId} />
    </ToastProvider>
  );
}
