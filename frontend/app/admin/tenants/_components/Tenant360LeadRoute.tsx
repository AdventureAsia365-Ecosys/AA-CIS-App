"use client";
// app/admin/tenants/_components/Tenant360LeadRoute.tsx — AA-1038 R5 slice 3 follow-up (S272): Tenant 360 ›
// Subscription › TripPlanner lead route. Staff only; a tenant never sees it.
//
// GET /api/admin/tenants/{id}/tripplanner-site/lead-route: the stored route (CIS 390 columns of
// shared.tenant_tripplanner_site). "Edit" (admin: tenant.write) opens a kit Drawer that PUTs the channel and its
// targets; the API refuses a webhook URL that is not https or names a private / internal host (422 with the field),
// and the refusal is shown under that field. A tenant with the CRM add-on always gets its leads in the CRM (the
// catalog view resolves the channel), so the section says so. Kit only (Section, Drawer, Field, Select, Input, Badge,
// Toast).

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge, Button, DateText, Drawer, ErrorState, Field, Input, K, Section, Select, mono, useT, useToast,
} from "../../../_kit";
import { PlanApiError, sendJson } from "@/lib/tenantPlans";
import {
  LEAD_CHANNELS, fieldOfCode, leadRouteBody, leadRouteDraftOf, leadRouteKey, leadRoutePath, sameLeadRoute,
  type LeadChannel, type LeadRouteDraft, type LeadRouteField, type LeadRouteResp,
} from "@/lib/tenantLeadRoute";
import { T360_LEAD } from "./leadRoute.messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.45, margin: 0 } as const;
type Msg = keyof typeof T360_LEAD.en;

function errKey(code: string): Msg | null {
  const k = `err_${code}`;
  return k in T360_LEAD.en ? (k as Msg) : null;
}

function LeadRouteDrawer({ data, onClose }: { data: LeadRouteResp; onClose: () => void }) {
  const t = useT(T360_LEAD);
  const toast = useToast();
  const qc = useQueryClient();
  const start = useMemo(() => leadRouteDraftOf(data), [data]);
  const [d, setD] = useState<LeadRouteDraft>(start);
  const [shownProblem, setShownProblem] = useState(false);
  const [apiField, setApiField] = useState<{ field: LeadRouteField; text: string } | null>(null);
  const [apiError, setApiError] = useState("");
  const { body, problem } = leadRouteBody(d);
  const set = (p: Partial<LeadRouteDraft>) => { setD((x) => ({ ...x, ...p })); setApiField(null); setApiError(""); };
  const save = useMutation({
    mutationFn: () => sendJson<LeadRouteResp>(leadRoutePath(data.tenant_id), "PUT", body),
    onSuccess: () => {
      toast.success(t("saved"));
      void qc.invalidateQueries({ queryKey: leadRouteKey(data.tenant_id) });
      onClose();
    },
    onError: (e) => {
      const code = e instanceof PlanApiError ? e.code : "";
      const field = fieldOfCode(code);
      const key = errKey(code);
      const text = key ? (t(key) as string) : e instanceof Error ? e.message : String(e);
      if (field) setApiField({ field, text });
      else setApiError(e instanceof PlanApiError && e.status === 403 ? t("readOnly") : t("failed", text));
    },
  });
  // a local problem shows once the user tried to save, or as soon as a typed URL lacks https://
  const local = problem && (shownProblem || problem.code === "lead_webhook_url_not_https") ? problem : null;
  const errorOf = (f: LeadRouteField): string | undefined => {
    if (apiField?.field === f) return apiField.text;
    if (local?.field === f) { const k = errKey(local.code); return k ? (t(k) as string) : local.code; }
    return undefined;
  };
  const changed = !data.site || !sameLeadRoute(d, start);

  return (
    <Drawer open onClose={onClose} width={520} title={t("editTitle")}
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <Button variant="ghost" onClick={onClose} disabled={save.isPending}>{t("cancel")}</Button>
          <span data-testid="lead-route-save" style={{ display: "inline-flex" }}>
            <Button variant="primary" disabled={!changed || save.isPending}
              onClick={() => { setShownProblem(true); if (body) save.mutate(); }}>
              {save.isPending ? t("saving") : t("save")}
            </Button>
          </span>
        </div>
      }>
      <div data-testid="lead-route-drawer" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <p style={small}>{t("hint")}</p>
        <Field label={t("fieldChannel")} error={errorOf("lead_channel")}>
          <Select value={d.channel} disabled={save.isPending}
            onChange={(e) => set({ channel: e.target.value as LeadChannel })}
            options={LEAD_CHANNELS.map((c) => ({ value: c, label: t(`channel_${c}`) }))} />
        </Field>
        <Field label={t("fieldUrl")} hint={t("fieldUrlHint")} required={d.channel === "webhook"}
          error={errorOf("lead_webhook_url")}>
          <Input type="url" inputMode="url" value={d.url} placeholder="https://" disabled={save.isPending}
            maxLength={500} onChange={(e) => set({ url: e.target.value })} data-testid="lead-route-url" />
        </Field>
        <Field label={t("fieldEmail")} hint={t("fieldEmailHint")} required={d.channel === "email"}
          error={errorOf("lead_email")}>
          <Input type="email" value={d.email} disabled={save.isPending} maxLength={254}
            onChange={(e) => set({ email: e.target.value })} data-testid="lead-route-email" />
        </Field>
        {apiError && <span role="alert" style={{ fontSize: 12, color: K.danger }}>{apiError}</span>}
      </div>
    </Drawer>
  );
}

/** The TripPlanner lead route section of the Subscription tab (mounted inside its ToastProvider). */
export function Tenant360LeadRoute({ tenantId, crmCore, canEdit }: {
  tenantId: string; crmCore: boolean; canEdit: boolean;
}) {
  const t = useT(T360_LEAD);
  const [editing, setEditing] = useState(false);
  const q = useQuery({
    queryKey: leadRouteKey(tenantId),
    queryFn: () => sendJson<LeadRouteResp>(leadRoutePath(tenantId), "GET"),
    retry: false,
  });
  const data = q.data;
  if (data && !data.has_tripplanner && !data.site) return null;
  const target = data?.lead_channel === "webhook" ? data.lead_webhook_url
    : data?.lead_channel === "email" ? data.lead_email : null;

  return (
    <Section title={t("title")} description={t("hint")} testId="tenant-lead-route"
      actions={canEdit && data ? (
        <span data-testid="lead-route-edit" style={{ display: "inline-flex" }}>
          <Button size="sm" variant="secondary" onClick={() => setEditing(true)}>
            {data.site ? t("edit") : t("set")}
          </Button>
        </span>
      ) : undefined}>
      {q.isPending && <p style={small}>{t("loading")}</p>}
      {q.error && <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`}
        onRetry={() => void q.refetch()} />}
      {data && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, minWidth: 0 }}>
          {crmCore && <p style={{ ...small, color: K.amber }}>{t("crmNote")}</p>}
          {!data.has_tripplanner && <p style={small}>{t("noTripPlanner")}</p>}
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", fontSize: 13, minWidth: 0 }}>
            <span style={{ color: K.muted }}>{t("current")}</span>
            <Badge tone={data.lead_channel === "none" ? "neutral" : "success"} dot={false}>
              {t(`channel_${data.lead_channel}`)}
            </Badge>
            {target && (
              <span style={{ fontFamily: mono, fontSize: 12, color: K.ink, overflowWrap: "anywhere", minWidth: 0 }}>
                {target}
              </span>
            )}
            {data.updated_at && <span style={small}><DateText value={data.updated_at} relative /></span>}
          </div>
        </div>
      )}
      {editing && data && <LeadRouteDrawer data={data} onClose={() => setEditing(false)} />}
    </Section>
  );
}
