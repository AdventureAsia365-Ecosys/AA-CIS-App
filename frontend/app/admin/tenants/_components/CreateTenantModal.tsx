"use client";
// app/admin/tenants/_components/CreateTenantModal.tsx — the New tenant dialog of Platform › Tenants.
// AA-1116 (S282 gaps G5 / G11): one request sets up the whole agency. It calls the tenant_provision saga
// (POST /api/admin/tenants/provision) instead of the old synchronous POST /api/admin/tenants: the account, its content
// subscription and the chosen add-ons (CRM, TripPlanner) in one job; a CRM add-on also creates the CRM agency and, with
// the owner's address, sends the first-owner invitation in the same request, so there is no hand step on Tenant 360 ›
// Subscription or Platform › Agencies. The content tiers come from the plan catalogue (useContentTiers), the add-on
// plans from GET /api/admin/plans (live plans only). The API re-checks everything. The result dialog lists the saga's
// outcome line by line (OutcomeList: tenant, content subscription, each add-on, the CRM agency and owner invitation).

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle, Loader2, MinusCircle } from "lucide-react";
import { Button, Field, Input, K, Modal, Select, SegmentedControl, useT } from "../../../_kit";
import {
  PLANS_API, PLANS_KEY, addOnChoices, crmOnboardingNote, crmOwnerField, newRequestKey, newTenantBody,
  provisionOutcome, provisionedTenantId, runSaga, sendJson, type OutcomeRow, type PlansResp, type ProvisionJob,
} from "@/lib/tenantPlans";
import type { Translator } from "../../../_kit/core/i18n";
import { useContentTiers } from "./useContentTiers";
import { TEN } from "../messages";

/** The result dialog of a create / restore (AA-1036: no tenant API key; people join by invitation). */
export interface TenantNotice {
  tenant_id: string; tenant_name: string;
  title: string; note: string;
  /** AA-1116: the New tenant saga's outcome, one row per thing it set up (absent for a restore). */
  outcome?: OutcomeRow[];
}

type Tr = Translator<typeof TEN.en>;

const autoSlug = (n: string) => n.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

/** The CRM agency outcome of a crm add-on, as one sentence ("" without a crm add-on). */
function crmNoteText(job: ProvisionJob, t: Tr): string {
  const crm = crmOnboardingNote(job.crm_onboarding);
  if (crm === "done") return t("crmReadyNote");
  if (crm === "noInvite") return t("crmNoInviteNote");
  if (crm === "attention") return t("crmAttentionNote", job.crm_onboarding?.error?.code ?? job.crm_onboarding?.status ?? "");
  return "";
}

/** The result dialog: the CRM agency outcome of a crm add-on follows the usual note. */
function createdNotice(job: ProvisionJob, name: string, products: ("crm" | "tripplanner")[], t: Tr): TenantNotice {
  const tenantId = provisionedTenantId(job) ?? "";
  const outcome = provisionOutcome(job, products);
  if (job.status !== "succeeded") {   // still queued / running after the wait: the worker finishes it
    return { tenant_id: tenantId, tenant_name: name, title: t("tenantSettingUp"), note: t("settingUpNote"), outcome };
  }
  const crmNote = crmNoteText(job, t);
  return { tenant_id: tenantId, tenant_name: name, title: t("tenantCreated"),
           note: crmNote ? `${t("createdNote")} ${crmNote}` : t("createdNote"), outcome };
}

const OUTCOME_TONE = {
  done: { Icon: CheckCircle, color: K.success },
  pending: { Icon: Loader2, color: K.muted },
  skipped: { Icon: MinusCircle, color: K.muted },
  attention: { Icon: AlertTriangle, color: K.amber },
} as const;

/** AA-1116: the saga's outcome in the result dialog, one line per thing set up, with its state and detail. */
export function OutcomeList({ rows }: { rows: OutcomeRow[] }) {
  const t = useT(TEN);
  if (!rows.length) return null;
  const detail = (r: OutcomeRow) => {
    if (!r.detail) return "";
    if (/^\d+$/.test(r.detail)) return t("outcomeSeats", r.detail);
    switch (r.detail) {
      case "trialing": return t("outcomeDetail_trialing");
      case "active": return t("outcomeDetail_active");
      case "invited": return t("outcomeDetail_invited");
      case "invite_pending": return t("outcomeDetail_invite_pending");
      case "owner_present": return t("outcomeDetail_owner_present");
      default: return r.detail;   // a CRM / saga error code, shown as sent
    }
  };
  return (
    <div data-testid="tenant-outcome" style={{ marginTop: 12 }}>
      <div style={{ fontSize: 12, fontWeight: 600, color: K.ink, marginBottom: 6 }}>{t("outcomeTitle")}</div>
      <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 6 }}>
        {rows.map((r) => {
          const { Icon, color } = OUTCOME_TONE[r.state];
          const d = detail(r);
          return (
            <li key={r.item} data-testid={`tenant-outcome-${r.item}`} data-state={r.state}
              style={{ display: "flex", alignItems: "flex-start", gap: 8, fontSize: 12.5, color: K.muted, minWidth: 0 }}>
              <Icon size={14} color={color} aria-hidden="true" style={{ flexShrink: 0, marginTop: 2 }} />
              <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>
                <strong style={{ color: K.ink, fontWeight: 500 }}>{t(`outcome_${r.item}`)}</strong>
                {" · "}{t(`outcomeState_${r.state}`)}{d ? ` (${d})` : ""}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** The form's first problem as a message, or "" when it can be sent. */
function formProblem(name: string, slug: string, postsPerWeek: string, ownerBad: boolean, t: Tr): string {
  if (!name.trim() || !slug.trim()) return t("nameSlugRequired");
  const ppw = Number(postsPerWeek);
  if (!Number.isInteger(ppw) || ppw < 1 || ppw > 14) return t("ppwRange");
  return ownerBad ? t("ownerEmailInvalid") : "";
}

const SAGA_WAITING = new Set(["succeeded", "queued", "running"]);

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
    const problem = formProblem(name, slug, postsPerWeek, owner.bad, t);
    if (problem) { setError(problem); return; }
    setLoading(true); setError("");
    try {
      const job = await runSaga("/api/admin/tenants/provision", newTenantBody(newRequestKey(), {
        name, slug, plan_tier: plan, posts_per_week: Number(postsPerWeek), crmPlan, tripplannerPlan: tpPlan,
        ownerEmail: owner.owner,
      }));
      const products = [...(crmPlan ? ["crm" as const] : []), ...(tpPlan ? ["tripplanner" as const] : [])];
      if (SAGA_WAITING.has(job.status)) onCreated(createdNotice(job, name.trim(), products, t));
      else setError(t("setupFailed", job.error ?? job.status));
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
