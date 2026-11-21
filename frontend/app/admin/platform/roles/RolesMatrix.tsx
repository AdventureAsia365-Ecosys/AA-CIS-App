"use client";
// app/admin/platform/roles/RolesMatrix.tsx — Platform › Roles & permissions (AA-1155 spec task T10, R5.4).
//
// Read-only: every app's staff roles from GET /api/admin/platform/roles (backend api/core/staff_roles.py
// roles_catalogue, staff.read; BFF admin only, lib/rbac.ts API_READ_RULES "platform"). CIS built-in and custom roles
// with their permissions plus a CIS permission matrix, Booking built-in roles (their permissions are the Booking
// code's) and custom roles, the three CRM staff groups as fixed rows, TripPlanner as "no staff roles". No person, no
// agent or test identity, no issue id (lib/rolesMatrix.ts visibleRows). Kit components only; pure helpers in
// lib/rolesMatrix.ts.

import { useQuery } from "@tanstack/react-query";
import { Check, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import {
  Badge, DataTable, ErrorState, Input, K, PageFrame, PageHeader, Section, TableSkeleton, apiGet, mono, useT,
  type ColumnDef,
} from "../../../_kit";
import {
  ROLES_PATH, cisCapabilityColumns, matchesFilter, matrixRows, rowsForApp, type MatrixRow, type RoleApp,
  type RolesCatalogue,
} from "@/lib/rolesMatrix";
import { ROLES } from "./messages";

const small = { fontSize: 12, color: K.muted, lineHeight: 1.5 } as const;

type RT = ReturnType<typeof useRolesT>;
function useRolesT() {
  return useT(ROLES);
}

function Permissions({ row, t }: { row: MatrixRow; t: RT }) {
  if (row.capabilities === null) {
    return <span style={small}>{row.app === "crm" ? t("definedInCrm") : t("definedInBooking")}</span>;
  }
  if (row.capabilities.length === 0) return <span style={small}>{t("noPermissions")}</span>;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 6px" }}>
      {row.capabilities.map((c) => (
        <code key={c} style={{ fontFamily: mono, fontSize: 11.5, color: K.ink3, background: K.line2, borderRadius: 4,
          padding: "1px 5px" }}>{c}</code>
      ))}
    </div>
  );
}

function KindBadges({ row, t }: { row: MatrixRow; t: RT }) {
  const kind = row.kind === "custom" ? t("kindCustom") : row.kind === "group" ? t("kindGroup") : t("kindBuiltin");
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
      <Badge tone={row.kind === "custom" ? "info" : "neutral"} dot={false}>{kind}</Badge>
      {row.archived && <Badge tone="warning" dot={false}>{t("archived")}</Badge>}
    </div>
  );
}

function AppTable({ app, rows, t, testId }: { app: RoleApp; rows: MatrixRow[]; t: RT; testId: string }) {
  const columns = useMemo<ColumnDef<MatrixRow, unknown>[]>(() => [
    { id: "role", header: t("colRole"), accessorFn: (r) => r.label,
      cell: ({ row: { original: r } }) => (
        <div style={{ display: "grid", gap: 2, minWidth: 0 }}>
          <span style={{ fontSize: 13, color: K.ink, fontWeight: 600 }}>{r.label}</span>
          <span style={{ ...small, fontFamily: mono, wordBreak: "break-all" }}>{r.code}</span>
        </div>
      ) },
    { id: "kind", header: t("colKind"), accessorFn: (r) => r.kind,
      cell: ({ row: { original: r } }) => <KindBadges row={r} t={t} /> },
    ...(app === "crm" ? [] : [{
      id: "based", header: t("colBasedOn"), accessorFn: (r: MatrixRow) => r.basedOn ?? "",
      cell: ({ row: { original: r } }: { row: { original: MatrixRow } }) => (
        <span style={{ ...small, fontFamily: mono }}>{r.basedOn ?? "—"}</span>
      ),
    } as ColumnDef<MatrixRow, unknown>]),
    { id: "perms", header: t("colPermissions"), accessorFn: (r) => (r.capabilities ?? []).join(" "),
      cell: ({ row: { original: r } }) => <Permissions row={r} t={t} /> },
  ], [app, t]);
  return (
    <DataTable<MatrixRow> tableId={testId} data={rows} columns={columns} getRowId={(r) => r.id} searchable={false}
      paginate={false} compact tableMinWidth={720} />
  );
}

function CisMatrix({ rows, t }: { rows: MatrixRow[]; t: RT }) {
  const roles = rowsForApp(rows, "cis");
  const caps = cisCapabilityColumns(rows);
  const data = useMemo(() => caps.map((c) => ({ id: c })), [caps]);
  const columns = useMemo<ColumnDef<{ id: string }, unknown>[]>(() => [
    { id: "permission", header: t("colPermission"), accessorKey: "id",
      cell: ({ row: { original: p } }) => <span style={{ fontFamily: mono, fontSize: 12 }}>{p.id}</span> },
    ...roles.map((r) => ({
      id: r.id, header: r.label, accessorFn: (p: { id: string }) => (r.capabilities ?? []).includes(p.id),
      cell: ({ row: { original: p } }: { row: { original: { id: string } } }) => (
        (r.capabilities ?? []).includes(p.id)
          ? <span aria-label={t("holds")} title={t("holds")} style={{ color: K.success }}><Check size={14} /></span>
          : <span aria-hidden="true" style={{ color: K.muted2 }}>·</span>
      ),
    } as ColumnDef<{ id: string }, unknown>)),
  ], [roles, t]);
  if (!roles.length || !caps.length) return null;
  return (
    <Section title={t("matrixTitle")} description={t("matrixIntro")} padded={false} testId="roles-cis-matrix">
      <DataTable<{ id: string }> tableId="roles-cis-matrix" data={data} columns={columns} getRowId={(p) => p.id}
        searchable={false} paginate={false} compact tableMinWidth={260 + roles.length * 110} />
    </Section>
  );
}

function Loaded({ data }: { data: RolesCatalogue }) {
  const t = useRolesT();
  const [query, setQuery] = useState("");
  const all = useMemo(() => matrixRows(data), [data]);
  const shown = useMemo(() => all.filter((r) => r.kind === "none" || matchesFilter(r, query)), [all, query]);
  const cis = rowsForApp(shown, "cis");
  const booking = rowsForApp(shown, "booking");
  const crm = rowsForApp(shown, "crm");
  const tripplanner = rowsForApp(shown, "tripplanner");
  return (
    <>
      <div style={{ maxWidth: 360 }}>
        <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("filter")} aria-label={t("filter")}
          data-testid="roles-filter" />
      </div>
      <Section title={t("appCis")} description={t("appCisNote")} padded={false} testId="roles-app-cis">
        {!data.cis.custom_available && <div style={{ ...small, color: K.amber, padding: "10px 20px" }} role="status">
          {t("customUnavailable")}</div>}
        {cis.length ? <AppTable app="cis" rows={cis} t={t} testId="roles-cis" />
          : <div style={{ ...small, padding: "14px 20px" }}>{t("noMatch")}</div>}
      </Section>
      {!query && <CisMatrix rows={all} t={t} />}
      <Section title={t("appBooking")} description={t("appBookingNote")} padded={false} testId="roles-app-booking">
        {!data.booking.custom_available && <div style={{ ...small, color: K.amber, padding: "10px 20px" }}
          role="status">{t("customUnavailable")}</div>}
        {data.booking.custom_available && data.booking.custom.length === 0 && !query && (
          <div style={{ ...small, padding: "10px 20px" }}>{t("noCustom")}</div>)}
        {booking.length ? <AppTable app="booking" rows={booking} t={t} testId="roles-booking" />
          : <div style={{ ...small, padding: "14px 20px" }}>{t("noMatch")}</div>}
      </Section>
      <Section title={t("appCrm")} description={t("appCrmNote")} padded={false} testId="roles-app-crm">
        {crm.length ? <AppTable app="crm" rows={crm} t={t} testId="roles-crm" />
          : <div style={{ ...small, padding: "14px 20px" }}>{t("noMatch")}</div>}
      </Section>
      {tripplanner.length > 0 && (
        <Section title={t("appTripPlanner")} testId="roles-app-tripplanner">
          <div style={small}>{t("noStaffRoles")}</div>
        </Section>
      )}
    </>
  );
}

export default function RolesMatrix() {
  const t = useRolesT();
  const q = useQuery({ queryKey: ["platform-roles"], queryFn: () => apiGet<RolesCatalogue>(ROLES_PATH) });
  return (
    <PageFrame>
      <PageHeader
        title={<span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}><ShieldCheck size={18} /> {t("title")}</span>}
        description={t("intro")}
      />
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 14 }}>
        {q.isLoading && <TableSkeleton rows={8} cols={4} />}
        {q.isError && <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`}
          onRetry={() => void q.refetch()} />}
        {q.data && <Loaded data={q.data} />}
      </div>
    </PageFrame>
  );
}
