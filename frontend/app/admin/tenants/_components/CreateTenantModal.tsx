"use client";
// app/admin/tenants/_components/CreateTenantModal.tsx — the New tenant dialog of Platform › Tenants.
// AA-1116 (S282 gaps G5 / G11): one request sets up the whole agency. It calls the tenant_provision saga
// (POST /api/admin/tenants/provision) instead of the old synchronous POST /api/admin/tenants: the account, its content
// subscription and the chosen add-ons (CRM, TripPlanner) in one job; a CRM add-on also creates the CRM agency and, with
// the owner's address, sends the first-owner invitation in the same request, so there is no hand step on Tenant 360 ›
// Subscription or Platform › Agencies. The content tiers come from the plan catalogue (useContentTiers), the add-on
// plans from GET /api/admin/plans (live plans only). The API re-checks everything.

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { Button, Field, Input, K, Modal, Select, SegmentedControl, useT } from "../../../_kit";
import {
  PLANS_API, PLANS_KEY, addOnChoices, crmOnboardingNote, crmOwnerField, newRequestKey, newTenantBody,
  provisionedTenantId, runSaga, sendJson, type PlansResp, type ProvisionJob,
} from "@/lib/tenantPlans";
import type { Translator } from "../../../_kit/core/i18n";
import { useContentTiers } from "./useContentTiers";
import { TEN } from "../messages";

/** The result dialog of a create / restore (AA-1036: no tenant API key; people join by invitation). */
export interface TenantNotice {
  tenant_id: string; tenant_name: string;
  title: string; note: string;
}

type Tr = Translator<typeof TEN.en>;

const autoSlug = (n: string) => n.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

/** The note under the tenant's name: the CRM agency outcome of a crm add-on follows the usual note. */
function createdNotice(job: ProvisionJob, name: string, t: Tr): TenantNotice {
  const tenantId = provisionedTenantId(job) ?? "";
  if (job.status !== "succeeded") {   // still queued / running after the wait: the worker finishes it
    return { tenant_id: tenantId, tenant_name: name, title: t("tenantSettingUp"), note: t("settingUpNote") };
  }
  const crm = crmOnboardingNote(job.crm_onboarding);
  const crmNote = crm === "done" ? t("crmReadyNote") : crm === "noInvite" ? t("crmNoInviteNote")
    : crm === "attention" ? t("crmAttentionNote", job.crm_onboarding?.error?.code ?? job.crm_onboarding?.status ?? "")
      : "";
  return { tenant_id: tenantId, tenant_name: name, title: t("tenantCreated"),
           note: crmNote ? `${t("createdNote")} ${crmNote}` : t("createdNote") };
}

function AddOnSelect({ label, value, choices, disabled, onChange, testId }: {
  label: string; value: string; choices: { plan_key: string; name: string }[]; disabled: boolean;
  onChange: (v: string) => void; testId: string;
}) {
  const t = useT(TEN);
  return (
    <Field label={label}>
      <Select value={value} disabled={disabled} data-testid={testId} onChange={(e) => onChange(e.target.value)}
        options={[{ value: "", label: t("addOnNone") },
                  ...choices.map((p) => ({ value: p.plan_key, label: `${p.name} (${p.plan_key})` }))]} />
    </Field>
  );
}

export function CreateTenantModal({ onClose, onCreated }: {
  onClose: () => void; onCreated: (k: TenantNotice) => void;
}) {
  const t = useT(TEN);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [plan, setPlan] = useState("starter");
  const { tiers } = useContentTiers(plan);
  // AA-384: posts_per_week is a free tenant choice, not implied by the plan (1-14).
  const [postsPerWeek, setPostsPerWeek] = useState("3");
  const [crmPlan, setCrmPlan] = useState("");
  const [tpPlan, setTpPlan] = useState("");
  const [ownerEmail, setOwnerEmail] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const addOnPlans = useQuery({
    queryKey: [...PLANS_KEY, "add-ons"],
    queryFn: () => sendJson<PlansResp>(PLANS_API, "GET"),
    staleTime: 60_000,
  });
  const owner = crmOwnerField(crmPlan ? "crm" : "", true, ownerEmail);

  async function submit() {
    if (!name.trim() || !slug.trim()) { setError(t("nameSlugRequired")); return; }
    const ppw = Number(postsPerWeek);
    if (!Number.isInteger(ppw) || ppw < 1 || ppw > 14) { setError(t("ppwRange")); return; }
    if (owner.bad) { setError(t("ownerEmailInvalid")); return; }
    setLoading(true); setError("");
    try {
      const job = await runSaga("/api/admin/tenants/provision", newTenantBody(newRequestKey(), {
        name, slug, plan_tier: plan, posts_per_week: ppw, crmPlan, tripplannerPlan: tpPlan, ownerEmail: owner.owner,
      }));
      if (job.status !== "succeeded" && job.status !== "queued" && job.status !== "running") {
        setError(t("setupFailed", job.error ?? job.status)); return;
      }
      onCreated(createdNotice(job, name.trim(), t));
    } catch (e) {
      setError(e instanceof Error && e.message ? e.message : t("createFailed"));
    } finally { setLoading(false); }
  }

  return (
    <Modal open onClose={loading ? () => {} : onClose} title={t("newTenant")} width={480}
      footer={
        <>
          <Button variant="secondary" disabled={loading} onClick={onClose}>{t("cancel")}</Button>
          <Button variant="primary" disabled={loading} onClick={submit} testId="create-tenant-submit">
            {loading ? <><Loader2 size={13} style={{ animation: "spin 1s linear infinite" }} /> {t("creating")}</> : t("createTenant")}
          </Button>
        </>
      }
    >
      <div data-testid="create-tenant-modal" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Field label={t("companyName")}>
          <Input value={name} disabled={loading} placeholder="WanderLux Travel"
            onChange={(e) => { setName(e.target.value); setSlug(autoSlug(e.target.value)); }} />
        </Field>
        <Field label={t("slug")}>
          <Input value={slug} disabled={loading} placeholder="wanderlux-travel" onChange={(e) => setSlug(e.target.value)} />
        </Field>
        <Field label={t("plan")}>
          <SegmentedControl label={t("plan")} value={plan} onChange={setPlan}
            options={tiers.map((p) => ({ value: p, label: p }))} />
        </Field>
        <Field label={t("ppwLabel")} hint={t("ppwHelp")}>
          <Input type="number" min={1} max={14} value={postsPerWeek} disabled={loading}
            onChange={(e) => setPostsPerWeek(e.target.value)} style={{ width: 100 }} />
        </Field>
        <AddOnSelect label={t("addOnCrm")} value={crmPlan} disabled={loading} testId="create-tenant-crm"
          choices={addOnChoices(addOnPlans.data?.plans, "crm")} onChange={setCrmPlan} />
        {crmPlan && (
          <Field label={t("ownerEmailLabel")} hint={t("ownerEmailHint")}
            error={owner.bad ? t("ownerEmailInvalid") : undefined}>
            <Input type="email" autoComplete="off" value={ownerEmail} disabled={loading}
              onChange={(e) => setOwnerEmail(e.target.value)} />
          </Field>
        )}
        <AddOnSelect label={t("addOnTripplanner")} value={tpPlan} disabled={loading} testId="create-tenant-tripplanner"
          choices={addOnChoices(addOnPlans.data?.plans, "tripplanner")} onChange={setTpPlan} />
        {error && (
          <div role="alert" style={{ padding: "9px 12px", background: K.dangerSoft, border: `1px solid ${K.dangerBorder}`,
            borderRadius: 8, fontSize: 12, color: K.danger }}>
            {error}
          </div>
        )}
      </div>
    </Modal>
  );
}
