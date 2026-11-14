"use client";
// app/admin/tenants/_components/Tenant360AiBudget.tsx — AA-1016 UI (S262 slice C4): the tenant's monthly AI budget on
// Tenant 360 › Cost. Staff only; a tenant never sees it.
//
// GET /api/admin/ai-gateway/tenant-budgets/{tenant_id} (spend.read): the budget row (or none) and this month's AI
// spend (llm_call_log of the tenant, shadow rows out: the same figure as the Cost tab). Edit opens a kit Drawer; Save
// asks to confirm and PUTs the whole row (spend.write, audited); "Remove the budget" DELETEs it (back to no budget,
// audited). Before CIS migration 321 the read says "not ready" and the tile explains why (no edit).
// AA-1028 R1 slice 3: the gateway enforces the tenant's resolved budget (an override on the Subscription tab > the row
// edited here > the plan's included spend), so the tile shows that effective budget (``effective``) and its source;
// the editor still edits this tenant's own row.
// S280 (AA-1096): Set / Edit only for a role that writes spend (spend.write; finance and customer_success read the tile
// and got a 403); a tighten-only role (no settings.write, S279) sees no Remove, the current values as its limit and
// what it may not loosen before saving; the editor says when a Subscription override wins (a change here then has no
// effect while it lasts).
// Kit only (KpiTile, Drawer, Field, Input, Select, Textarea, ConfirmModal, Toast).

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge, Button, ConfirmModal, DateText, Drawer, Field, Input, K, KpiTile, Select, Textarea, ToastProvider, apiGet,
  apiSend, useAdminIdentity, useT, useToast,
} from "../../../_kit";
import {
  actorLabel, isReadOnlyRefusal, pct, sameTenantDraft, tenantBudgetBody, tenantBudgetDraftOf, tenantBudgetPath, tenantBudgetTone, usd,
  type TenantBudgetDraft, type TenantBudgetError, type TenantBudgetResp, type TenantBudgetSource,
  type TenantBudgetState,
} from "@/lib/aiGateway";
import { TENANT_BUDGET_KINDS, anyOf, isTightenOnlyRefusal, loosenedFields, tightHint } from "@/lib/capTighten";
import { can, isTightenOnly } from "@/lib/rbac";
import type { Translator } from "../../../_kit/core/i18n";
import { T360_BUDGET } from "./tenant360Budget.messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45 } as const;

const ERROR_KEY = {
  monthly: "err_monthly", alertPct: "err_alertPct", approvalPct: "err_approvalPct", order: "err_order",
  notes: "err_notes",
} as const satisfies Record<TenantBudgetError, keyof typeof T360_BUDGET.en>;

const STATE_KEY = {
  unlimited: "state_unlimited", ok: "state_ok", alert: "state_alert", approval: "state_approval", over: "state_over",
} as const satisfies Record<TenantBudgetState, keyof typeof T360_BUDGET.en>;

const SOURCE_KEY = {
  subscription_override: "source_subscription_override", budget_row: "source_budget_row", plan: "source_plan",
  none: "source_none",
} as const satisfies Record<TenantBudgetSource, keyof typeof T360_BUDGET.en>;

type Base = { monthly_usd?: number | null; alert_pct?: number | null; approval_pct?: number | null;
  hard_stop?: boolean | null } | null;

/** The budget a tighten-only role must not loosen: the tenant's row, else the budget in force (plan / override), as
 *  the backend compares (tenant_ai_budget.upsert, S280). */
function tightenBase(data: TenantBudgetResp): Base {
  if (data.budget) return data.budget;
  const e = data.effective;
  if (!e || e.monthly_usd == null) return null;
  return { monthly_usd: e.monthly_usd, alert_pct: e.alert_pct, approval_pct: e.approval_pct, hard_stop: e.hard_stop };
}

/** The editor's first draft: the tenant's row; S280: a tighten-only role's first row starts from the budget in force
 *  (its plan's), which it may only tighten. */
function startDraft(data: TenantBudgetResp, tightenOnly: boolean): TenantBudgetDraft {
  const base = tightenOnly && !data.budget ? tightenBase(data) : null;
  if (!base) return tenantBudgetDraftOf(data.budget);
  return tenantBudgetDraftOf({ monthly_usd: base.monthly_usd ?? null, alert_pct: base.alert_pct ?? 100,
    approval_pct: base.approval_pct ?? 150, hard_stop: base.hard_stop ?? true, notes: null });
}

/** S280: what a tighten-only role may not do here (loosen these fields; soften the hold) and each field's hint. */
function useTenantTighten(data: TenantBudgetResp, tightenOnly: boolean, body: object | null | undefined) {
  const t = useT(T360_BUDGET);
  const base = useMemo(() => tightenBase(data), [data]);
  const b = base ?? {};
  const worse = tightenOnly && body ? loosenedFields(base, body, TENANT_BUDGET_KINDS) : [];
  const stopLocked = tightenOnly && b.hard_stop !== false;
  const names: Record<string, string> = { monthly_usd: t("fieldMonthly"), alert_pct: t("fieldAlert"),
    approval_pct: t("fieldApproval"), hard_stop: t("fieldAtApproval") };
  return {
    stopLocked,
    worseText: worse.length > 0 && t("tightenWorse", worse.map((f) => names[f] ?? f).join(", ")),
    hints: {
      monthly: tightHint(tightenOnly, b.monthly_usd, (v) => t("tightenMonthlyHint", usd(v, 2)), t("fieldMonthlyHint")),
      alert: tightHint(tightenOnly, b.alert_pct, (v) => t("tightenPctHint", v), t("fieldAlertHint")),
      approval: tightHint(tightenOnly, b.approval_pct, (v) => t("tightenPctHint", v), t("fieldApprovalHint")),
      stop: stopLocked ? t("tightenStopHint") : t("fieldAtApprovalHint"),
    },
  };
}

/** A refused write in the UI language (a read-only session; S280: a tighten-only role). */
function useRefusal() {
  const t = useT(T360_BUDGET);
  return (e: unknown) => {
    if (isReadOnlyRefusal(e)) return t("readOnlySession");
    if (isTightenOnlyRefusal(e)) return t("tightenOnlyRefused");
    return e instanceof Error ? e.message : String(e);
  };
}

/** The notes above the fields: the editor's intro, a Subscription override that wins (S280: a change here then has no
 *  effect while it lasts), and the tighten-only rule. */
function EditorNotes({ overrideWins, tightenOnly }: { overrideWins: boolean; tightenOnly: boolean }) {
  const t = useT(T360_BUDGET);
  return (
    <>
      <p style={{ ...small, margin: 0 }}>{t("editIntro")}</p>
      {overrideWins && (
        <p role="status" data-testid="tenant-ai-budget-override-wins"
          style={{ ...small, margin: 0, color: K.amber }}>{t("overrideWins")}</p>
      )}
      {tightenOnly && <p style={{ ...small, margin: 0 }}>{t("tightenOnlyNote")}</p>}
    </>
  );
}

function EditorFooter({ canRemove, busy, saving, blocked, onRemove, onSave, onClose }: {
  canRemove: boolean; busy: boolean; saving: boolean; blocked: boolean; onRemove: () => void; onSave: () => void;
  onClose: () => void;
}) {
  const t = useT(T360_BUDGET);
  return (
    <div style={{ display: "flex", gap: 8, justifyContent: "space-between", flexWrap: "wrap" }}>
      <span>
        {canRemove && (
          <Button variant="ghost" onClick={onRemove} disabled={busy} testId="tenant-ai-budget-remove">{t("remove")}</Button>
        )}
      </span>
      <span style={{ display: "inline-flex", gap: 8 }}>
        <Button variant="ghost" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
        <Button variant="primary" disabled={blocked} onClick={onSave}
          testId="tenant-ai-budget-save">{saving ? t("saving") : t("save")}</Button>
      </span>
    </div>
  );
}

function SaveSummary({ body }: { body: ReturnType<typeof tenantBudgetBody>["body"] }) {
  const t = useT(T360_BUDGET);
  if (!body) return null;
  if (body.monthly_usd == null) return <span>{t("confirmNoAmount")}</span>;
  return <span>{t("confirmSaveBody", usd(body.monthly_usd, 2), body.alert_pct, body.approval_pct,
    body.hard_stop ? t("holdJobs") : t("alertOnly"))}</span>;
}

function BudgetEditor({ data, tightenOnly, onClose }: { data: TenantBudgetResp; tightenOnly: boolean; onClose: () => void }) {
  const t = useT(T360_BUDGET);
  const toast = useToast();
  const qc = useQueryClient();
  const refusal = useRefusal();
  const start = useMemo(() => startDraft(data, tightenOnly), [data, tightenOnly]);
  const [draft, setDraft] = useState<TenantBudgetDraft>(start);
  const [confirm, setConfirm] = useState<"save" | "remove" | null>(null);
  const [apiError, setApiError] = useState("");
  const { body, error } = tenantBudgetBody(draft);
  const changed = !data.budget || !sameTenantDraft(draft, start);
  const tight = useTenantTighten(data, tightenOnly, body);
  const set = (p: Partial<TenantBudgetDraft>) => setDraft((d) => ({ ...d, ...p }));
  const done = (msg: string) => {
    toast.success(msg);
    qc.invalidateQueries({ queryKey: ["tenant360", data.tenant_id, "ai-budget"] });
    qc.invalidateQueries({ queryKey: ["ai-gateway", "budgets"] });
    onClose();
  };
  const fail = (e: unknown) => {
    setConfirm(null);
    setApiError(refusal(e));
  };
  const save = useMutation({
    mutationFn: () => apiSend(tenantBudgetPath(data.tenant_id), { method: "PUT", body, admin: true }),
    onSuccess: () => done(t("saved")),
    onError: fail,
  });
  const remove = useMutation({
    mutationFn: () => apiSend(tenantBudgetPath(data.tenant_id), { method: "DELETE", admin: true }),
    onSuccess: () => done(t("removed")),
    onError: fail,
  });
  const busy = save.isPending || remove.isPending;
  const err = (k: TenantBudgetError) => (error === k ? t(ERROR_KEY[k]) : undefined);
  const name = data.tenant_name ?? t("thisTenant");

  return (
    <Drawer open onClose={onClose} title={t("editTitle", name)} width={520}
      footer={<EditorFooter canRemove={!!data.budget && !tightenOnly} busy={busy} saving={save.isPending}
        blocked={anyOf(!body, !changed, tight.worseText, busy)} onRemove={() => setConfirm("remove")}
        onSave={() => setConfirm("save")} onClose={onClose} />}>
      <div data-testid="tenant-ai-budget-editor" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <EditorNotes overrideWins={data.effective?.source === "subscription_override"} tightenOnly={tightenOnly} />
        <Field label={t("fieldMonthly")} error={err("monthly")} hint={tight.hints.monthly}>
          <Input inputMode="decimal" value={draft.monthly} placeholder={t("noAmount")} disabled={busy}
            onChange={(e) => set({ monthly: e.target.value })} data-testid="tenant-ai-budget-monthly" />
        </Field>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 12 }}>
          <Field label={t("fieldAlert")} error={err("alertPct")} hint={tight.hints.alert}>
            <Input inputMode="numeric" value={draft.alert_pct} disabled={busy}
              onChange={(e) => set({ alert_pct: e.target.value })} />
          </Field>
          <Field label={t("fieldApproval")} error={err("approvalPct") ?? err("order")} hint={tight.hints.approval}>
            <Input inputMode="numeric" value={draft.approval_pct} disabled={busy}
              onChange={(e) => set({ approval_pct: e.target.value })} />
          </Field>
        </div>
        <Field label={t("fieldAtApproval")} hint={tight.hints.stop}>
          <Select value={draft.hard_stop ? "hold" : "alert"} disabled={busy || tight.stopLocked}
            onChange={(e) => set({ hard_stop: e.target.value === "hold" })}
            options={[{ value: "hold", label: t("holdJobs") }, { value: "alert", label: t("alertOnly") }]} />
        </Field>
        <Field label={t("fieldNotes")} error={err("notes")}>
          <Textarea value={draft.notes} rows={3} maxLength={2000} disabled={busy}
            onChange={(e) => set({ notes: e.target.value })} />
        </Field>
        <AlertLines lines={[tight.worseText, apiError]} />
      </div>
      <ConfirmModal open={confirm === "save"} onClose={() => setConfirm(null)} onConfirm={() => save.mutate()}
        title={t("confirmSaveTitle", name)} confirmLabel={t("save")} busy={save.isPending}
        testId="tenant-ai-budget-confirm" body={<SaveSummary body={body} />} />
      <ConfirmModal open={confirm === "remove"} onClose={() => setConfirm(null)} onConfirm={() => remove.mutate()}
        title={t("confirmRemoveTitle", name)} body={t("confirmRemoveBody")} confirmLabel={t("remove")}
        busy={remove.isPending} destructive testId="tenant-ai-budget-confirm-remove" />
    </Drawer>
  );
}

/** The problems under the fields, each as an alert line (empty ones are skipped). */
function AlertLines({ lines }: { lines: (string | false | null | undefined)[] }) {
  return <>{lines.filter(Boolean).map((l) => (
    <span key={String(l)} role="alert" style={{ fontSize: 12, color: K.danger }}>{l}</span>))}</>;
}

/** The tile's hint: this month's spend against the budget in force (or none), "not ready" before the table exists. */
function tileHint(t: Translator<typeof T360_BUDGET.en>, d: TenantBudgetResp | undefined,
  shown: { monthly_usd: number | null; used_pct: number | null } | null): string | undefined {
  if (!d) return undefined;
  if (!d.ready) return t("notReady");
  if (!shown || shown.monthly_usd == null) return t("spentNoBudget", usd(d.month_spent_usd));
  return t("spentOf", usd(d.month_spent_usd), shown.monthly_usd <= 0 ? t("noAiSpend") : pct(shown.used_pct));
}

function AiBudgetTile({ tenantId }: { tenantId: string }) {
  const t = useT(T360_BUDGET);
  const role = useAdminIdentity().role;
  // S280: the PUT / DELETE need spend.write; finance and customer_success read the tile only.
  const canWrite = can(role, "spend.write");
  const tightenOnly = isTightenOnly(role);
  const [editing, setEditing] = useState(false);
  const q = useQuery({
    queryKey: ["tenant360", tenantId, "ai-budget"],
    queryFn: () => apiGet<TenantBudgetResp>(tenantBudgetPath(tenantId), { admin: true }),
    retry: false,
  });
  const d = q.data;
  const b = d?.budget ?? null;
  // what the gateway applies (effective) when the plan catalogue exists; before it, the row alone decides
  const e = d?.effective ?? null;
  const shown = e ?? (b ? { monthly_usd: b.monthly_usd, used_pct: b.used_pct, state: b.state } : null);
  const value = !d ? undefined : !d.ready ? "—" : shown?.monthly_usd == null ? t("noBudget") : usd(shown.monthly_usd, 2);
  const hint = q.error ? t("loadError") : tileHint(t, d, shown);
  return (
    <div data-testid="tenant360-ai-budget"
      style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12, minWidth: 0, marginBottom: 10 }}>
      <div style={{ flex: "0 1 300px", minWidth: 0 }}>
        <KpiTile size="sm" label={t("tileLabel")} value={value} hint={hint}
          valueTone={shown && (shown.state === "approval" || shown.state === "alert" || shown.state === "over")
            ? "warning" : undefined} />
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", minWidth: 0 }}>
        {shown && shown.monthly_usd != null && (
          <Badge tone={tenantBudgetTone(shown.state)} dot={false} style={{ whiteSpace: "nowrap" }}>
            {t(STATE_KEY[shown.state] ?? "state_ok")}
          </Badge>
        )}
        {e && (
          <span style={{ ...small, color: e.source === "subscription_override" ? K.amber : K.muted }}
            data-testid="tenant-ai-budget-source">
            {t(SOURCE_KEY[e.source] ?? "source_none")}
          </span>
        )}
        {b && (
          <span style={small}>
            {actorLabel(b, t("staffMember"), (ref) => t("staffLogin", ref)) ?? "—"}{b.updated_at && <> · <DateText value={b.updated_at} relative /></>}
          </span>
        )}
        {d?.ready && canWrite && (
          <span data-testid="tenant-ai-budget-edit" style={{ display: "inline-flex" }}>
            <Button size="sm" variant="secondary" onClick={() => setEditing(true)}>
              {b ? t("edit") : t("set")}
            </Button>
          </span>
        )}
      </div>
      {editing && d && canWrite && <BudgetEditor data={d} tightenOnly={tightenOnly} onClose={() => setEditing(false)} />}
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
