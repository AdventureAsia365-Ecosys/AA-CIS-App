// lib/tenantCustomDomain.ts — AA-1090 TripPlanner R8: Tenant 360 › Subscription › TripPlanner own domains.
// Pure parts (types, paths, the host check the form can make alone, the status tone and whether to keep polling) so
// node --test can load them.
//
// Backend: GET / POST /admin/tenants/{id}/tripplanner-site/domains, DELETE …/domains/{domain_id}, POST
// …/domains/{domain_id}/retry (api/routers/admin_tenant_subscription.py, services/tenant_account/tripplanner_domain.py).
// The API is the guard (it also refuses platform and AWS hosts and checks the limits, which are settings); the form
// only catches a host it can see is malformed, and shows the API's refusal under the field.

export type DomainStatus = "pending" | "validating" | "attaching" | "active" | "removing" | "failed";
export type DomainTone = "neutral" | "info" | "warning" | "success" | "danger";

export interface DnsRecord { purpose: "route" | "certificate"; type: "CNAME"; name: string; value: string }

export interface CustomDomain {
  domain_id: string;
  domain: string;
  status: DomainStatus;
  dns_records: DnsRecord[];
  failure_code: string | null;
  failure_message: string | null;
  requested_at: string | null;
  validated_at: string | null;
  active_at: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

export interface DomainList {
  tenant_id: string;
  has_tripplanner: boolean;
  available: boolean;
  max_domains: number;
  domains: CustomDomain[];
}

export const domainsPath = (tenantId: string) =>
  `/api/admin/tenants/${encodeURIComponent(tenantId)}/tripplanner-site/domains`;
export const domainPath = (tenantId: string, domainId: string) =>
  `${domainsPath(tenantId)}/${encodeURIComponent(domainId)}`;
export const domainsKey = (tenantId: string) => ["tenant360", tenantId, "tp-domains"] as const;

// The statuses the job is still working on: the section polls while one is shown.
const WORKING: ReadonlySet<DomainStatus> = new Set(["pending", "validating", "attaching", "removing"]);
// Each poll is one cheap API read (the job does the AWS calls); a minute-scale job needs no faster refresh.
export const POLL_MS = 15_000;

/** A removal the job could not finish: the row stays removing with the reason, and staff can remove it again. */
export function removalStuck(d: CustomDomain): boolean {
  return d.status === "removing" && !!d.failure_code;
}

export function shouldPoll(list: DomainList | null | undefined): boolean {
  return !!list?.domains.some((d) => WORKING.has(d.status) && !removalStuck(d));
}

export function statusTone(s: DomainStatus): DomainTone {
  if (s === "active") return "success";
  if (s === "failed") return "danger";
  if (s === "validating") return "warning";
  if (s === "removing") return "neutral";
  return "info";
}

/** The host as the API stores it: trimmed, lower-case, without a pasted scheme, path or trailing dot. */
export function normaliseHost(raw: string): string {
  return raw.trim().toLowerCase().replace(/^[a-z][a-z0-9+.-]*:\/\//, "").split("/")[0].replace(/\.+$/, "");
}

const HOST_RE = /^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$/;

/** The first problem the form can see alone (the API decides the rest), or null. */
export function hostProblem(raw: string): "domain_invalid" | "domain_apex" | null {
  const host = normaliseHost(raw);
  if (!host || host.length > 253 || !HOST_RE.test(host)) return "domain_invalid";
  if (host.split(".").length < 3) return "domain_apex";
  return null;
}

/** Whether staff may add one more host: the edge is switched on, the tenant has TripPlanner and is under its limit. */
export function canAdd(list: DomainList): boolean {
  const used = list.domains.filter((d) => d.status !== "removing").length;
  return list.available && list.has_tripplanner && used < list.max_domains;
}
