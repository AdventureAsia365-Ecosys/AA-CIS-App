"use client";
// app/admin/tenants/page.tsx — AA-159: lifecycle stats + country + count fix
// AA-473: Gate A removed entirely (ADR-2026-038 §0.2) — a new tenant is is_active=true
// immediately, no onboarding approval step left. The "Onboarding" tab and its pending-tenant
// UI (added AA-389, trimmed AA-472) are gone.
// AA-797: three states (active / suspended / offboarded). Suspend <-> active is the status
// toggle (PATCH body); Offboard (reason required) revokes the key and replaces the old unaudited
// Delete; Restore (tech lead decision, 14/10/2026) brings an offboarded tenant back with its data and settings
// and a new key.
// AA-766 (S236): Tenant 360 — Settings editors (plan, RPM, markets, channels, posts/week), and new
// Cost, Quality (per T10 gate + edit rate) and Markets tabs, in ./_components (admin-only data).
// AA-994: text in the UI language (messages.ts, _components/tenant360.messages.ts); values from the API stay as sent.
// AA-1031 (S264): Members tab (Content studio memberships: invite, send again, change role, remove), ./_components.
// AA-1028 R1 slice 3: Subscription tab (subscriptions, entitlements, overrides, add-ons); the create modal's tier choices
// come from the plan catalogue (useContentTiers), not a hard-coded list.
// AA-1035 (directory capability UI): the write controls follow the role (lib/tenantDirectory.ts): New tenant and
// Offboard / Restore need tenant.write, the status pill toggles for tenant.write or billing.write, and the
// unmapped-markets banner (tenant.read) is not even fetched for the desk roles. The backend stays the authority.

import { Suspense, useState, useEffect, useCallback } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  Plus, RefreshCw, ChevronRight,
  AlertCircle, Loader2, CheckCircle, Ban, Globe, RotateCcw,
} from "lucide-react";
import {
  Button, PageFrame, Modal, PageHeader, useT, Input, Textarea,
  K, serif, mono, sans, Section, Eyebrow, Badge, LoadingScreen,
  DataTable, SegmentedControl, type ColumnDef,
  type Tone,
  ButtonGroup, ChipButton, Pressable, useAdminIdentity,
} from "../../_kit";
import { legacyTenantLinkTarget, tenant360Href } from "@/lib/adminIa";
import { tenantDirectoryControls, type TenantDirectoryControls } from "@/lib/tenantDirectory";
import { useContentTiers } from "./_components/useContentTiers";
import type { Lifecycle, Tenant, TenantStatus } from "./_components/tenantRow";
import { TEN } from "./messages";

// ─── Types ────────────────────────────────────────────────────────────────────

type StatusFilter = "all" | TenantStatus;
const TENANT_STATUS_STYLE: Record<
  TenantStatus, { label: "statusActive" | "statusSuspended" | "statusOffboarded"; tone: Tone }
> = {
  active:     { label: "statusActive",     tone: "success" },
  suspended:  { label: "statusSuspended",  tone: "warning" },
  offboarded: { label: "statusOffboarded", tone: "danger" },
};

// AA-1036 R3 slice 3 (root ADR 0019 d6): no tenant API key is issued, rotated or shown any more; create and restore
// end in this small result dialog instead of the old one-time key dialog.
interface TenantNotice {
  tenant_id: string; tenant_name: string;
  title: string; note: string;
}

// AA-629 Tier 2 — GET /admin/unmapped-market-requests
interface UnmappedMarketRequest {
  country_code: string; tenant_count: number;
  first_seen_at: string | null; last_requested_at: string | null; tenant_ids: string[];
}

const PLAN_BADGE: Record<string, Tone> = {
  starter: "info", growth: "purple", business: "success", internal: "accent",
};

// ─── Lifecycle Bar ────────────────────────────────────────────────────────────

function StatPill({ n, label, color }: { n: number; label: string; color: string }) {
  return (
    <span title={label} style={{
      display: "inline-flex", alignItems: "center", gap: 3,
      fontSize: 11, fontWeight: n > 0 ? 600 : 400,
      color: n > 0 ? color : K.muted2,
    }}>
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: n > 0 ? color : K.line, flexShrink: 0 }} />
      {n}
    </span>
  );
}


function LifecycleBar({ lc }: { lc: Lifecycle }) {
  const t = useT(TEN);
  const sourceTotal = lc.source_active + lc.source_superseded + lc.source_trashed;
  const masterTotal = lc.master_active + lc.master_inactive + lc.master_trashed;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      {/* Source row */}
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{ fontSize: 10, color: K.muted2, width: 42, flexShrink: 0 }}>{t("source")}</span>
        <div style={{ display: "flex", gap: 8 }}>
          <StatPill n={lc.source_active}     label={t("active")}     color={K.success} />
          <StatPill n={lc.source_superseded} label={t("superseded")} color={K.accent} />
          <StatPill n={lc.source_trashed}    label={t("trashed")}    color={K.danger} />
        </div>
        {sourceTotal > 0 && (
          <span style={{ fontSize: 10, color: K.muted2, marginLeft: "auto" }}>{t("total", sourceTotal)}</span>
        )}
      </div>
      {/* Master row */}
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{ fontSize: 10, color: K.muted2, width: 42, flexShrink: 0 }}>{t("master")}</span>
        <div style={{ display: "flex", gap: 8 }}>
          <StatPill n={lc.master_active}   label={t("active")}   color={K.success} />
          <StatPill n={lc.master_inactive} label={t("inactive")} color={K.muted} />
          <StatPill n={lc.master_trashed}  label={t("trashed")}  color={K.danger} />
        </div>
        {masterTotal > 0 && (
          <span style={{ fontSize: 10, color: K.muted2, marginLeft: "auto" }}>{t("total", masterTotal)}</span>
        )}
      </div>
    </div>
  );
}

// ─── Create Tenant Modal ──────────────────────────────────────────────────────

function CreateModal({ onClose, onCreated }: { onClose: () => void; onCreated: (k: TenantNotice) => void }) {
  const t = useT(TEN);
  const [name, setName]   = useState("");
  const [slug, setSlug]   = useState("");
  const [plan, setPlan]   = useState("starter");
  const { tiers } = useContentTiers(plan);
  // AA-384: posts_per_week is a free tenant choice now, not implied by plan — caller states its
  // own posting cadence at creation (1-14, no default that matches a tier).
  const [postsPerWeek, setPostsPerWeek] = useState("3");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const autoSlug = (n: string) => n.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

  async function submit() {
    if (!name.trim() || !slug.trim()) { setError(t("nameSlugRequired")); return; }
    const ppw = Number(postsPerWeek);
    if (!Number.isInteger(ppw) || ppw < 1 || ppw > 14) {
      setError(t("ppwRange")); return;
    }
    setLoading(true); setError("");
    try {
      const res = await fetch("/api/admin/tenants", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), slug: slug.trim(), plan_tier: plan, posts_per_week: ppw }),
      });
      const data = await res.json();
      if (!res.ok) { setError(data.detail ?? t("createFailed")); return; }
      onCreated({ tenant_id: data.tenant_id, tenant_name: data.name, title: t("tenantCreated"), note: t("createdNote") });
    } catch { setError(t("connectionError")); } finally { setLoading(false); }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={t("newTenant")}
      width={440}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>{t("cancel")}</Button>
          <Button variant="primary" disabled={loading} onClick={submit}>
            {loading ? <><Loader2 size={13} style={{ animation: "spin 1s linear infinite" }} /> {t("creating")}</> : t("createTenant")}
          </Button>
        </>
      }
    >
        {[
          { key: "name", label: t("companyName"), value: name, onChange: (v: string) => { setName(v); setSlug(autoSlug(v)); }, placeholder: "WanderLux Travel" },
          { key: "slug", label: t("slug"),        value: slug, onChange: setSlug, placeholder: "wanderlux-travel" },
        ].map(f => (
          <div key={f.key} style={{ marginBottom: 16 }}>
            <label style={{ fontSize: 11, fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.1em", display: "block", marginBottom: 6 }}>{f.label}</label>
            <Input value={f.value} onChange={e => f.onChange(e.target.value)} placeholder={f.placeholder}
              aria-label={f.label} style={{ width: "100%" }} />
          </div>
        ))}
        <div style={{ marginBottom: 24 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.1em", display: "block", marginBottom: 8 }}>{t("plan")}</label>
          <SegmentedControl
            label={t("plan")}
            value={plan}
            onChange={setPlan}
            options={tiers.map(p => ({ value: p, label: p }))}
          />
        </div>
        <div style={{ marginBottom: 24 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: K.muted, textTransform: "uppercase", letterSpacing: "0.1em", display: "block", marginBottom: 8 }}>
            {t("ppwLabel")}
          </label>
          <Input type="number" min={1} max={14} value={postsPerWeek} onChange={e => setPostsPerWeek(e.target.value)}
            aria-label={t("ppwLabel")} style={{ width: 100 }} />
          <p style={{ fontSize: 11, color: K.muted2, margin: "6px 0 0" }}>
            {t("ppwHelp")}
          </p>
        </div>
        {error && (
          <div style={{ marginBottom: 14, padding: "9px 12px", background: K.dangerSoft, border: `1px solid ${K.dangerBorder}`, borderRadius: 8, fontSize: 12, color: K.danger }}>
            {error}
          </div>
        )}
    </Modal>
  );
}

// ─── Result dialog (create / restore) ─────────────────────────────────────────

function TenantNoticeModal({ notice, onClose }: { notice: TenantNotice; onClose: () => void }) {
  const t = useT(TEN);
  return (
    <Modal
      open
      onClose={onClose}
      width={460}
      ariaLabel={notice.title}
      testId="tenant-notice-modal"
      footer={<Button variant="primary" onClick={onClose}>{t("done")}</Button>}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8, fontFamily: serif, fontSize: 17,
        fontWeight: 600, color: K.success }}>
        <CheckCircle size={20} color={K.success} />
        {notice.title}
      </div>
      <p style={{ fontSize: 13, color: K.muted, margin: 0 }}>
        <strong style={{ color: K.ink }}>{notice.tenant_name}</strong> {notice.note}
      </p>
    </Modal>
  );
}


// ─── Tenant Row ───────────────────────────────────────────────────────────────

async function errorDetail(res: Response, fallback: string): Promise<string> {
  const body = await res.json().catch(() => ({}));
  return typeof body?.detail === "string" ? body.detail : fallback;
}

// AA-797 — Offboard needs a reason (offboarding audit trail): the account becomes offboarding (no studio sign-in)
// and queued jobs are cancelled. Data and settings are kept, so Restore can bring the tenant back.
function OffboardModal({ tenant, onClose, onDone }: {
  tenant: Tenant; onClose: () => void; onDone: () => void;
}) {
  const t = useT(TEN);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit() {
    if (!reason.trim()) { setError(t("reasonRequired")); return; }
    setBusy(true); setError("");
    try {
      const res = await fetch(`/api/admin/tenants/${tenant.tenant_id}/offboard`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: reason.trim() }),
      });
      if (!res.ok) { setError(await errorDetail(res, t("offboardFailed"))); return; }
      onDone();
    } catch { setError(t("connectionError")); } finally { setBusy(false); }
  }
  return (
    <Modal open onClose={busy ? () => {} : onClose} title={t("offboardTitle", tenant.name)}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
          <Button variant="danger" onClick={submit} disabled={busy}>
            {busy && <Loader2 size={13} style={{ animation: "spin 1s linear infinite" }} />} {t("offboard")}
          </Button>
        </>
      }>
      <p style={{ margin: "0 0 12px", fontSize: 13, color: K.muted }}>
        {t("offboardBody")}
      </p>
      <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: K.ink, marginBottom: 6 }}>{t("reasonLabel")}</label>
      <Textarea value={reason} onChange={e => setReason(e.target.value)} rows={3}
        style={{ width: "100%", boxSizing: "border-box", padding: 10, borderRadius: 8, border: `1px solid ${K.line}`,
          background: K.bg, color: K.ink, fontSize: 13, fontFamily: "inherit", resize: "vertical" }} />
      {error && (
        <div style={{ marginTop: 10, fontSize: 12, color: K.danger, display: "flex", alignItems: "center", gap: 6 }}>
          <AlertCircle size={13} /> {error}
        </div>
      )}
    </Modal>
  );
}

interface RestoreResult {
  tenant_id: string; tenant_name: string;
  cancelled_jobs: { id: string; kind: string }[];
}

// AA-797 — Restore (tech lead decision, 14/10/2026): offboarded -> active with the same plan, brand rules,
// config and content; jobs the offboard cancelled are not re-run. AA-1036: no API key is issued.
function RestoreModal({ tenant, onClose, onDone }: {
  tenant: Tenant; onClose: () => void; onDone: (r: RestoreResult) => void;
}) {
  const t = useT(TEN);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit() {
    setBusy(true); setError("");
    try {
      const res = await fetch(`/api/admin/tenants/${tenant.tenant_id}/restore`, { method: "POST" });
      if (!res.ok) { setError(await errorDetail(res, t("restoreFailed"))); return; }
      onDone(await res.json());
    } catch { setError(t("connectionError")); } finally { setBusy(false); }
  }
  return (
    <Modal open onClose={busy ? () => {} : onClose} title={t("restoreTitle", tenant.name)}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
          <Button variant="primary" onClick={submit} disabled={busy}>
            {busy && <Loader2 size={13} style={{ animation: "spin 1s linear infinite" }} />} {t("restore")}
          </Button>
        </>
      }>
      <p style={{ margin: "0 0 8px", fontSize: 13, color: K.muted }}>
        {t("restoreBody")}
      </p>
      <ul style={{ margin: "0 0 4px", paddingLeft: 18, fontSize: 13, color: K.muted }}>
        <li>{t("restoreSignIn")}</li>
        <li>{t("restoreJobsA")} <strong style={{ color: K.ink }}>{t("restoreJobsB")}</strong>{t("restoreJobsC")}</li>
      </ul>
      {error && (
        <div style={{ marginTop: 10, fontSize: 12, color: K.danger, display: "flex", alignItems: "center", gap: 6 }}>
          <AlertCircle size={13} /> {error}
        </div>
      )}
    </Modal>
  );
}

// AA-1006: the tenant list is a kit DataTable (a card per tenant on a phone); the status toggle and the row actions are
// small components that keep their own state (busy, error, the offboard / restore dialogs), and Detail opens the
// tenant at its Tenant 360 route (AA-1039 R6 rest; a kit Drawer before, a table row expanded under the tenant first).
function tenantStatus(tenant: Tenant): TenantStatus {
  return tenant.status ?? (tenant.is_active ? "active" : "suspended");
}

function tenantLifecycle(tenant: Tenant): Lifecycle {
  return tenant.lifecycle ?? {
    source_active: 0, source_superseded: 0, source_trashed: 0,
    master_active: 0, master_inactive: 0, master_trashed: 0,
  };
}

/** Status pill that toggles active <-> suspended (AA-797; offboarded is read-only). A role that may not suspend or
 *  resume (AA-1035: neither tenant.write nor billing.write) gets the pill as read-only text. */
function TenantStatusToggle({ tenant, onChanged, canToggle }: {
  tenant: Tenant; onChanged: () => void; canToggle: boolean;
}) {
  const t = useT(TEN);
  const [toggling, setToggling] = useState(false);
  const [rowError, setRowError] = useState("");
  const status = tenantStatus(tenant);
  const offboarded = status === "offboarded";
  const chip = TENANT_STATUS_STYLE[status];

  async function toggle() {
    if (offboarded) return;
    setToggling(true); setRowError("");
    try {
      const res = await fetch(`/api/admin/tenants/${tenant.tenant_id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_active: status !== "active" }),
      });
      if (!res.ok) { setRowError(await errorDetail(res, t("statusUpdateFailed"))); return; }
      onChanged();
    } catch { setRowError(t("connectionError")); } finally { setToggling(false); }
  }

  if (!canToggle) return <Badge tone={chip.tone} dot={false}>{t(chip.label)}</Badge>;
  const toggleTitle = offboarded ? t("offboardedHint")
    : status === "active" ? t("suspendHint") : t("resumeHint");
  return (
    <div onClick={e => e.stopPropagation()}>
      <Pressable display="inline" onClick={toggle} disabled={toggling || offboarded} title={toggleTitle}
        style={{ cursor: offboarded ? "default" : "pointer" }}>
        <Badge tone={chip.tone} dot={false}>{toggling ? "…" : t(chip.label)}</Badge>
      </Pressable>
      {rowError && (
        <div style={{ marginTop: 6, fontSize: 11.5, color: K.danger, display: "flex", alignItems: "center", gap: 4 }}>
          <AlertCircle size={12} /> {rowError}
        </div>
      )}
    </div>
  );
}

/** Offboard / restore (same slot, AA-1035: tenant.write only) and Detail (every role). */
function TenantActions({ tenant, onChanged, onRestored, onOpen, canOffboardRestore }: {
  tenant: Tenant;
  onChanged: () => void;
  onRestored: (r: RestoreResult) => void;
  onOpen: (tenantId: string) => void;
  canOffboardRestore: boolean;
}) {
  const t = useT(TEN);
  const [offboarding, setOffboarding] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const offboarded = tenantStatus(tenant) === "offboarded";
  return (
    <div onClick={e => e.stopPropagation()}>
      <ButtonGroup label={t("actionsFor", tenant.name)} gap="sm">
        {canOffboardRestore && (offboarded ? (
          <Button size="sm" variant="ghost" onClick={() => setRestoring(true)} title={t("restoreHint")}>
            <RotateCcw size={12} /> {t("restore")}
          </Button>
        ) : (
          <Button size="sm" variant="danger" onClick={() => setOffboarding(true)} title={t("offboardHint")}>
            <Ban size={12} /> {t("offboard")}
          </Button>
        ))}
        <Button size="sm" variant="ghost" onClick={() => onOpen(tenant.tenant_id)}>
          <ChevronRight size={12} /> {t("detail")}
        </Button>
      </ButtonGroup>
      {canOffboardRestore && offboarding && (
        <OffboardModal tenant={tenant} onClose={() => setOffboarding(false)}
          onDone={() => { setOffboarding(false); onChanged(); }} />
      )}
      {canOffboardRestore && restoring && (
        <RestoreModal tenant={tenant} onClose={() => setRestoring(false)}
          onDone={r => { setRestoring(false); onRestored(r); }} />
      )}
    </div>
  );
}

function TenantsTable({ tenants, onChanged, onRestored, onOpen, controls }: {
  tenants: Tenant[];
  onChanged: () => void;
  onRestored: (r: RestoreResult) => void;
  onOpen: (tenantId: string) => void;
  controls: TenantDirectoryControls;
}) {
  const t = useT(TEN);
  const columns: ColumnDef<Tenant, unknown>[] = [
    { id: "tenant", header: t("colTenant"), accessorKey: "name", cell: ({ row: { original: x } }) => (
      <div>
        <div style={{ fontWeight: 600, color: K.ink, fontSize: 13 }}>{x.name}</div>
        <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 3, flexWrap: "wrap" }}>
          <code style={{ fontSize: 10.5, color: K.muted, fontFamily: mono }}>{x.slug}</code>
          {x.country && (
            <span style={{ display: "inline-flex", alignItems: "center", gap: 3, fontSize: 10.5, color: K.muted2 }}>
              <Globe size={10} />
              {x.country}
            </span>
          )}
        </div>
      </div>
    ) },
    { id: "plan", header: t("plan"), accessorKey: "plan_tier", cell: ({ row: { original: x } }) => (
      <div>
        <Badge tone={PLAN_BADGE[x.plan_tier] ?? "neutral"}>{x.plan_tier}</Badge>
        <div style={{ fontSize: 10.5, color: K.muted2, marginTop: 4 }}>
          {x.posts_per_week != null ? t("postsPerWeek", x.posts_per_week) : "—"}
        </div>
      </div>
    ) },
    { id: "lifecycle", header: t("colLifecycle"), enableSorting: false,
      cell: ({ row: { original: x } }) => <div style={{ minWidth: 200 }}><LifecycleBar lc={tenantLifecycle(x)} /></div> },
    { id: "sourceActive", header: t("colSourceActive"), accessorFn: (x) => tenantLifecycle(x).source_active, meta: { align: "right" },
      cell: ({ row: { original: x } }) => (
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: K.ink }}>{t("activeCount", tenantLifecycle(x).source_active)}</div>
          <div style={{ fontSize: 11, color: K.success }}>{t("calls", x.this_month.api_calls_used.toLocaleString())}</div>
        </div>
      ) },
    { id: "quota", header: t("colQuota"), accessorFn: (x) => x.this_month.quota_tours_pct, meta: { align: "right" },
      cell: ({ row: { original: x } }) => <span style={{ fontSize: 12, color: K.muted }}>{t("tourQuota", x.this_month.quota_tours_pct)}</span> },
    { id: "status", header: t("colStatus"), accessorFn: (x) => tenantStatus(x),
      cell: ({ row: { original: x } }) => (
        <TenantStatusToggle tenant={x} onChanged={onChanged} canToggle={controls.statusToggle} />
      ) },
    { id: "actions", header: "", enableSorting: false, enableHiding: false, meta: { hideLabelOnCard: true },
      cell: ({ row: { original: x } }) => (
        <TenantActions tenant={x} onChanged={onChanged} onRestored={onRestored} onOpen={onOpen}
          canOffboardRestore={controls.offboardRestore} />
      ) },
  ];
  return (
    <DataTable<Tenant>
      tableId="admin-tenants"
      data={tenants}
      columns={columns}
      getRowId={(x) => x.tenant_id}
      onRowClick={(x) => onOpen(x.tenant_id)}
      searchPlaceholder={t("searchTenants")}
      searchText={(x) => [x.name, x.slug, x.tenant_id, x.country].filter(Boolean).join(" ")}
      paginate={false}
      emptyTitle={t("noTenants")}
    />
  );
}

// ─── Main Page ────────────────────────────────────────────────────────────────

export default function TenantsPage() {
  // useSearchParams() needs a Suspense boundary in the App Router.
  return (
    <Suspense fallback={null}>
      <TenantsRouter />
    </Suspense>
  );
}

/** An old `?tenant=<id>` link goes to the Tenant 360 route (replace: the old URL leaves no history entry). */
function TenantsRouter() {
  const router = useRouter();
  const target = legacyTenantLinkTarget(useSearchParams().toString());
  useEffect(() => {
    if (target) router.replace(target);
  }, [target, router]);
  return target ? null : <TenantsPageInner />;
}

function TenantsPageInner() {
  const tr = useT(TEN);
  const router = useRouter();
  // AA-1039 R6 rest: Tenant 360 is its own route (/admin/tenants/<id>); a row opens it. An old deep link
  // (/admin/tenants?tenant=<id>[&tab=cost]: bookmarks, the ⌘K palette and Job drawer links before this change) is replaced
  // by the route (TenantsRouter below), so it never lands on the bare Directory.
  const openTenant = useCallback((tenantId: string) => router.push(tenant360Href(tenantId)), [router]);
  // AA-1035: the controls this role may use (the role reads "" until the identity cookie is read: no controls).
  const controls = tenantDirectoryControls(useAdminIdentity().role);
  const showUnmapped = controls.unmappedMarkets;
  const [tenants, setTenants]       = useState<Tenant[]>([]);
  const [loading, setLoading]       = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [notice, setNotice]         = useState<TenantNotice | null>(null);
  const [error, setError]           = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  // AA-629 Tier 2 — best-effort, tolerate failure so the whole page still loads without it.
  const [unmappedMarkets, setUnmappedMarkets] = useState<UnmappedMarketRequest[]>([]);

  // The fetches alone (state set only once an answer arrives), so the mount effect can call it; `loading` starts true.
  const fetchTenants = useCallback(() => {
    // AA-797 — this page manages suspended/offboarded tenants too; other callers stay active-only.
    const tenantsP = fetch("/api/admin/tenants?include_inactive=true")
      .then(async (res) => {
        if (!res.ok) { setError(tr("loadError")); return; }
        const data = await res.json();
        setError("");
        setTenants(data.tenants ?? []);
      })
      .catch(() => setError(tr("connectionError")))
      .finally(() => setLoading(false));
    return tenantsP;
  }, [tr]);
  // AA-1035: only a role with tenant.read asks (the API answers 403 to every desk role); its own effect, so the call
  // goes out once the role is known and never for a role without the read.
  const fetchUnmapped = useCallback(() => fetch("/api/admin/unmapped-market-requests")
    .then(r => r.ok ? r.json() : null)
    .then(d => setUnmappedMarkets(d?.requests ?? []))
    .catch(() => setUnmappedMarkets([])), []);
  // silent = refresh in place after a row action (keeps expanded rows; no loading screen).
  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    if (showUnmapped) fetchUnmapped();
    await fetchTenants();
  }, [fetchTenants, fetchUnmapped, showUnmapped]);

  useEffect(() => { fetchTenants(); }, [fetchTenants]);
  useEffect(() => { if (showUnmapped) fetchUnmapped(); }, [showUnmapped, fetchUnmapped]);

  function restored(r: RestoreResult) {
    const n = r.cancelled_jobs?.length ?? 0;
    setNotice({
      tenant_id: r.tenant_id, tenant_name: r.tenant_name,
      title: tr("tenantRestored"),
      note: n ? tr("restoredJobs", n, r.cancelled_jobs.map(j => j.kind).join(", "))
        : tr("restoredNoJobs"),
    });
    load(true);
  }

  const statusOf = (t: Tenant): TenantStatus => t.status ?? (t.is_active ? "active" : "suspended");
  const countBy = (st: TenantStatus) => tenants.filter(t => statusOf(t) === st).length;
  const totalActive     = countBy("active");
  const shown = statusFilter === "all" ? tenants : tenants.filter(t => statusOf(t) === statusFilter);
  const totalSrcActive  = tenants.reduce((s, t) => s + (t.lifecycle?.source_active ?? 0), 0);
  const totalMstrActive = tenants.reduce((s, t) => s + (t.lifecycle?.master_active ?? 0), 0);

  return (
    <>
        <PageFrame>
          <PageHeader
            title={tr("title")}
            description={tr("description", totalActive, totalSrcActive.toLocaleString(), totalMstrActive.toLocaleString())}
            actions={
              <>
                <Button variant="secondary" onClick={() => load()}><RefreshCw size={13} /> {tr("refresh")}</Button>
                {controls.create && (
                  <Button variant="primary" onClick={() => setShowCreate(true)}><Plus size={14} /> {tr("newTenant")}</Button>
                )}
              </>
            }
          />

          {error && (
            <div style={{ marginBottom: 16, padding: "10px 14px", background: K.dangerSoft, border: `1px solid ${K.dangerBorder}`, borderRadius: 8, fontSize: 13, color: K.danger, display: "flex", alignItems: "center", gap: 8 }}>
              <AlertCircle size={14} /> {error}
            </div>
          )}

          {/* AA-629 Tier 2 — tenants waiting on a market outside DFS_LOCATION_MAP. Data-driven
              visibility for the Tier 3 decision (add the market? worth the DFS spend?) — no
              action here, just the queue. */}
          {showUnmapped && unmappedMarkets.length > 0 && (
            <div style={{ marginBottom: 16, padding: "10px 14px", background: K.amberSoft, border: `1px solid ${K.amber}`, borderRadius: 8, fontSize: 13, color: K.ink, display: "flex", alignItems: "flex-start", gap: 8 }}>
              <Globe size={14} style={{ marginTop: 1, flexShrink: 0, color: K.amber }} />
              <span>
                {unmappedMarkets.map((u, i) => (
                  <span key={u.country_code}>
                    {i > 0 && " · "}
                    <strong>{u.tenant_count}</strong> {tr("waitingOnMarket", u.tenant_count)} <strong>{u.country_code}</strong> {tr("notSupported")}
                  </span>
                ))}
              </span>
            </div>
          )}

          {/* Summary cards */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 14, marginBottom: 20 }}>
            {[
              { label: tr("statusActive"),     value: String(totalActive),               color: K.success },
              { label: tr("statusSuspended"),  value: String(countBy("suspended")),      color: K.amber },
              { label: tr("statusOffboarded"), value: String(countBy("offboarded")),     color: K.danger },
              { label: tr("masterActive"),     value: totalMstrActive.toLocaleString(),  color: K.ink },
            ].map(c => (
              <Section key={c.label}>
                <Eyebrow>{c.label}</Eyebrow>
                <div style={{ fontFamily: sans, fontVariantNumeric: "tabular-nums", fontSize: 26, fontWeight: 600, color: c.color, letterSpacing: "-0.02em" }}>{c.value}</div>
              </Section>
            ))}
          </div>

          {/* AA-797 — status filter */}
          <div style={{ display: "flex", gap: 6, marginBottom: 12, flexWrap: "wrap" }}>
            {(["all", "active", "suspended", "offboarded"] as StatusFilter[]).map(f => {
              const on = statusFilter === f;
              const n = f === "all" ? tenants.length : countBy(f);
              return (
                <ChipButton key={f} size="sm" selected={on} onClick={() => setStatusFilter(f)}>
                  {f === "all" ? tr("all") : tr(TENANT_STATUS_STYLE[f].label)} ({n})
                </ChipButton>
              );
            })}
          </div>

          {/* Table */}
          <Section padded={false} style={{ overflow: "hidden" }} bodyStyle={{ padding: loading ? 0 : 12 }}>
            {loading ? <LoadingScreen message={tr("loadingTenants")} /> : (
              <TenantsTable tenants={shown} onChanged={() => load(true)} onRestored={restored}
                onOpen={openTenant} controls={controls} />
            )}
          </Section>
        </PageFrame>


      {controls.create && showCreate && <CreateModal onClose={() => setShowCreate(false)} onCreated={k => { setShowCreate(false); setNotice(k); load(); }} />}
      {notice && <TenantNoticeModal notice={notice} onClose={() => setNotice(null)} />}
    </>
  );
}
