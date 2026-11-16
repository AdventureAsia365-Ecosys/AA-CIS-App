"use client";
// app/admin/tenants/_components/Tenant360Commercial.tsx — AA-1116 (option B), inside Tenant 360 › Subscription:
//   * Add-ons (custom domain, AI pack x quantity; shared.tenant_addon): grant / change / cancel, reason required;
//   * Billing details (bill-to, billing currency, founding offer) and the terms of each subscription (billed monthly or
//     yearly, an agreed price with an optional end, "lock today's list price");
//   * Quote: the lines, the offer in force and the monthly / yearly totals in the tenant's currency.
// Writes need tenant.write (admin); the API re-checks it and audits every change. All values come from the API: prices
// from Plans & add-ons, the offers and lengths from Platform › Settings.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge, Button, ConfirmModal, DataTable, Drawer, Field, Input, K, Section, Select, mono, useT, useToast,
  type ColumnDef, type Translator,
} from "../../../_kit";
import {
  PLANS_API, billingDetailsKey, billingDetailsPath, money, quoteKey, quotePath, sendJson, subscriptionTermsPath,
  tenantAddOnsKey, tenantAddOnsPath,
  type BillingDetails, type Plan, type PlansResp, type QuoteLine, type QuoteSubscription, type TenantAddOn,
  type TenantQuote,
} from "@/lib/tenantPlans";
import { T360_COMMERCIAL } from "./commercial.messages";

const small = { fontSize: 12, color: K.muted, margin: 0 } as const;
const grid = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 200px), 1fr))", gap: 12 };
const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function Tenant360Commercial({ tenantId, canWrite }: { tenantId: string; canWrite: boolean }) {
  return (
    <div data-testid="tenant360-commercial" style={{ display: "grid", gap: 16 }}>
      <AddOns tenantId={tenantId} canWrite={canWrite} />
      <Billing tenantId={tenantId} canWrite={canWrite} />
      <Quote tenantId={tenantId} />
    </div>
  );
}

function useRefresh(tenantId: string) {
  const qc = useQueryClient();
  return () => {
    for (const key of [tenantAddOnsKey(tenantId), billingDetailsKey(tenantId), quoteKey(tenantId),
                       ["tenant360", tenantId, "subscription"]]) void qc.invalidateQueries({ queryKey: key });
  };
}

function AddOns({ tenantId, canWrite }: { tenantId: string; canWrite: boolean }) {
  const t = useT(T360_COMMERCIAL);
  const toast = useToast();
  const refresh = useRefresh(tenantId);
  const q = useQuery({ queryKey: tenantAddOnsKey(tenantId),
                       queryFn: () => sendJson<{ add_ons: TenantAddOn[] }>(tenantAddOnsPath(tenantId), "GET") });
  const [granting, setGranting] = useState(false);
  const [cancelling, setCancelling] = useState<TenantAddOn | null>(null);
  const cancel = useMutation({
    mutationFn: ({ row, reason }: { row: TenantAddOn; reason: string }) =>
      sendJson(`${tenantAddOnsPath(tenantId)}/${row.addon_id}/cancel`, "POST", { reason }),
    onSuccess: () => { setCancelling(null); toast.success(t("saved")); refresh(); },
    onError: (e) => { setCancelling(null); toast.error(t("failed", errText(e))); },
  });
  const cols: ColumnDef<TenantAddOn, unknown>[] = [
    { accessorKey: "plan_name", header: t("colAddOn"),
      cell: (c) => (
        <span style={{ display: "flex", flexDirection: "column" }}>
          <span style={{ color: K.ink }}>{c.row.original.plan_name}</span>
          <span style={{ fontSize: 11, color: K.muted, fontFamily: mono }}>{c.row.original.plan_key}</span>
        </span>) },
    { accessorKey: "quantity", header: t("colQuantity") },
    { id: "status", header: t("colStatus"), cell: (c) => {
      const r = c.row.original;
      if (r.status !== "canceled" && !r.live) return <Badge tone="neutral">{t("ended")}</Badge>;
      return <Badge tone={r.status === "active" ? "success" : r.status === "trialing" ? "info" : "neutral"}>
        {t(`status_${r.status}`)}</Badge>;
    } },
    { id: "trial", header: t("colTrialEnds"), cell: (c) => c.row.original.trial_ends_at?.slice(0, 10) ?? t("none") },
    { id: "ends", header: t("colEnds"), cell: (c) => c.row.original.ends_at?.slice(0, 10) ?? t("none") },
    { id: "price", header: t("colPrice"), cell: (c) => money(c.row.original.price_usd_monthly, "USD") },
    ...(canWrite ? [{ id: "actions", header: "", cell: (c: { row: { original: TenantAddOn } }) =>
      c.row.original.status === "canceled" ? null : (
        <Button size="sm" variant="ghost" onClick={(e: React.MouseEvent) => { e.stopPropagation();
          setCancelling(c.row.original); }}>{t("cancel")}</Button>) } as ColumnDef<TenantAddOn, unknown>] : []),
  ];
  return (
    <Section title={t("addOnsTitle")} description={t("addOnsHint")} padded={false} testId="tenant360-addons"
      actions={canWrite ? <Button size="sm" variant="secondary" testId="addon-grant"
        onClick={() => setGranting(true)}>{t("grant")}</Button> : undefined}>
      <DataTable<TenantAddOn> tableId={`tenant-addons-${tenantId}`} data={q.data?.add_ons ?? []} columns={cols}
        getRowId={(r) => r.addon_id} loading={q.isLoading} paginate={false} compact tableMinWidth={720}
        emptyTitle={t("noAddOns")} />
      {granting && <GrantDrawer tenantId={tenantId} onClose={() => setGranting(false)} onSaved={refresh} />}
      <ConfirmModal open={!!cancelling} onClose={() => setCancelling(null)}
        onConfirm={(reason) => cancelling && cancel.mutate({ row: cancelling, reason })}
        title={cancelling ? t("cancelTitle", cancelling.plan_name) : ""} body={t("cancelBody")}
        confirmLabel={t("cancel")} destructive busy={cancel.isPending} testId="tenant-addon-cancel"
        reason={{ label: t("reason"), maxLength: 500, required: true }} />
    </Section>
  );
}

function GrantDrawer({ tenantId, onClose, onSaved }: { tenantId: string; onClose: () => void; onSaved: () => void }) {
  const t = useT(T360_COMMERCIAL);
  const toast = useToast();
  const plansQ = useQuery({ queryKey: ["admin-plans", "addon"],
                            queryFn: () => sendJson<PlansResp>(`${PLANS_API}?product=addon`, "GET") });
  const plans: Plan[] = plansQ.data?.plans ?? [];
  const [planKey, setPlanKey] = useState("");
  const [quantity, setQuantity] = useState("1");
  const [ends, setEnds] = useState("");
  const [reason, setReason] = useState("");
  const chosen = planKey || plans[0]?.plan_key || "";
  const ok = !!chosen && /^\d{1,4}$/.test(quantity) && Number(quantity) >= 1 && reason.trim().length >= 3;
  const save = useMutation({
    mutationFn: () => sendJson(tenantAddOnsPath(tenantId), "POST", {
      plan_key: chosen, quantity: Number(quantity), reason: reason.trim(),
      ends_at: ends ? new Date(`${ends}T23:59:59Z`).toISOString() : null }),
    onSuccess: () => { toast.success(t("saved")); onSaved(); onClose(); },
    onError: (e) => toast.error(t("failed", errText(e))),
  });
  return (
    <Drawer open onClose={onClose} width={480} title={t("grantTitle")}
      footer={<span style={{ display: "inline-flex", gap: 8 }}>
        <Button variant="ghost" onClick={onClose}>{t("close")}</Button>
        <Button variant="primary" disabled={!ok || save.isPending} onClick={() => save.mutate()}>{t("save")}</Button>
      </span>}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <Field label={t("fieldPlan")}>
          <Select value={chosen} onChange={(e) => setPlanKey(e.target.value)}
            options={plans.map((p) => ({ value: p.plan_key, label: `${p.name} (${money(p.price_usd_monthly, "USD")})` }))} />
        </Field>
        <Field label={t("fieldQuantity")}>
          <Input inputMode="numeric" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
        </Field>
        <Field label={t("fieldEnds")} hint={t("fieldEndsHint")}>
          <Input type="date" value={ends} onChange={(e) => setEnds(e.target.value)} />
        </Field>
        <Field label={t("reason")} required>
          <Input value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
        </Field>
      </div>
    </Drawer>
  );
}

function Billing({ tenantId, canWrite }: { tenantId: string; canWrite: boolean }) {
  const t = useT(T360_COMMERCIAL);
  const q = useQuery({ queryKey: billingDetailsKey(tenantId),
                       queryFn: () => sendJson<BillingDetails>(billingDetailsPath(tenantId), "GET") });
  const quoteQ = useQuery({ queryKey: quoteKey(tenantId), queryFn: () => sendJson<TenantQuote>(quotePath(tenantId), "GET") });
  const [editing, setEditing] = useState(false);
  const [terms, setTerms] = useState<QuoteSubscription | null>(null);
  const d = q.data;
  const rows = billingRows(d, t);
  const termCols = termColumns(t);
  return (
    <Section title={t("billingTitle")} description={t("billingHint")} testId="tenant360-billing-details"
      actions={canWrite && d ? <Button size="sm" variant="secondary" onClick={() => setEditing(true)}>{t("edit")}</Button>
        : undefined}>
      <dl style={{ ...grid, margin: 0 }}>
        {rows.map(([label, value]) => (
          <div key={label}><dt style={small}>{label}</dt><dd style={{ margin: 0, color: K.ink, overflowWrap: "anywhere" }}>
            {value}</dd></div>
        ))}
      </dl>
      <div style={{ marginTop: 14, fontSize: 13, fontWeight: 600, color: K.ink }}>{t("termsTitle")}</div>
      <DataTable<QuoteSubscription> tableId={`tenant-terms-${tenantId}`} data={quoteQ.data?.subscriptions ?? []}
        columns={termCols} getRowId={(r) => r.product} paginate={false} compact tableMinWidth={560}
        emptyTitle={t("none")} onRowClick={canWrite ? (r) => setTerms(r) : undefined} />
      {editing && d && <BillingDrawer tenantId={tenantId} details={d} onClose={() => setEditing(false)} />}
      {terms && <TermsDrawer tenantId={tenantId} sub={terms} details={d} onClose={() => setTerms(null)} />}
    </Section>
  );
}

type CT = Translator<typeof T360_COMMERCIAL.en>;

function billingRows(d: BillingDetails | undefined, t: CT): [string, string][] {
  if (!d) return [];
  const rows: [string, string | null][] = [
    [t("fieldLegal"), d.legal_entity_name], [t("fieldTaxId"), d.tax_id], [t("fieldEmail"), d.billing_email],
    [t("fieldAddress"), d.billing_address], [t("fieldCountry"), d.billing_country], [t("fieldCurrency"), d.billing_currency],
    [t("fieldFoundingFrom"), d.founding_offer_from], [t("fieldFoundingUntil"), d.founding_offer_until],
  ];
  return rows.map(([label, value]) => [label, value ?? t("none")]);
}

function termColumns(t: CT): ColumnDef<QuoteSubscription, unknown>[] {
  return [
    { accessorKey: "product", header: t("colProduct"), cell: (c) => c.row.original.plan_key },
    { id: "interval", header: t("colInterval"), cell: (c) => t(`interval_${c.row.original.billing_interval}`) },
    { id: "agreed", header: t("colAgreed"), cell: (c) => c.row.original.agreed_price_monthly == null ? t("none")
      : money(c.row.original.agreed_price_monthly, c.row.original.agreed_currency ?? "USD") },
    { id: "until", header: t("colUntil"), cell: (c) => c.row.original.agreed_until ?? t("none") },
  ];
}

function BillingDrawer({ tenantId, details, onClose }: { tenantId: string; details: BillingDetails;
                                                          onClose: () => void }) {
  const t = useT(T360_COMMERCIAL);
  const toast = useToast();
  const refresh = useRefresh(tenantId);
  const [f, setF] = useState({
    legal_entity_name: details.legal_entity_name ?? "", tax_id: details.tax_id ?? "",
    billing_email: details.billing_email ?? "", billing_address: details.billing_address ?? "",
    billing_country: details.billing_country ?? "", billing_currency: details.billing_currency,
    founding_offer_from: details.founding_offer_from ?? "", founding_offer_until: details.founding_offer_until ?? "",
  });
  const set = (k: keyof typeof f, v: string) => setF((x) => ({ ...x, [k]: v }));
  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, string | null> = {};
      for (const k of ["legal_entity_name", "tax_id", "billing_email", "billing_address", "billing_country"] as const) {
        body[k] = f[k].trim() || null;
      }
      body.billing_currency = f.billing_currency;
      if (f.founding_offer_from !== (details.founding_offer_from ?? "")
          || f.founding_offer_until !== (details.founding_offer_until ?? "")) {
        body.founding_offer_from = f.founding_offer_from || null;
        if (f.founding_offer_until) body.founding_offer_until = f.founding_offer_until;
      }
      return sendJson(billingDetailsPath(tenantId), "PUT", body);
    },
    onSuccess: () => { toast.success(t("saved")); refresh(); onClose(); },
    onError: (e) => toast.error(t("failed", errText(e))),
  });
  const text = (k: keyof typeof f, label: string, max: number) => (
    <Field label={label}><Input value={f[k]} maxLength={max} onChange={(e) => set(k, e.target.value)} /></Field>
  );
  return (
    <Drawer open onClose={onClose} width={560} title={t("billingTitle")}
      footer={<span style={{ display: "inline-flex", gap: 8 }}>
        <Button variant="ghost" onClick={onClose}>{t("close")}</Button>
        <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>{t("save")}</Button>
      </span>}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {text("legal_entity_name", t("fieldLegal"), 300)}
        {text("tax_id", t("fieldTaxId"), 64)}
        {text("billing_email", t("fieldEmail"), 254)}
        {text("billing_address", t("fieldAddress"), 1000)}
        <div style={grid}>
          {text("billing_country", t("fieldCountry"), 2)}
          <Field label={t("fieldCurrency")}>
            <Select value={f.billing_currency} onChange={(e) => set("billing_currency", e.target.value)}
              options={[{ value: "USD", label: "USD" }, { value: "VND", label: "VND" }]} />
          </Field>
        </div>
        <div style={grid}>
          <Field label={t("fieldFoundingFrom")} hint={t("fieldFoundingHint")}>
            <Input type="date" value={f.founding_offer_from} onChange={(e) => set("founding_offer_from", e.target.value)} />
          </Field>
          <Field label={t("fieldFoundingUntil")}>
            <Input type="date" value={f.founding_offer_until}
              onChange={(e) => set("founding_offer_until", e.target.value)} />
          </Field>
        </div>
      </div>
    </Drawer>
  );
}

function TermsDrawer({ tenantId, sub, details, onClose }: { tenantId: string; sub: QuoteSubscription;
                                                            details?: BillingDetails; onClose: () => void }) {
  const currency = details?.billing_currency ?? "USD";
  const t = useT(T360_COMMERCIAL);
  const toast = useToast();
  const refresh = useRefresh(tenantId);
  const [interval, setInterval_] = useState(sub.billing_interval);
  const [price, setPrice] = useState(sub.agreed_price_monthly ?? "");
  const [cur, setCur] = useState(sub.agreed_currency ?? currency);
  const [until, setUntil] = useState(sub.agreed_until ?? "");
  const send = useMutation({
    mutationFn: (lock: boolean) => sendJson(subscriptionTermsPath(tenantId, sub.product), "PUT", lock
      ? { lock: true, billing_interval: interval }
      : { billing_interval: interval, agreed_price_monthly: price.trim() || null, agreed_currency: cur,
          agreed_until: until || null }),
    onSuccess: () => { toast.success(t("saved")); refresh(); onClose(); },
    onError: (e) => toast.error(t("failed", errText(e))),
  });
  return (
    <Drawer open onClose={onClose} width={480} title={t("termsDrawerTitle", sub.plan_key)}
      footer={<span style={{ display: "inline-flex", gap: 8, flexWrap: "wrap" }}>
        <Button variant="ghost" onClick={onClose}>{t("close")}</Button>
        <Button variant="secondary" disabled={send.isPending} onClick={() => send.mutate(true)}>{t("lock")}</Button>
        <Button variant="primary" disabled={send.isPending} onClick={() => send.mutate(false)}>{t("save")}</Button>
      </span>}>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <Field label={t("fieldInterval")}>
          <Select value={interval} onChange={(e) => setInterval_(e.target.value as "month" | "year")}
            options={[{ value: "month", label: t("interval_month") }, { value: "year", label: t("interval_year") }]} />
        </Field>
        <div style={grid}>
          <Field label={t("fieldAgreed")} hint={t("fieldAgreedHint")}>
            <Input inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} />
          </Field>
          <Field label={t("fieldAgreedCurrency")}>
            <Select value={cur} onChange={(e) => setCur(e.target.value)}
              options={[{ value: "USD", label: "USD" }, { value: "VND", label: "VND" }]} />
          </Field>
        </div>
        <Field label={t("fieldAgreedUntil")}>
          <Input type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
        </Field>
        <p style={small}>{t("lockHint")}</p>
      </div>
    </Drawer>
  );
}

function Quote({ tenantId }: { tenantId: string }) {
  const t = useT(T360_COMMERCIAL);
  const q = useQuery({ queryKey: quoteKey(tenantId), queryFn: () => sendJson<TenantQuote>(quotePath(tenantId), "GET") });
  const d = q.data;
  const c = d?.currency ?? "USD";
  const cols: ColumnDef<QuoteLine, unknown>[] = [
    { id: "line", header: t("colLine"), cell: (x) => {
      const r = x.row.original;
      const tags = [r.trialing && t("trial"), r.agreed && t("agreed"), r.price_missing && t("priceMissing")]
        .filter(Boolean).join(" · ");
      return <span>{r.name}{r.quantity > 1 ? ` × ${r.quantity}` : ""}
        {tags && <span style={{ fontSize: 11, color: K.muted }}> ({tags})</span>}</span>;
    } },
    { id: "amount", header: t("colAmount"), cell: (x) => money(x.row.original.amount, c) },
    { id: "discount", header: t("colDiscount"), cell: (x) => Number(x.row.original.discount) > 0
      ? `− ${money(x.row.original.discount, c)}` : t("none") },
  ];
  const line = (label: string, value: string, strong = false) => (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, fontWeight: strong ? 600 : 400,
      color: K.ink }}><span>{label}</span><span>{value}</span></div>
  );
  return (
    <Section title={t("quoteTitle")} description={t("quoteHint")} testId="tenant360-quote">
      <DataTable<QuoteLine> tableId={`tenant-quote-${tenantId}`} data={d?.lines ?? []} columns={cols}
        getRowId={(r) => `${r.kind}:${r.plan_key}`} loading={q.isLoading} paginate={false} compact tableMinWidth={480}
        emptyTitle={t("none")} />
      {d && (
        <div style={{ display: "grid", gap: 4, marginTop: 10, maxWidth: 420, marginLeft: "auto" }}>
          {line(t("subtotal"), money(d.subtotal, c))}
          {d.discount_kind && line(t(`discount_${d.discount_kind}`, d.discount_pct), `− ${money(d.discount, c)}`)}
          {d.pilot_free && <Badge tone="info">{t("pilotFree")}</Badge>}
          {line(t("monthly"), money(d.monthly_total, c), true)}
          {line(t("annual", d.annual_paid_months), money(d.annual_total, c))}
          {d.reference && <p style={small}>{t("reference", money(d.reference.monthly_total, d.reference.currency))}</p>}
        </div>
      )}
    </Section>
  );
}
