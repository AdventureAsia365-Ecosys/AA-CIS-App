// app/admin/tenants/_components/tenantRow.ts
// AA-1039 R6 rest: the tenant row of GET /admin/tenants, shared by the Directory (../page.tsx) and the Tenant 360
// route (../[id]/page.tsx, TenantDetail.tsx). Moved out of the Directory page unchanged.

export interface Lifecycle {
  source_active: number;
  source_superseded: number;
  source_trashed: number;
  master_active: number;
  master_inactive: number;
  master_trashed: number;
}

export interface Tenant {
  tenant_id: string;
  name: string;
  slug: string;
  plan_tier: string;
  posts_per_week: number;
  country: string | null;
  rate_limit_rpm: number;
  daily_publish_limit?: number | null; // AA-819 — per-tenant override; null = code default, absent = backend not deployed
  is_active: boolean;
  status: TenantStatus;           // AA-797 — derived by GET /admin/tenants
  cancelled_at: string | null;
  created_at: string;
  plan_attached?: boolean;        // AA-798 — false: no plan, plan fields are null
  plan: { tours_quota_monthly: number | null; api_calls_quota_monthly: number | null; price_usd_monthly: number | null };
  this_month: {
    tours_rewritten: number; api_calls_used: number;
    quota_tours_pct: number; quota_calls_pct: number;
    // AA-1035: null for a role that reaches the tenant without its money (support)
    tours_overage: number; overage_usd: number | null; llm_cost_usd: number | null;
  };
  lifecycle: Lifecycle;
}

export type TenantStatus = "active" | "suspended" | "offboarded";
