// lib/tenantLeadRoute.ts — AA-1038 R5 slice 3 follow-up (S272): Tenant 360 › Subscription › TripPlanner lead route.
// Pure parts (types, draft, the checks the form can make alone, the field of an API refusal) so node --test can load
// them.
//
// Backend: GET / PUT /admin/tenants/{id}/tripplanner-site/lead-route (api/routers/admin_tenant_subscription.py,
// services/tenant_account/tripplanner_site.py). The API is the guard: it refuses a webhook URL that is not https or
// names a private / internal host (422 {detail: {detail: code, field, message}}); the form only catches what it can
// see without a rule copy (a missing target, a URL without https://), and shows the API's refusal under its field.

export type LeadChannel = "none" | "webhook" | "email";
export const LEAD_CHANNELS: LeadChannel[] = ["none", "webhook", "email"];
export type LeadRouteField = "lead_channel" | "lead_webhook_url" | "lead_email";

export interface LeadRouteResp {
  tenant_id: string;
  has_tripplanner: boolean;
  site: boolean;
  lead_channel: LeadChannel;
  lead_webhook_url: string | null;
  lead_email: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

export interface LeadRouteDraft { channel: LeadChannel; url: string; email: string }
export interface LeadRouteBody { lead_channel: LeadChannel; lead_webhook_url: string | null; lead_email: string | null }
export interface LeadRouteProblem { field: LeadRouteField; code: string }

export const WEBHOOK_URL_MAX = 500;
export const EMAIL_MAX = 254;

export const leadRoutePath = (tenantId: string) =>
  `/api/admin/tenants/${encodeURIComponent(tenantId)}/tripplanner-site/lead-route`;
export const leadRouteKey = (tenantId: string) => ["tenant360", tenantId, "lead-route"] as const;

export function leadRouteDraftOf(r: LeadRouteResp | null | undefined): LeadRouteDraft {
  return { channel: r?.lead_channel ?? "none", url: r?.lead_webhook_url ?? "", email: r?.lead_email ?? "" };
}

/** The body to PUT, or the first problem the form can see on its own. A target typed for another channel is sent
 *  too (stored for later; the API checks it as well). */
export function leadRouteBody(d: LeadRouteDraft): { body: LeadRouteBody | null; problem: LeadRouteProblem | null } {
  const url = d.url.trim();
  const email = d.email.trim();
  let problem: LeadRouteProblem | null = null;
  if (url && url.length > WEBHOOK_URL_MAX) problem = { field: "lead_webhook_url", code: "lead_webhook_url_too_long" };
  else if (url && !url.startsWith("https://")) problem = { field: "lead_webhook_url", code: "lead_webhook_url_not_https" };
  else if (email && (email.length > EMAIL_MAX || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email))) {
    problem = { field: "lead_email", code: "lead_email_invalid" };
  } else if (d.channel === "webhook" && !url) problem = { field: "lead_webhook_url", code: "lead_webhook_url_required" };
  else if (d.channel === "email" && !email) problem = { field: "lead_email", code: "lead_email_required" };
  if (problem) return { body: null, problem };
  return { body: { lead_channel: d.channel, lead_webhook_url: url || null, lead_email: email || null }, problem: null };
}

/** The form field an API refusal code is about (null: show it below the form). */
export function fieldOfCode(code: string | null | undefined): LeadRouteField | null {
  if (!code) return null;
  if (code.startsWith("lead_webhook_url_")) return "lead_webhook_url";
  if (code.startsWith("lead_email_")) return "lead_email";
  if (code.startsWith("lead_channel_")) return "lead_channel";
  return null;
}

export function sameLeadRoute(a: LeadRouteDraft, b: LeadRouteDraft): boolean {
  return a.channel === b.channel && a.url.trim() === b.url.trim() && a.email.trim() === b.email.trim();
}
