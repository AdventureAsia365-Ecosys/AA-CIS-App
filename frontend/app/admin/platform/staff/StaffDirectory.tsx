"use client";
// app/admin/platform/staff/StaffDirectory.tsx
// AA-883 (S241 admin console GĐ2, design §5.4) — Platform › People & access › Staff: who has a CIS
// staff account, with which role, and whether it is active. Read-only (Nghiệp S241 Q6): accounts are
// created / reset with scripts/create_staff_user.py until Cognito (ADR 0006); role and active changes
// (owner-only staff.write, Q2) come with GĐ5.
//
// API: GET /api/admin/platform/staff and /api/admin/platform/staff/{id} (backend
// api/routers/admin_platform.py, admin only: staff_policy staff.read + BFF API_READ_RULES). Only the
// existing shared.admin_users columns exist: no e-mail and no last login yet.
//
// The drawer is driven by local state; the URL follows it (/admin/platform/staff/{id}) through
// history.replaceState, so a deep link (from Booking later, §6.3) opens the same drawer. The initial
// id comes from the [id] route as a prop — no setState in an effect (React Compiler).

import { useQuery } from "@tanstack/react-query";
import { ExternalLink, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  Drawer,
  ErrorState,
  K,
  PageHeader,
  RADIUS,
  apiGet,
  formatDate,
  formatDateTime,
  mono,
  sans,
  type ColumnDef,
  type Tone,
} from "../../../_kit";
import { ROLE_LABEL, isStaffRole } from "@/lib/rbac";
import { BOOKING_ADMIN_URL, bookingStaffUrl } from "@/lib/apps";

export interface StaffAccount {
  id: string;
  username: string;
  role: string;
  is_active: boolean;
  created_at: string | null;
  updated_at: string | null;
}

interface StaffListResp {
  staff: StaffAccount[];
  total: number;
  active: number;
  active_by_role: Record<string, number>;
}

const LIST_KEY = ["platform-staff"] as const;
const BASE_PATH = "/admin/platform/staff";

const ROLE_TONE: Record<string, Tone> = { admin: "accent", reviewer: "info", content: "purple" };

function roleLabel(role: string): string {
  return isStaffRole(role) ? ROLE_LABEL[role] : role;
}

function RoleBadge({ role }: { role: string }) {
  return <Badge tone={ROLE_TONE[role] ?? "neutral"} dot={false}>{roleLabel(role)}</Badge>;
}

function ActiveBadge({ active }: { active: boolean }) {
  return <Badge tone={active ? "success" : "neutral"}>{active ? "Active" : "Inactive"}</Badge>;
}

function BookingLink({ id, label = "Open in Booking" }: { id: string; label?: string }) {
  return (
    <a
      href={bookingStaffUrl(id)}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(e) => e.stopPropagation()}
      title="This account's profile in AA-Booking admin (new tab; sign in there with your CIS account)"
      style={{
        display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12.5, fontWeight: 600,
        color: K.accentDeep, textDecoration: "none", whiteSpace: "nowrap", fontFamily: sans,
      }}
    >
      {label} <ExternalLink size={12} />
    </a>
  );
}

function setUrl(id: string | null): void {
  if (typeof window === "undefined") return;
  window.history.replaceState(window.history.state, "", id ? `${BASE_PATH}/${encodeURIComponent(id)}` : BASE_PATH);
}

export default function StaffDirectory({ initialId = null }: { initialId?: string | null }) {
  const [openId, setOpenId] = useState<string | null>(initialId);

  const list = useQuery({
    queryKey: LIST_KEY,
    queryFn: () => apiGet<StaffListResp>("/api/admin/platform/staff"),
    staleTime: 60_000,
  });
  const staff = useMemo(() => list.data?.staff ?? [], [list.data]);

  const open = (id: string) => { setOpenId(id); setUrl(id); };
  const close = () => { setOpenId(null); setUrl(null); };

  const columns = useMemo<ColumnDef<StaffAccount, unknown>[]>(() => [
    {
      accessorKey: "username",
      header: "Username",
      cell: (c) => <span style={{ fontWeight: 600, color: K.ink }}>{c.row.original.username}</span>,
    },
    { accessorKey: "role", header: "Role", cell: (c) => <RoleBadge role={c.row.original.role} /> },
    {
      id: "status",
      accessorFn: (r) => (r.is_active ? "active" : "inactive"),
      header: "Status",
      cell: (c) => <ActiveBadge active={c.row.original.is_active} />,
    },
    { accessorKey: "created_at", header: "Created", cell: (c) => formatDate(c.row.original.created_at) },
    { accessorKey: "updated_at", header: "Updated", cell: (c) => formatDateTime(c.row.original.updated_at) },
    {
      id: "booking",
      header: "Booking",
      enableSorting: false,
      cell: (c) => <BookingLink id={c.row.original.id} />,
    },
  ], []);

  const counts = list.data;

  return (
    <main className="aa-admin-main" style={{ flex: 1, minWidth: 0, overflowY: "auto", padding: "28px 36px 56px" }}>
      <PageHeader
        title="Staff"
        description={
          <>
            Every CIS staff account and its role. The same account signs in to{" "}
            <a href={BOOKING_ADMIN_URL} target="_blank" rel="noopener noreferrer" style={{ color: K.accentDeep, fontWeight: 600 }}>
              Booking admin
            </a>
            , which derives its own role from the CIS role. Read-only for now: accounts are created and reset by
            the platform owner until the move to single sign-on.
          </>
        }
        actions={
          <Button variant="secondary" size="sm" onClick={() => list.refetch()} disabled={list.isFetching}>
            <RefreshCw size={13} /> Refresh
          </Button>
        }
      />

      {counts && (
        <div data-testid="staff-counts" style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 16 }}>
          <CountChip label="Accounts" value={counts.total} />
          <CountChip label="Active" value={counts.active} />
          {Object.entries(counts.active_by_role).map(([role, n]) => (
            <CountChip key={role} label={`Active ${roleLabel(role).toLowerCase()}`} value={n} />
          ))}
        </div>
      )}

      {list.isError ? (
        <ErrorState message={(list.error as Error)?.message || "Could not load staff accounts"} onRetry={() => list.refetch()} />
      ) : (
        <DataTable<StaffAccount>
          tableId="platform-staff"
          data={staff}
          columns={columns}
          getRowId={(r) => r.id}
          loading={list.isLoading}
          searchable
          searchPlaceholder="Search username or role…"
          enableCsv
          csvFilename="cis-staff.csv"
          emptyTitle="No staff accounts"
          emptyDescription="No account in shared.admin_users yet."
          onRowClick={(r) => open(r.id)}
          tableMinWidth={640}
        />
      )}

      <StaffDrawer id={openId} fallback={staff.find((s) => s.id === openId) ?? null} onClose={close} />
    </main>
  );
}

function CountChip({ label, value }: { label: string; value: number }) {
  return (
    <div style={{
      display: "flex", alignItems: "baseline", gap: 8, padding: "8px 14px",
      background: K.card, border: `1px solid ${K.line}`, borderRadius: RADIUS.lg, fontFamily: sans,
    }}>
      <span style={{ fontSize: 18, fontWeight: 600, color: K.ink }}>{value}</span>
      <span style={{ fontSize: 12, color: K.muted }}>{label}</span>
    </div>
  );
}

function StaffDrawer({ id, fallback, onClose }: { id: string | null; fallback: StaffAccount | null; onClose: () => void }) {
  const detail = useQuery({
    queryKey: ["platform-staff", id],
    queryFn: () => apiGet<StaffAccount>(`/api/admin/platform/staff/${encodeURIComponent(id ?? "")}`),
    enabled: !!id,
    staleTime: 60_000,
    retry: false,
  });
  const s = detail.data ?? fallback;
  const notFound = !s && detail.isError;

  return (
    <Drawer
      open={!!id}
      onClose={onClose}
      title={s ? s.username : notFound ? "Staff account" : "Loading…"}
      footer={id && s ? <BookingLink id={s.id} label="Open this account in Booking admin" /> : undefined}
    >
      {notFound && (
        <ErrorState message={(detail.error as Error)?.message || "Staff account not found"} />
      )}
      {s && (
        <div style={{ display: "flex", flexDirection: "column", gap: 18, fontFamily: sans }}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <RoleBadge role={s.role} />
            <ActiveBadge active={s.is_active} />
          </div>
          <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "10px 18px", margin: 0, fontSize: 13 }}>
            <Field label="Username" value={s.username} />
            <Field label="CIS role" value={roleLabel(s.role)} />
            {/* Booking owns booking.staff_users (ADR 0008): its role starts from the CIS role on first
                sign-in and a Booking admin can change it there, so CIS does not show a copy. */}
            <Field label="Booking role" value={<BookingLink id={s.id} label="See in Booking admin" />}
              hint="Managed in Booking admin; it starts from the CIS role when the account first signs in there" />
            <Field label="Status" value={s.is_active ? "Active" : "Inactive (cannot sign in; open sessions end within ~6 min)"} />
            <Field label="Created" value={formatDateTime(s.created_at)} />
            <Field label="Updated" value={formatDateTime(s.updated_at)} />
            <Field label="Account id" value={<span style={{ fontFamily: mono, fontSize: 12, wordBreak: "break-all" }}>{s.id}</span>} />
          </dl>
          <div style={{ fontSize: 12.5, color: K.muted, lineHeight: 1.5 }}>
            Changing a role or deactivating an account is not available here yet. It will be limited to the
            platform owner and recorded in the audit log.
          </div>
        </div>
      )}
    </Drawer>
  );
}

function Field({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <>
      <dt style={{ color: K.muted, fontWeight: 500 }}>{label}</dt>
      <dd style={{ margin: 0, color: K.ink, minWidth: 0 }} title={hint}>{value}</dd>
    </>
  );
}
