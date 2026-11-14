"use client";
// app/admin/tenants/_components/Tenant360Subscription.tsx — AA-1028 R1 slice 3 (root ADR 0019 d1 / d2): Tenant 360 ›
// Subscription. Staff only; a tenant never sees it.
//
// GET /api/admin/tenants/{id}/subscription: the account state, every subscription, the resolved entitlements with
// their source and the overrides. Add-ons: "Add an add-on" / "Change" run the tenant_provision saga (POST
// …/provision, one job, the UI waits for it), "Cancel" runs its cancel mode (POST …/provision/cancel). Overrides: PUT /
// DELETE …/entitlement-overrides/{key} (reason required, audited). Kit only (Section, DataTable, Drawer, Field, Select,
// Input, ConfirmModal, Badge, Toast). AA-1036 R3: the agency's add-on requests sit above the subscriptions
// (Tenant360AddOnRequests: approve runs the same saga, decline needs a reason). AA-1038 follow-up: the TripPlanner
// lead route sits below the overrides (Tenant360LeadRoute). AA-1086 G2: "End pilot" next to the pilot badge
// (Tenant360EndPilot, admin only) asks for a reason and calls POST …/pilot/end, which clears the pilot marker.
// S280 (AA-1096): the writes (add-on, cancel, override PUT / DELETE) need tenant.write, so finance and
// customer_success (who read the tab) see no write button and a clicked override opens read-only; an override value
// reads in its own unit; the limit field names its unit and the value in force; "By" names a person.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge, Button, ConfirmModal, DataTable, DateText, Drawer, ErrorState, Field, Input, K, Section, Select, Textarea,
  ToastProvider, mono, useT, useToast, type ColumnDef,
} from "../../../_kit";
import type { Translator } from "../../../_kit/core/i18n";
import {
  DEFAULT_OVERRIDE_RULES, FEATURES_API, LIVE_STATUSES, PLANS_KEY, defaultOverrideMode, limitText, newRequestKey,
  overrideBody, overrideDraftOf, overrideFeatures, overrideModes, overridePath, overrideUnit, runSaga, sagaFailed,
  sendJson, sourceTone, statusTone, subscriptionKey, subscriptionPath, unitText,
  type EntitlementRow, type Feature, type OverrideDraft, type OverrideDraftError, type OverrideRow, type OverrideRules,
  type Product, type SubscriptionResp, type SubscriptionRow,
} from "@/lib/tenantPlans";
import { actorLabel } from "@/lib/aiGateway";
import { can } from "@/lib/rbac";
import { useAdminIdentity } from "../../../_kit/useAdminIdentity";
import { T360_SUB } from "./subscription.messages";
import { Tenant360AddOnRequests } from "./Tenant360AddOnRequests";
import { Tenant360EndPilot } from "./Tenant360EndPilot";
import { Tenant360LeadRoute } from "./Tenant360LeadRoute";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45, margin: 0 } as const;

function words(t: Translator<typeof T360_SUB.en>) {
  return { on: t("on"), noLimit: t("noLimit"), perMonth: (v: string) => t("perMonth", v),
           usdMonth: (v: string) => t("usdMonth", v), perMinute: (v: string) => t("perMinute", v),
           seats: (v: string) => t("seats", v), days: (v: string) => t("days", v) };
}

/** The columns, without the write actions column for a role that may not write. */
function withoutActions<R>(canWrite: boolean, cols: ColumnDef<R, unknown>[]): ColumnDef<R, unknown>[] {
  return canWrite ? cols : cols.filter((c) => c.id !== "actions");
}

function unitWords(t: Translator<typeof T360_SUB.en>) {
  return { usdMonth: t("unit_usdMonth"), perMinute: t("unit_perMinute"), seats: t("unit_seats"), days: t("unit_days"),
           perMonth: t("unit_perMonth") };
}

function FeatureCell({ description, featureKey }: { description?: string; featureKey: string }) {
  return (
    <span style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
      {description && <span style={{ color: K.ink, overflowWrap: "anywhere" }}>{description}</span>}
      <span style={{ fontSize: 11, color: K.muted, fontFamily: mono, overflowWrap: "anywhere" }}>{featureKey}</span>
    </span>
  );
}

// ── add-on drawer ────────────────────────────────────────────────────────────────────────────────────────────────

function AddOnDrawer({ tenantId, data, start, onClose }: {
  tenantId: string; data: SubscriptionResp; start: SubscriptionRow | null; onClose: () => void;
}) {
  const t = useT(T360_SUB);
  const toast = useToast();
  const qc = useQueryClient();
  const [product, setProduct] = useState<Product>(start?.product ?? "crm");
  const plans = data.add_on_plans.filter((p) => p.product === product);
  const [planKey, setPlanKey] = useState(start?.plan_key ?? plans[0]?.plan_key ?? "");
  const [seats, setSeats] = useState(start?.seats == null ? "" : String(start.seats));
  const [error, setError] = useState("");
  const seatsBad = seats.trim() !== "" && (!/^\d{1,3}$/.test(seats.trim()) || Number(seats) < 1 || Number(seats) > 500);
  const run = useMutation({
    mutationFn: () => runSaga(`/api/admin/tenants/${tenantId}/provision`, {
      request_key: newRequestKey(), add_ons: [{ product, plan_key: planKey, seats: seats.trim() ? Number(seats) : null }],
    }),
    onSuccess: (job) => {
      void qc.invalidateQueries({ queryKey: subscriptionKey(tenantId) });
      void qc.invalidateQueries({ queryKey: PLANS_KEY });
      if (job.status === "succeeded") { toast.success(t("addOnDone")); onClose(); }
      else if (sagaFailed(job) && job.status !== "queued" && job.status !== "running") {
        setError(t("jobFailed", job.error ?? job.status));
      } else { toast.success(t("jobPending")); onClose(); }
    },
    onError: (e) => setError(t("failed", e instanceof Error ? e.message : String(e))),
  });
  return (
    <Drawer open onClose={onClose} width={480} title={t("addOnTitle", data.tenant.name)}
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={onClose} disabled={run.isPending}>{t("close")}</Button>
          <Button variant="primary" disabled={!planKey || seatsBad || run.isPending} testId="addon-save"
            onClick={() => { setError(""); run.mutate(); }}>
            {run.isPending ? t("working") : t("submitAddOn")}
          </Button>
        </div>
      }>
      <div data-testid="addon-drawer" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Field label={t("fieldProduct")}>
          <Select value={product} disabled={!!start || run.isPending}
            onChange={(e) => {
              const p = e.target.value as Product;
              setProduct(p);
              setPlanKey(data.add_on_plans.find((x) => x.product === p)?.plan_key ?? "");
            }}
            options={(["crm", "tripplanner"] as const).map((p) => ({ value: p, label: t(`product_${p}`) }))} />
        </Field>
        <Field label={t("fieldPlan")}>
          <Select value={planKey} disabled={run.isPending} onChange={(e) => setPlanKey(e.target.value)}
            options={plans.map((p) => ({ value: p.plan_key, label: `${p.name} (${p.plan_key})` }))} />
        </Field>
        {product === "crm" && (
          <Field label={t("fieldSeats")} hint={t("fieldSeatsHint")} error={seatsBad ? t("err_seats") : undefined}>
            <Input inputMode="numeric" value={seats} disabled={run.isPending} onChange={(e) => setSeats(e.target.value)} />
          </Field>
        )}
        {error && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{error}</span>}
      </div>
    </Drawer>
  );
}

// ── override drawer ──────────────────────────────────────────────────────────────────────────────────────────────

const OV_ERR = {
  feature: "err_feature", limit: "err_limit", reason: "err_reason", expires: "err_expires", flagLimit: "err_flagLimit",
  offLimit: "err_offLimit", limitRequired: "err_limitRequired", notOverridable: "err_notOverridable",
} as const satisfies Record<OverrideDraftError, keyof typeof T360_SUB.en>;

/** S280: the limit field's hint: its unit and what is in force now (plan, trial or this override), so a number is
 *  never a guess between dollars, calls and seats. */
function useLimitHint(feature: Feature | undefined, inForce: EntitlementRow | undefined): string {
  const t = useT(T360_SUB);
  const w = useMemo(() => words(t), [t]);
  const unit = feature && feature.value_kind !== "flag" ? t("limitUnit", unitText(feature.unit, unitWords(t))) : "";
  const now = inForce ? t("inForceNow", limitText(inForce.value_kind, inForce.unit, inForce.limit_value, w),
    t(`source_${inForce.source}`)) : "";
  return [unit, now, t("fieldLimitHint")].filter(Boolean).join(" ");
}

/** The override drawer's buttons: Remove and Save only for a role that writes (S280: others read the override). */
function OverrideFooter({ canRemove, canSave, busy, saving, removing, blocked, onRemove, onSave, onClose }: {
  canRemove: boolean; canSave: boolean; busy: boolean; saving: boolean; removing: boolean; blocked: boolean;
  onRemove: () => void; onSave: () => void; onClose: () => void;
}) {
  const t = useT(T360_SUB);
  return (
    <div style={{ display: "flex", gap: 8, justifyContent: "space-between", flexWrap: "wrap" }}>
      <span>{canRemove && <Button variant="ghost" disabled={busy} onClick={onRemove}>{t("remove")}</Button>}</span>
      <span style={{ display: "inline-flex", gap: 8 }}>
        <Button variant="ghost" onClick={onClose} disabled={saving || removing}>{t("close")}</Button>
        {canSave && (
          <Button variant="primary" disabled={blocked || busy} testId="override-save" onClick={onSave}>
            {saving ? t("working") : t("save")}</Button>
        )}
      </span>
    </div>
  );
}

/** The draft errors shown under their own field (the others show under the form). */
const FIELD_ERRORS = new Set<OverrideDraftError | null | undefined>(["limit", "expires"]);

function OverrideDrawer({ tenantId, existing, catalogue, rules, entitlements, readOnly, onClose }: {
  tenantId: string; existing: OverrideRow | null; catalogue: Feature[]; rules: OverrideRules;
  entitlements: EntitlementRow[]; readOnly: boolean; onClose: () => void;
}) {
  const t = useT(T360_SUB);
  const toast = useToast();
  const qc = useQueryClient();
  const [d, setD] = useState<OverrideDraft>(() => overrideDraftOf(existing));
  const [apiError, setApiError] = useState("");
  const [removing, setRemoving] = useState(false);
  const { body, error } = overrideBody(d, catalogue, undefined, rules);
  const feature = catalogue.find((f) => f.feature_key === d.feature_key);
  const choices = overrideFeatures(catalogue, rules);
  const set = (p: Partial<OverrideDraft>) => setD((x) => ({ ...x, ...p }));
  const done = (msg: string) => {
    toast.success(msg);
    void qc.invalidateQueries({ queryKey: subscriptionKey(tenantId) });
    onClose();
  };
  const save = useMutation({
    mutationFn: () => sendJson<{ applies: boolean }>(overridePath(tenantId, d.feature_key), "PUT", body),
    onSuccess: (r) => done(r.applies ? t("saved") : t("savedNotApplied")),
    onError: (e) => setApiError(t("failed", e instanceof Error ? e.message : String(e))),
  });
  const remove = useMutation({
    mutationFn: () => sendJson(overridePath(tenantId, d.feature_key), "DELETE"),
    onSuccess: () => done(t("removed")),
    onError: (e) => { setRemoving(false); setApiError(t("failed", e instanceof Error ? e.message : String(e))); },
  });
  const busy = save.isPending || remove.isPending || readOnly;
  const limitHint = useLimitHint(feature, entitlements.find((e) => e.feature_key === d.feature_key));
  // the first error the user can act on (a missing reason only once a feature is chosen)
  const shown = error && (error !== "reason" || d.reason !== "") ? t(OV_ERR[error]) : "";
  const modes = overrideModes(feature, rules);
  // "on" of a limit is "no limit"; a stored disabled limit (refused now) still shows what it is
  const modeLabel = (m: OverrideDraft["mode"]) =>
    (feature && feature.value_kind !== "flag" && m === "on" ? t("mode_noLimit") : t(`mode_${m}`));

  return (
    <Drawer open onClose={onClose} width={480} title={existing ? t("ovEditTitle", existing.feature_key) : t("ovNewTitle")}
      footer={<OverrideFooter canRemove={!!existing && !readOnly} canSave={!readOnly} busy={busy}
        saving={save.isPending} removing={remove.isPending} blocked={!body} onRemove={() => setRemoving(true)}
        onSave={() => { setApiError(""); save.mutate(); }} onClose={onClose} />}>
      <div data-testid="override-drawer" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {readOnly && <p style={small}>{t("readOnlyView")}</p>}
        <Field label={t("fieldFeature")}>
          <Select value={d.feature_key} disabled={!!existing || busy} placeholder={t("err_feature")}
            onChange={(e) => set({ feature_key: e.target.value, limit: "",
                                   mode: defaultOverrideMode(catalogue.find((f) => f.feature_key === e.target.value)) })}
            options={(existing && !choices.some((f) => f.feature_key === existing.feature_key)
              ? catalogue.filter((f) => f.feature_key === existing.feature_key) : choices)
              .map((f) => ({ value: f.feature_key, label: `${f.description} (${f.feature_key})` }))} />
        </Field>
        <Field label={t("fieldMode")}>
          <Select value={d.mode} disabled={busy} onChange={(e) => set({ mode: e.target.value as OverrideDraft["mode"] })}
            options={(modes.includes(d.mode) ? modes : [d.mode, ...modes])
              .map((m) => ({ value: m, label: modeLabel(m) }))} />
        </Field>
        {d.mode === "limit" && (
          <Field label={t("fieldLimit")} hint={limitHint}
            error={error === "limit" ? t("err_limit") : undefined}>
            <Input inputMode="decimal" value={d.limit} placeholder={t("noLimit")} disabled={busy}
              onChange={(e) => set({ limit: e.target.value })} data-testid="override-limit" />
          </Field>
        )}
        <Field label={t("fieldReason")} required>
          <Textarea rows={2} maxLength={500} value={d.reason} disabled={busy} onChange={(e) => set({ reason: e.target.value })} />
        </Field>
        <Field label={t("fieldExpires")} hint={t("fieldExpiresHint")} error={error === "expires" ? t("err_expires") : undefined}>
          <Input type="date" value={d.expires} disabled={busy} onChange={(e) => set({ expires: e.target.value })} />
        </Field>
        {feature && feature.product !== "content" && feature.product !== "platform" && <p style={small}>{t("appliesNote")}</p>}
        {!readOnly && shown && !FIELD_ERRORS.has(error) && <span style={{ fontSize: 12, color: K.muted }}>{shown}</span>}
        {apiError && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{apiError}</span>}
      </div>
      {existing && (
        <ConfirmModal open={removing} onClose={() => setRemoving(false)} onConfirm={() => remove.mutate()}
          title={t("remove")} body={t("ovEditTitle", existing.feature_key)} confirmLabel={t("remove")} destructive
          busy={remove.isPending} testId="override-remove-confirm" />
      )}
    </Drawer>
  );
}

// ── the tab ──────────────────────────────────────────────────────────────────────────────────────────────────────

export function Tenant360Subscription({ tenantId }: { tenantId: string }) {
  return (
    <ToastProvider>
      <SubscriptionTab tenantId={tenantId} />
    </ToastProvider>
  );
}

function SubscriptionTab({ tenantId }: { tenantId: string }) {
  const t = useT(T360_SUB);
  const toast = useToast();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: subscriptionKey(tenantId),
                       queryFn: () => sendJson<SubscriptionResp>(subscriptionPath(tenantId), "GET") });
  const featuresQ = useQuery({ queryKey: [...PLANS_KEY, "features"], staleTime: 5 * 60_000,
                               queryFn: () => sendJson<{ features: Feature[] }>(FEATURES_API, "GET").then((r) => r.features) });
  const [addOn, setAddOn] = useState<{ start: SubscriptionRow | null } | null>(null);
  const [cancelling, setCancelling] = useState<SubscriptionRow | null>(null);
  const [override, setOverride] = useState<{ row: OverrideRow | null } | null>(null);
  const w = useMemo(() => words(t), [t]);
  const { role } = useAdminIdentity();
  // S280: every write of this tab (add-on saga, cancel, override PUT / DELETE) needs tenant.write (admin only).
  const canWrite = can(role, "tenant.write");
  const writeAction = (node: React.ReactNode) => (canWrite ? node : undefined);

  const cancel = useMutation({
    mutationFn: (row: SubscriptionRow) => runSaga(`/api/admin/tenants/${tenantId}/provision/cancel`,
                                                  { request_key: newRequestKey(), products: [row.product] }),
    onSuccess: (job) => {
      setCancelling(null);
      void qc.invalidateQueries({ queryKey: subscriptionKey(tenantId) });
      if (job.status === "succeeded") toast.success(t("cancelDone"));
      else if (job.status === "queued" || job.status === "running") toast.success(t("jobPending"));
      else toast.error(t("jobFailed", job.error ?? job.status));
    },
    onError: (e) => { setCancelling(null); toast.error(t("failed", e instanceof Error ? e.message : String(e))); },
  });

  const subCols = useMemo<ColumnDef<SubscriptionRow, unknown>[]>(() => withoutActions(canWrite, [
    { accessorKey: "product", header: t("colProduct"), cell: (c) => t(`product_${c.row.original.product}`) },
    {
      accessorKey: "plan_key", header: t("colPlan"),
      cell: (c) => (
        <span style={{ display: "flex", flexDirection: "column" }}>
          <span style={{ color: K.ink }}>{c.row.original.plan_name}</span>
          <span style={{ fontSize: 11, color: K.muted, fontFamily: mono }}>
            {c.row.original.plan_key}{c.row.original.plan_retired ? ` · ${t("planRetired")}` : ""}
          </span>
        </span>
      ),
    },
    { accessorKey: "status", header: t("colStatus"),
      cell: (c) => <Badge tone={statusTone(c.row.original.status)}>{c.row.original.status}</Badge> },
    { accessorKey: "seats", header: t("colSeats"), cell: (c) => c.row.original.seats ?? t("none") },
    { accessorKey: "trial_ends_at", header: t("colTrialEnds"),
      cell: (c) => (c.row.original.trial_ends_at ? <DateText value={c.row.original.trial_ends_at} /> : t("none")) },
    { accessorKey: "started_at", header: t("colSince"),
      cell: (c) => (c.row.original.started_at ? <DateText value={c.row.original.started_at} /> : t("none")) },
    {
      id: "actions", header: t("colActions"),
      cell: (c) => {
        const r = c.row.original;
        if (r.product === "content") return null;
        const live = LIVE_STATUSES.includes(r.status);
        return (
          <span style={{ display: "inline-flex", gap: 6 }} onClick={(e) => e.stopPropagation()}>
            <Button size="sm" variant="secondary" onClick={() => setAddOn({ start: r })}>{t("change")}</Button>
            {live && <Button size="sm" variant="ghost" onClick={() => setCancelling(r)}>{t("cancelAddOn")}</Button>}
          </span>
        );
      },
    },
  ]), [t, canWrite]);

  const entCols = useMemo<ColumnDef<EntitlementRow, unknown>[]>(() => [
    { accessorKey: "feature_key", header: t("colFeature"),
      cell: (c) => <FeatureCell description={c.row.original.description} featureKey={c.row.original.feature_key} /> },
    { id: "value", header: t("colValue"),
      cell: (c) => limitText(c.row.original.value_kind, c.row.original.unit, c.row.original.limit_value, w) },
    { accessorKey: "source", header: t("colSource"),
      cell: (c) => <Badge tone={sourceTone(c.row.original.source)} dot={false}>{t(`source_${c.row.original.source}`)}</Badge> },
    { accessorKey: "expires_at", header: t("colExpires"),
      cell: (c) => (c.row.original.expires_at ? <DateText value={c.row.original.expires_at} /> : t("none")) },
  ], [t, w]);

  const ovCols = useMemo<ColumnDef<OverrideRow, unknown>[]>(() => [
    { accessorKey: "feature_key", header: t("colFeature"),
      cell: (c) => <FeatureCell featureKey={c.row.original.feature_key} /> },
    { id: "value", header: t("colValue"),
      cell: (c) => (!c.row.original.enabled ? <Badge tone="danger" dot={false}>{t("off")}</Badge>
        : limitText(c.row.original.value_kind, overrideUnit(c.row.original, featuresQ.data),
          c.row.original.limit_value, w)) },
    { accessorKey: "reason", header: t("colReason") },
    { id: "granted_by", header: t("colBy"),
      accessorFn: (r) => r.granted_by_name ?? r.granted_by,
      cell: (c) => {
        const r = c.row.original;
        return <span style={{ fontSize: 12 }} title={r.granted_by}>
          {actorLabel({ updated_by: r.granted_by, updated_by_kind: r.granted_by_kind, updated_by_name: r.granted_by_name },
            t("staffMember")) ?? "—"}
        </span>;
      } },
    { accessorKey: "expires_at", header: t("colExpires"),
      cell: (c) => (
        <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
          {c.row.original.expires_at ? <DateText value={c.row.original.expires_at} /> : t("none")}
          <Badge tone={c.row.original.active ? "success" : "neutral"} dot={false}>
            {c.row.original.active ? t("active") : t("expired")}
          </Badge>
        </span>
      ) },
  ], [t, w, featuresQ.data]);

  if (q.isPending) return <div style={{ padding: "12px 0", fontSize: 12, color: K.muted }}>{t("loading")}</div>;
  if (q.error || !q.data) {
    return <ErrorState message={`${t("loadError")}: ${(q.error as Error | null)?.message ?? ""}`}
      onRetry={() => void q.refetch()} />;
  }
  const data = q.data;
  const acct = data.tenant;

  return (
    <div data-testid={`tenant-subscription-${tenantId}`}
      style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
        <span style={{ color: K.muted }}>{t("account")}</span>
        <Badge tone={statusTone(acct.account_status)}>{acct.account_status}</Badge>
        <Badge tone={acct.in_service ? "success" : "danger"} dot={false}>
          {acct.in_service ? t("inService") : t("notInService")}
        </Badge>
        <Badge tone="neutral" dot={false}>{t("kind", acct.kind)}</Badge>
        {acct.is_pilot && acct.pilot_ends_at && <Badge tone="purple">{t("pilot", acct.pilot_ends_at.slice(0, 10))}</Badge>}
        <Tenant360EndPilot tenantId={tenantId} tenantName={acct.name} isPilot={acct.is_pilot} />
      </div>

      <Tenant360AddOnRequests tenantId={tenantId} data={data}
        canDecide={can(role, "tenant.write") || can(role, "tenant.book.write")} />

      <Section title={t("subsTitle")} description={t("subsHint")} padded={false} testId="tenant-subscriptions"
        actions={writeAction(<Button size="sm" variant="secondary" onClick={() => setAddOn({ start: null })}
          testId="addon-add">{t("addAddOn")}</Button>)}>
        <DataTable<SubscriptionRow> tableId={`tenant-subs-${tenantId}`} data={data.subscriptions} columns={subCols}
          getRowId={(r) => r.product} paginate={false} compact tableMinWidth={720} />
      </Section>

      <Section title={t("entTitle")} description={t("entHint")} padded={false} testId="tenant-entitlements">
        <DataTable<EntitlementRow> tableId={`tenant-ents-${tenantId}`} data={data.entitlements} columns={entCols}
          getRowId={(r) => r.feature_key} paginate={false} compact searchable tableMinWidth={640} />
      </Section>

      <Section title={t("ovTitle")} description={t("ovHint")} padded={false} testId="tenant-overrides"
        actions={writeAction(<Button size="sm" variant="secondary" disabled={!featuresQ.data} testId="override-add"
          onClick={() => setOverride({ row: null })}>{t("addOverride")}</Button>)}>
        <DataTable<OverrideRow> tableId={`tenant-ovs-${tenantId}`} data={data.overrides} columns={ovCols}
          getRowId={(r) => r.feature_key} paginate={false} compact tableMinWidth={720}
          emptyTitle={t("noOverrides")} onRowClick={(r) => featuresQ.data && setOverride({ row: r })} />
      </Section>

      <Tenant360LeadRoute tenantId={tenantId} canEdit={can(role, "tenant.write")}
        crmCore={data.entitlements.some((e) => e.feature_key === "crm.core")} />

      {addOn && <AddOnDrawer tenantId={tenantId} data={data} start={addOn.start} onClose={() => setAddOn(null)} />}
      {override && featuresQ.data && (
        <OverrideDrawer tenantId={tenantId} existing={override.row} catalogue={featuresQ.data}
          rules={data.override_rules ?? DEFAULT_OVERRIDE_RULES} entitlements={data.entitlements}
          readOnly={!canWrite} onClose={() => setOverride(null)} />
      )}
      <ConfirmModal open={!!cancelling} onClose={() => setCancelling(null)}
        onConfirm={() => cancelling && cancel.mutate(cancelling)}
        title={cancelling ? t("cancelTitle", t(`product_${cancelling.product}`)) : ""} body={t("cancelBody")}
        confirmLabel={t("cancelAddOn")} destructive busy={cancel.isPending} testId="addon-cancel-confirm" />
    </div>
  );
}
