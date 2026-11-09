"use client";
// app/admin/tenants/plans/PlanDrawer.tsx — AA-1028 R1 slice 3: create or edit one plan (kit Drawer). Product, key and
// tier are chosen once at create; everything else, including the feature set and its limits, is editable while the
// plan is live. A save sends the version it was made from (a stale one is refused by the API, 409 version_conflict);
// Retire asks to confirm with an optional reason. Every write is audited by the API.

import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Badge, Button, Checkbox, ConfirmModal, Drawer, Field, Input, K, Select, Switch, Textarea, mono, useT, useToast,
} from "../../../_kit";
import {
  DEFAULT_UNIT, PLANS_API, PLANS_KEY, PRODUCTS, TIERS, featuresFor, lockedLimitKeys, planBody, planDraftOf, sendJson,
  type Feature, type Plan, type PlanDraft, type PlanDraftError, type Product,
} from "@/lib/tenantPlans";
import { PLANS } from "./plans.messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45, margin: 0 } as const;
const grid = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 180px), 1fr))", gap: 12 };

// "limit", "enforcedRequired" and "limitRemoved" name their feature key: errText, below
type KeyedError = "limit" | "enforcedRequired" | "limitRemoved";
const ERR = {
  key: "err_key", name: "err_name", price: "err_price", seats: "err_seats", extraSeat: "err_extraSeat",
  notes: "err_notes", rpmRequired: "err_rpmRequired",
} as const satisfies Record<Exclude<PlanDraftError, KeyedError>, keyof typeof PLANS.en>;

export function PlanDrawer({ plan, product, catalogue, onClose }: {
  plan: Plan | null; product: Product; catalogue: Feature[]; onClose: () => void;
}) {
  const t = useT(PLANS);
  const toast = useToast();
  const qc = useQueryClient();
  const [draft, setDraft] = useState<PlanDraft>(() => planDraftOf(plan, product));
  const [retiring, setRetiring] = useState(false);
  const [apiError, setApiError] = useState("");
  const frozen = !!plan?.retired_at;
  const features = useMemo(() => featuresFor(draft.product, catalogue), [draft.product, catalogue]);
  const { body, error, field } = planBody(draft, catalogue, plan);
  const set = (p: Partial<PlanDraft>) => setDraft((d) => ({ ...d, ...p }));
  const locked = useMemo(() => lockedLimitKeys(plan, catalogue), [plan, catalogue]);
  const err = (k: PlanDraftError) => {
    if (error !== k) return undefined;
    if (k === "limit") return t("err_limit", field ?? "");
    if (k === "enforcedRequired") return t("err_enforcedRequired", field ?? "");
    if (k === "limitRemoved") return t("err_limitRemoved", field ?? "");
    return t(ERR[k]);
  };
  const featureError = err("rpmRequired") ?? err("enforcedRequired") ?? err("limitRemoved");

  const done = () => { void qc.invalidateQueries({ queryKey: PLANS_KEY }); onClose(); };
  const save = useMutation({
    mutationFn: (): Promise<Plan & { changed?: string[] }> => plan
      ? sendJson<Plan & { changed: string[] }>(`${PLANS_API}/${encodeURIComponent(plan.plan_key)}`, "PATCH", body)
      : sendJson<Plan>(PLANS_API, "POST", body),
    onSuccess: (res) => {
      if (!plan) toast.success(t("created"));
      else toast.success(res.changed?.length === 0 ? t("nothingChanged") : t("saved", res.version));
      done();
    },
    onError: (e) => setApiError(e instanceof Error ? e.message : String(e)),
  });
  const retire = useMutation({
    mutationFn: (reason: string) => sendJson<Plan>(`${PLANS_API}/${encodeURIComponent(plan!.plan_key)}/retire`, "POST",
                                                     { version: plan!.version, reason: reason || null }),
    onSuccess: () => { toast.success(t("retiredToast")); done(); },
    onError: (e) => { setRetiring(false); setApiError(e instanceof Error ? e.message : String(e)); },
  });
  const busy = save.isPending || retire.isPending;
  const lock = busy || frozen;

  function toggle(key: string, on: boolean) {
    setDraft((d) => {
      const next = { ...d.features };
      if (on) next[key] = next[key] ?? "";
      else delete next[key];
      return { ...d, features: next };
    });
  }

  return (
    <Drawer open onClose={onClose} width={600} title={plan ? t("editTitle", plan.plan_key) : t("createTitle")}
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "space-between", flexWrap: "wrap" }}>
          <span>
            {plan && !frozen && (
              <Button variant="ghost" onClick={() => setRetiring(true)} disabled={busy}>{t("retire")}</Button>
            )}
          </span>
          <span style={{ display: "inline-flex", gap: 8 }}>
            <Button variant="ghost" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
            {!frozen && (
              <Button variant="primary" disabled={!body || busy} onClick={() => { setApiError(""); save.mutate(); }}>
                {save.isPending ? t("saving") : plan ? t("save") : t("create")}
              </Button>
            )}
          </span>
        </div>
      }>
      <div data-testid="plan-drawer" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {plan && (
          <p style={small}>
            {t("versionLine", plan.version, plan.updated_by)} · {t("subscribersNote", plan.live_subscriptions)}
          </p>
        )}
        {frozen && <Badge tone="danger">{t("frozen")}</Badge>}
        {!plan && (
          <div style={grid}>
            <Field label={t("fieldProduct")}>
              <Select value={draft.product} disabled={busy}
                onChange={(e) => {
                  const p = e.target.value as Product;
                  set({ product: p, plan_key: `${p}.${draft.plan_key.split(".")[1] ?? ""}`, tier: "",
                        price_unit: DEFAULT_UNIT[p], features: {} });
                }}
                options={PRODUCTS.map((p) => ({ value: p, label: t(`product_${p}`) }))} />
            </Field>
            <Field label={t("fieldKey")} hint={t("fieldKeyHint")} error={err("key")} required>
              <Input value={draft.plan_key} disabled={busy} onChange={(e) => set({ plan_key: e.target.value.trim() })}
                data-testid="plan-key" />
            </Field>
            {draft.product === "content" && (
              <Field label={t("fieldTier")} hint={t("fieldTierHint")}>
                <Select value={draft.tier} disabled={busy} onChange={(e) => set({ tier: e.target.value })}
                  options={[{ value: "", label: t("tierNone") }, ...TIERS.map((x) => ({ value: x, label: x }))]} />
              </Field>
            )}
          </div>
        )}
        <Field label={t("fieldName")} error={err("name")} required>
          <Input value={draft.name} disabled={lock} maxLength={100} onChange={(e) => set({ name: e.target.value })}
            data-testid="plan-name" />
        </Field>
        <div style={grid}>
          <Field label={t("fieldPrice")} hint={t("fieldPriceHint")} error={err("price")}>
            <Input inputMode="decimal" value={draft.price} disabled={lock} onChange={(e) => set({ price: e.target.value })} />
          </Field>
          <Field label={t("fieldUnit")}>
            <Select value={draft.price_unit} disabled={lock}
              onChange={(e) => set({ price_unit: e.target.value as PlanDraft["price_unit"] })}
              options={(["tenant", "seat", "site"] as const).map((u) => ({ value: u, label: t(`unit_${u}`) }))} />
          </Field>
          <Field label={t("fieldSeats")} error={err("seats")}>
            <Input inputMode="numeric" value={draft.seats_included} disabled={lock}
              onChange={(e) => set({ seats_included: e.target.value })} />
          </Field>
          <Field label={t("fieldExtraSeat")} error={err("extraSeat")}>
            <Input inputMode="decimal" value={draft.price_extra_seat} disabled={lock}
              onChange={(e) => set({ price_extra_seat: e.target.value })} />
          </Field>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <Switch checked={draft.is_public} disabled={lock} label={t("fieldPublic")}
            onChange={(v) => set({ is_public: v })} />
          <span style={{ fontSize: 13, color: K.body }}>{t("fieldPublic")}</span>
        </div>
        <Field label={t("fieldNotes")} error={err("notes")}>
          <Textarea rows={2} maxLength={1000} value={draft.notes} disabled={lock}
            onChange={(e) => set({ notes: e.target.value })} />
        </Field>

        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: K.ink }}>{t("featuresTitle")}</div>
          <p style={{ ...small, marginBottom: 8 }}>{t("featuresHint")}</p>
          {featureError && <p role="alert" style={{ ...small, color: K.danger }}>{featureError}</p>}
          <div data-testid="plan-features" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {features.map((f) => {
              const on = f.feature_key in draft.features;
              const bad = (error === "limit" || error === "enforcedRequired" || error === "limitRemoved")
                && field === f.feature_key;
              const keep = on && locked.has(f.feature_key);
              return (
                <div key={f.feature_key} style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 140px",
                  gap: 10, alignItems: "center", padding: "6px 8px", borderRadius: 8,
                  background: on ? K.panel : "transparent", border: `1px solid ${bad ? K.danger : K.line}` }}>
                  <Checkbox checked={on} disabled={lock || keep} title={keep ? t("limitLocked") : undefined}
                    onChange={(e) => toggle(f.feature_key, e.target.checked)}
                    label={
                      <span style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
                        <span style={{ fontSize: 13, color: K.ink, overflowWrap: "anywhere" }}>{f.description}</span>
                        <span style={{ fontSize: 11, color: K.muted, fontFamily: mono,
                          overflowWrap: "anywhere" }}>{f.feature_key}</span>
                      </span>
                    } />
                  {f.value_kind !== "flag" ? (
                    <Input size="sm" inputMode="decimal" aria-label={f.feature_key} placeholder={t("limitPlaceholder")}
                      disabled={lock || !on} value={draft.features[f.feature_key] ?? ""} invalid={bad}
                      onChange={(e) => setDraft((d) => ({ ...d, features: { ...d.features, [f.feature_key]: e.target.value } }))} />
                  ) : <span />}
                </div>
              );
            })}
          </div>
        </div>
        {apiError && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{t("failed", apiError)}</span>}
      </div>
      {plan && (
        <ConfirmModal open={retiring} onClose={() => setRetiring(false)} onConfirm={(r) => retire.mutate(r)}
          title={t("retireTitle", plan.plan_key)} body={t("retireBody", plan.live_subscriptions)}
          confirmLabel={t("retire")} destructive busy={retire.isPending} testId="plan-retire-confirm"
          reason={{ label: t("retireReason"), maxLength: 500 }} />
      )}
    </Drawer>
  );
}
