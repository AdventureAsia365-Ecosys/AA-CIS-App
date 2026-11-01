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
// AA-978 follow-up (crm.ops.v1 1.2, S253): the State column names who locked a locked agency next to the badge
// (TenantSummary.lock_source: "by AA staff", "by CIS tenant lifecycle", …; an unknown source is shown raw), and one
// line above the table shows the CIS membership feed's health as the CRM reports it (platform-wide, the same value on
// every agency). Read-only display: no new route or write.
// AA-994: text in the UI language (./messages.ts; CRM codes and labels through ./useCrmText.ts).

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
  useFormat,
  useT,
  useToast,
  type ColumnDef, PageFrame,
  Checkbox, Input, Radio, Select,
} from "../../../_kit";
import {
  CRM_API,
  LOCALES,
  isTicket,
  type CrmConsole,
  type TenantPage,
  type TenantSummary,
} from "@/lib/crmConsole";
import type { TenantRef } from "@/lib/tenants";
import { TenantSelect } from "../../_components/TenantSelect";
import AgencyDrawer from "./AgencyDrawer";
import { Field, InlineError, fieldStyle, noteStyle } from "./consoleUi";
import { AGENCIES } from "./messages";
import { useCrmText } from "./useCrmText";

export const CONSOLE_KEY = ["crm-console"] as const;
export const AGENCIES_KEY = ["crm-agencies"] as const;
const PAGE_SIZE = 50;

function useErrorText(): (e: unknown) => string {
  const t = useT(AGENCIES);
  const crm = useCrmText();
  return (e: unknown) => {
    if (e instanceof ApiError) return crm.errorText(e.status, e.message);
    return (e as Error)?.message || t("reachError");
  };
}

export default function AgenciesConsole() {
  const t = useT(AGENCIES);
  const crm = useCrmText();
  const { formatDate } = useFormat();
  const errorText = useErrorText();
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
  const feed = crm.platformFeed(rows);

  const columns = useMemo<ColumnDef<TenantSummary, unknown>[]>(() => [
    {
      accessorKey: "agency_name", header: t("colAgency"),
      cell: (c) => (
        <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{ fontWeight: 600, color: K.ink }}>{c.row.original.agency_name}</span>
        </span>
      ),
    },
    {
      accessorKey: "state", header: t("colState"),
      cell: (c) => {
        const s = crm.state(c.row.original.state);
        // AA-978 follow-up: a locked row names who locked it (the drawer shows the details and what lifts it).
        const by = crm.lockSource(c.row.original);
        return (
          <span title={by?.label} style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            <Badge tone={s.tone}>{s.label}</Badge>
            {by && <span data-testid="crm-row-lock-source" style={{ color: K.muted, fontSize: 12 }}>{by.short}</span>}
          </span>
        );
      },
    },
    {
      accessorKey: "is_synthetic", header: t("colSynthetic"),
      cell: (c) => (c.row.original.is_synthetic ? <Badge tone="purple" dot={false}>{t("synthetic")}</Badge>
        : <span style={{ color: K.muted }}>{t("no")}</span>),
    },
    { accessorKey: "has_owner", header: t("colOwner"), cell: (c) => yesNo(c.row.original.has_owner, t) },
    { accessorKey: "holds_travellers", header: t("colTravellers"), cell: (c) => yesNo(c.row.original.holds_travellers, t) },
    { accessorKey: "created_at", header: t("colCreated"), cell: (c) => formatDate(c.row.original.created_at) },
  ], [t, crm, formatDate]);

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: CONSOLE_KEY });
    void qc.invalidateQueries({ queryKey: AGENCIES_KEY });
  };

  const consoleError = consoleQ.error ? errorText(consoleQ.error) : null;
  const listError = agencies.error ? errorText(agencies.error) : null;

  return (
    <PageFrame>
      <PageHeader
        title={t("title")}
        breadcrumbs={[{ label: t("crumbPlatform") }, { label: t("crumbTenants") }, { label: t("title") }]}
        description={t("description")}
        actions={
          <>
            <Button variant="secondary" size="sm" onClick={refresh} disabled={agencies.isFetching}>
              <RefreshCw size={13} /> {agencies.isFetching ? t("loading") : t("refresh")}
            </Button>
            {canWrite && (
              <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
                <Plus size={13} /> {t("createAgency")}
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
              {t("readOnly")}
            </div>
          )}
          {feed && (
            <div data-testid="crm-console-membership-feed" title={feed.detail}
              style={{ ...noteStyle, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
              <span style={{ color: K.ink }}>{t("feedLine")}</span>
              <Badge tone={feed.tone}>{feed.label}</Badge>
              <span>{t("lastPull", feed.lastPull)}</span>
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
            searchPlaceholder={t("filterPlaceholder")}
            paginate={false}
            emptyTitle={t("emptyTitle")}
            emptyDescription={canWrite ? t("emptyWrite") : t("emptyRead")}
            onRowClick={(r) => setSelected(r)}
            tableMinWidth={720}
          />
          {agencies.hasNextPage && (
            <div style={{ marginTop: 12 }}>
              <Button variant="ghost" size="sm" onClick={() => void agencies.fetchNextPage()}
                disabled={agencies.isFetchingNextPage}>
                {agencies.isFetchingNextPage ? t("loading") : t("loadMore")}
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
    </PageFrame>
  );
}

function yesNo(v: boolean, t: (k: "yes" | "no") => string) {
  return v ? <span style={{ color: K.ink }}>{t("yes")}</span> : <span style={{ color: K.muted }}>{t("no")}</span>;
}

// ── Create agency ────────────────────────────────────────────────────────────────────────────────

function newUuid(): string {
  // crypto.randomUUID needs a secure context, which the admin site always is (https, or localhost in development).
  return crypto.randomUUID();
}

function CreateAgencyModal({ open, forceSynthetic, onClose, onCreated }: {
  open: boolean; forceSynthetic: boolean; onClose: () => void; onCreated: (agency: TenantSummary) => void;
}) {
  const t = useT(AGENCIES);
  const crm = useCrmText();
  const toast = useToast();
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

  const pickTenant = (id: string, t?: TenantRef) => {
    setTenantId(id);
    if (t) setName(t.name);
  };

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    const r = await crm.write<TenantSummary>("POST", "/agencies", {
      tenant_id: tenantId, agency_name: name.trim(), is_synthetic: isSynthetic, default_locale: locale,
      ticket_ref: ticket.trim(),
    });
    setBusy(false);
    if (!r.ok) {
      setError(r.message);
      return;
    }
    // The CRM creates only (201); an id that already exists is refused with 409 tenant_exists above.
    toast.success(t("created", r.body.agency_name));
    reset();
    onCreated(r.body);
  };

  return (
    <Modal open={open} onClose={close} title={t("createAgency")} width={520}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>{t("cancel")}</Button>
          <Button variant="primary" onClick={() => void submit()} disabled={!canSubmit}>
            {busy ? t("creating") : t("createAgency")}
          </Button>
        </>
      }>
      <form data-testid="crm-create-agency" onSubmit={(e) => { e.preventDefault(); void submit(); }}
        style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div role="radiogroup" aria-label={t("sourceGroup")} style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 13 }}>
          <label style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
            <Radio name="crm-source" checked={source === "cis"}
              onChange={() => { setSource("cis"); setTenantId(""); setName(""); }} />
            {t("forCisTenant")}
          </label>
          {forceSynthetic && (
            <label style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
              <Radio name="crm-source" checked={source === "synthetic"}
                onChange={() => { setSource("synthetic"); setTenantId(newUuid()); setName(""); }} />
              {t("newSynthetic")}
            </label>
          )}
        </div>
        {source === "cis" ? (
          // AA-919 S254: the shared admin tenant picker (active CIS tenants by name; it shows its own load error).
          <TenantSelect label={t("cisTenant")} value={tenantId} onChange={pickTenant} required
            hint={t("cisTenantHint")} />
        ) : (
          <Field label={t("newId")} htmlFor="crm-create-id" hint={t("newIdHint")}>
            <Input id="crm-create-id" type="text" readOnly value={tenantId} style={{ ...fieldStyle, color: K.muted }} />
          </Field>
        )}
        <Field label={t("agencyName")} htmlFor="crm-create-name">
          <Input id="crm-create-name" type="text" autoComplete="off" maxLength={120} value={name}
            onChange={(e) => setName(e.target.value)} style={fieldStyle} />
        </Field>
        <Field label={t("locale")} htmlFor="crm-create-locale">
          <Select id="crm-create-locale" value={locale} onChange={(e) => setLocale(e.target.value)} style={fieldStyle}>
            {LOCALES.map((l) => <option key={l} value={l}>{l}</option>)}
          </Select>
        </Field>
        <label style={{ display: "inline-flex", gap: 8, alignItems: "center", fontSize: 13 }}>
          <Checkbox checked={isSynthetic} disabled={forceSynthetic}
            onChange={(e) => setSynthetic(e.target.checked)} />
          {t("syntheticBox")}
        </label>
        <Field label={t("ticketLabel")} htmlFor="crm-create-ticket"
          hint={ticket && !ticketOk ? t("ticketBad") : t("createTicketHint")}>
          <Input id="crm-create-ticket" data-testid="crm-create-ticket" type="text" autoComplete="off" value={ticket}
            maxLength={27} placeholder="SUP-1024" onChange={(e) => setTicket(e.target.value)} style={fieldStyle}
            aria-invalid={!!ticket && !ticketOk} />
        </Field>
        <div style={noteStyle}>
          {forceSynthetic ? t("forceSynthetic") : t("prodRefuses")} {t("noMember")}
        </div>
        {error && <InlineError message={error} />}
      </form>
    </Modal>
  );
}
