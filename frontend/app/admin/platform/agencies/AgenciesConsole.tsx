"use client";
// app/admin/platform/agencies/AgenciesConsole.tsx
// AA-941 (card CRM-13a, screen S-01) — Platform › Tenants › Agencies (CRM): the AA admin L1 console of the partner CRM.
//
// API (CIS BFF → api/routers/admin_crm_console.py → the CRM's crm.ops.v1 with this staff member's own Cognito token):
//   GET  /api/admin/platform/crm/console          {can_write, environment}: write buttons only for crm-platform-admin
//   GET  /api/admin/platform/crm/agencies         keyset pages of TenantSummary ("Load more")
//   POST /api/admin/platform/crm/agencies         create an agency (201; an existing id is 409 tenant_exists, nothing written)
//   GET  /api/admin/tenants                       the CIS tenants, to pick the one the agency belongs to
// The drawer (AgencyDrawer.tsx) holds the meters and every L1 action.
//
// L1 only: agency name, state, flags (owner yes/no, holds travellers yes/no), meters. No trip, pipeline, revenue or
// traveller data is requested or shown anywhere on this page. No polling (S240): load on open + Refresh.
// English only (CIS admin has no language switch here); strings are plain so they can move to an i18n file.

import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import {
  ApiError,
  Badge,
  Button,
  DataTable,
  ErrorState,
  K,
  Modal,
  PageHeader,
  apiGet,
  formatDate,
  useToast,
  type ColumnDef,
} from "../../../_kit";
import {
  CRM_API,
  LOCALES,
  crmErrorText,
  crmWrite,
  isTicket,
  stateLabel,
  type CrmConsole,
  type TenantPage,
  type TenantSummary,
} from "@/lib/crmConsole";
import AgencyDrawer from "./AgencyDrawer";
import { Field, InlineError, fieldStyle, noteStyle } from "./consoleUi";

export const CONSOLE_KEY = ["crm-console"] as const;
export const AGENCIES_KEY = ["crm-agencies"] as const;
const PAGE_SIZE = 50;

function errorText(e: unknown): string {
  if (e instanceof ApiError) return crmErrorText(e.status, e.message);
  return (e as Error)?.message || "Could not reach the CRM console.";
}

export default function AgenciesConsole() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<TenantSummary | null>(null);
  const [creating, setCreating] = useState(false);

  const consoleQ = useQuery({
    queryKey: CONSOLE_KEY,
    queryFn: () => apiGet<CrmConsole>(`${CRM_API}/console`),
    staleTime: 5 * 60_000,
    retry: false,
  });
  const agencies = useInfiniteQuery({
    queryKey: AGENCIES_KEY,
    queryFn: ({ pageParam }) =>
      apiGet<TenantPage>(`${CRM_API}/agencies?limit=${PAGE_SIZE}${pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ""}`),
    initialPageParam: "" as string,
    getNextPageParam: (last) => last.next_cursor || undefined,
    retry: false,
  });

  const rows = useMemo(() => agencies.data?.pages.flatMap((p) => p.items ?? []) ?? [], [agencies.data]);
  const canWrite = !!consoleQ.data?.can_write;
  const isProd = consoleQ.data?.environment === "prod";

  const columns = useMemo<ColumnDef<TenantSummary, unknown>[]>(() => [
    {
      accessorKey: "agency_name", header: "Agency",
      cell: (c) => (
        <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{ fontWeight: 600, color: K.ink }}>{c.row.original.agency_name}</span>
        </span>
      ),
    },
    {
      accessorKey: "state", header: "State",
      cell: (c) => {
        const s = stateLabel(c.row.original.state);
        return <Badge tone={s.tone}>{s.label}</Badge>;
      },
    },
    {
      accessorKey: "is_synthetic", header: "Synthetic",
      cell: (c) => (c.row.original.is_synthetic ? <Badge tone="purple" dot={false}>Synthetic</Badge>
        : <span style={{ color: K.muted }}>No</span>),
    },
    { accessorKey: "has_owner", header: "Owner", cell: (c) => yesNo(c.row.original.has_owner) },
    { accessorKey: "holds_travellers", header: "Has travellers", cell: (c) => yesNo(c.row.original.holds_travellers) },
    { accessorKey: "created_at", header: "Created", cell: (c) => formatDate(c.row.original.created_at) },
  ], []);

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: CONSOLE_KEY });
    void qc.invalidateQueries({ queryKey: AGENCIES_KEY });
  };

  const consoleError = consoleQ.error ? errorText(consoleQ.error) : null;
  const listError = agencies.error ? errorText(agencies.error) : null;

  return (
    <main className="aa-admin-main" style={{ flex: 1, minWidth: 0, overflowY: "auto", padding: "28px 36px 56px" }}>
      <PageHeader
        title="Agencies (CRM)"
        breadcrumbs={[{ label: "Platform" }, { label: "Tenants" }, { label: "Agencies (CRM)" }]}
        description="The partner CRM's agencies, as AA support sees them (L1): state, owner, usage meters and the support actions. No trips, pipeline or revenue are shown here. Loaded when you open the page; nothing refreshes in the background."
        actions={
          <>
            <Button variant="secondary" size="sm" onClick={refresh} disabled={agencies.isFetching}>
              <RefreshCw size={13} /> {agencies.isFetching ? "Loading…" : "Refresh"}
            </Button>
            {canWrite && (
              <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
                <Plus size={13} /> Create agency
              </Button>
            )}
          </>
        }
      />

      {consoleError ? (
        <ErrorState message={consoleError} onRetry={refresh} />
      ) : (
        <>
          {consoleQ.data && !canWrite && (
            <div data-testid="crm-console-readonly" style={{ ...noteStyle, marginBottom: 12 }}>
              Read only: your staff account is not in the crm-platform-admin group, so the actions are hidden.
            </div>
          )}
          <DataTable<TenantSummary>
            tableId="crm-agencies"
            data={rows}
            columns={columns}
            getRowId={(r) => r.tenant_id}
            loading={agencies.isLoading}
            error={listError}
            onRetry={refresh}
            searchable
            searchPlaceholder="Filter the loaded agencies"
            paginate={false}
            emptyTitle="No agencies yet"
            emptyDescription={canWrite ? "Create the first agency for a CIS tenant." : "The CRM has no agencies yet."}
            onRowClick={(r) => setSelected(r)}
            tableMinWidth={720}
          />
          {agencies.hasNextPage && (
            <div style={{ marginTop: 12 }}>
              <Button variant="ghost" size="sm" onClick={() => void agencies.fetchNextPage()}
                disabled={agencies.isFetchingNextPage}>
                {agencies.isFetchingNextPage ? "Loading…" : "Load more"}
              </Button>
            </div>
          )}
        </>
      )}

      {selected && (
        <AgencyDrawer
          key={selected.tenant_id}
          agency={selected}
          canWrite={canWrite}
          onClose={() => setSelected(null)}
          onChanged={(next) => {
            if (next) setSelected(next);
            void qc.invalidateQueries({ queryKey: AGENCIES_KEY });
          }}
        />
      )}
      <CreateAgencyModal open={creating} forceSynthetic={!isProd} onClose={() => setCreating(false)}
        onCreated={(agency) => { setCreating(false); void qc.invalidateQueries({ queryKey: AGENCIES_KEY }); setSelected(agency); }} />
    </main>
  );
}

function yesNo(v: boolean) {
  return v ? <span style={{ color: K.ink }}>Yes</span> : <span style={{ color: K.muted }}>No</span>;
}

// ── Create agency ────────────────────────────────────────────────────────────────────────────────

interface CisTenant {
  tenant_id: string;
  name: string;
  status?: string;
}

function newUuid(): string {
  // crypto.randomUUID needs a secure context, which the admin site always is (https, or localhost in development).
  return crypto.randomUUID();
}

function CreateAgencyModal({ open, forceSynthetic, onClose, onCreated }: {
  open: boolean; forceSynthetic: boolean; onClose: () => void; onCreated: (agency: TenantSummary) => void;
}) {
  const toast = useToast();
  const tenants = useQuery({
    queryKey: ["admin-tenants-for-crm"],
    queryFn: () => apiGet<{ tenants: CisTenant[] }>("/api/admin/tenants"),
    enabled: open,
    staleTime: 60_000,
  });
  const [source, setSource] = useState<"cis" | "synthetic">("cis");
  const [tenantId, setTenantId] = useState("");
  const [name, setName] = useState("");
  const [locale, setLocale] = useState<string>("en-AU");
  const [synthetic, setSynthetic] = useState(true);
  const [ticket, setTicket] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isSynthetic = forceSynthetic ? true : synthetic;
  const ticketOk = isTicket(ticket);
  const canSubmit = !!tenantId && name.trim().length > 0 && ticketOk && !busy;

  const reset = () => {
    setSource("cis"); setTenantId(""); setName(""); setLocale("en-AU"); setSynthetic(true); setTicket(""); setError(null);
  };
  const close = () => { if (!busy) { reset(); onClose(); } };

  const pickTenant = (id: string) => {
    setTenantId(id);
    const t = tenants.data?.tenants.find((x) => x.tenant_id === id);
    if (t) setName(t.name);
  };

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    const r = await crmWrite<TenantSummary>("POST", "/agencies", {
      tenant_id: tenantId, agency_name: name.trim(), is_synthetic: isSynthetic, default_locale: locale,
      ticket_ref: ticket.trim(),
    });
    setBusy(false);
    if (!r.ok) {
      setError(r.message);
      return;
    }
    // The CRM creates only (201); an id that already exists is refused with 409 tenant_exists above.
    toast.success(`Created ${r.body.agency_name}.`);
    reset();
    onCreated(r.body);
  };

  return (
    <Modal open={open} onClose={close} title="Create agency" width={520}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>Cancel</Button>
          <Button variant="primary" onClick={() => void submit()} disabled={!canSubmit}>
            {busy ? "Creating…" : "Create agency"}
          </Button>
        </>
      }>
      <form data-testid="crm-create-agency" onSubmit={(e) => { e.preventDefault(); void submit(); }}
        style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div role="radiogroup" aria-label="Agency source" style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 13 }}>
          <label style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
            <input type="radio" name="crm-source" checked={source === "cis"}
              onChange={() => { setSource("cis"); setTenantId(""); setName(""); }} />
            For a CIS tenant
          </label>
          {forceSynthetic && (
            <label style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
              <input type="radio" name="crm-source" checked={source === "synthetic"}
                onChange={() => { setSource("synthetic"); setTenantId(newUuid()); setName(""); }} />
              New synthetic agency (test)
            </label>
          )}
        </div>
        {source === "cis" ? (
          <Field label="CIS tenant" htmlFor="crm-create-tenant"
            hint={tenants.isError ? "Could not load the CIS tenants." : "The agency uses the CIS tenant's id."}>
            <select id="crm-create-tenant" value={tenantId} onChange={(e) => pickTenant(e.target.value)} style={fieldStyle}>
              <option value="">{tenants.isLoading ? "Loading…" : "Pick a tenant"}</option>
              {(tenants.data?.tenants ?? []).map((t) => (
                <option key={t.tenant_id} value={t.tenant_id}>{t.name}</option>
              ))}
            </select>
          </Field>
        ) : (
          <Field label="New agency id" htmlFor="crm-create-id" hint="A random id; this agency belongs to no CIS tenant.">
            <input id="crm-create-id" type="text" readOnly value={tenantId} style={{ ...fieldStyle, color: K.muted }} />
          </Field>
        )}
        <Field label="Agency name" htmlFor="crm-create-name">
          <input id="crm-create-name" type="text" autoComplete="off" maxLength={120} value={name}
            onChange={(e) => setName(e.target.value)} style={fieldStyle} />
        </Field>
        <Field label="Default language and region" htmlFor="crm-create-locale">
          <select id="crm-create-locale" value={locale} onChange={(e) => setLocale(e.target.value)} style={fieldStyle}>
            {LOCALES.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        </Field>
        <label style={{ display: "inline-flex", gap: 8, alignItems: "center", fontSize: 13 }}>
          <input type="checkbox" checked={isSynthetic} disabled={forceSynthetic}
            onChange={(e) => setSynthetic(e.target.checked)} />
          Synthetic (test) agency
        </label>
        <Field label="Ticket reference" htmlFor="crm-create-ticket"
          hint={ticket && !ticketOk ? "Use a project key, a dash and a number, for example AA-941."
            : "The support or Jira ticket this agency belongs to. It goes into the agency's access log."}>
          <input id="crm-create-ticket" data-testid="crm-create-ticket" type="text" autoComplete="off" value={ticket}
            maxLength={27} placeholder="AA-941" onChange={(e) => setTicket(e.target.value)} style={fieldStyle}
            aria-invalid={!!ticket && !ticketOk} />
        </Field>
        <div style={noteStyle}>
          {forceSynthetic
            ? "This environment only accepts synthetic agencies, so the box stays ticked."
            : "Production refuses synthetic agencies."}{" "}
          No member is created: invite the first owner from the agency&apos;s panel afterwards.
        </div>
        {error && <InlineError message={error} />}
      </form>
    </Modal>
  );
}
