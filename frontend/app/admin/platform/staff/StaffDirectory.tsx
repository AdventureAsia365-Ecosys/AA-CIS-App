"use client";
// app/admin/platform/staff/StaffDirectory.tsx
// AA-883 (S241 admin console GĐ2, design §5.4) — Platform › People & access › Staff: who has a CIS
// staff account, with which role, and whether it is active.
//
// API: GET /api/admin/platform/staff and /api/admin/platform/staff/{id} (backend
// api/routers/admin_platform.py, admin only: staff_policy staff.read + BFF API_READ_RULES). Since
// AA-816 C6 a row also carries the work email, display name, owner flag, last sign-in, whether a
// company sign-in (Cognito) is linked, and the Booking role (shared.staff_app_grant).
//
// AA-816 C6 writes (StaffManage.tsx): a platform owner on a company sign-in creates accounts and
// changes roles / access from this page; everyone else sees it read-only. Who may write comes from
// the list's `viewer` block (lib/rbac.ts canWriteStaff); the backend refuses everyone else anyway.
//
// The drawer is driven by local state; the URL follows it (/admin/platform/staff/{id}) through
// history.replaceState, so a deep link (from Booking later, §6.3) opens the same drawer. The initial
// id comes from the [id] route as a prop — no setState in an effect (React Compiler).

import { useQuery } from "@tanstack/react-query";
import { Check, Copy, ExternalLink, RefreshCw, UserPlus } from "lucide-react";
import { useMemo, useRef, useState } from "react";
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
  mono,
  sans,
  useFormat,
  useT,
  type ColumnDef,
  type Tone, AdminMain,
} from "../../../_kit";
import { canWriteStaff, type StaffViewer } from "@/lib/rbac";
import { BOOKING_ADMIN_URL, bookingStaffUrl } from "@/lib/apps";
import {
  CreateStaffModal,
  OutcomeBanner,
  StaffManagePanel,
  useStaffWords,
  type Outcome,
  type StaffAccount,
} from "./StaffManage";
import DirectoryHealth from "./DirectoryHealth";
import { STAFF } from "./messages";

export type { StaffAccount } from "./StaffManage";

interface StaffListResp {
  staff: StaffAccount[];
  total: number;
  active: number;
  active_by_role: Record<string, number>;
  viewer?: StaffViewer;
}

const LIST_KEY = ["platform-staff"] as const;
const BASE_PATH = "/admin/platform/staff";

const ROLE_TONE: Record<string, Tone> = { admin: "accent", reviewer: "info", content: "purple" };

function RoleBadge({ role }: { role: string }) {
  const { roleLabel } = useStaffWords();
  return <Badge tone={ROLE_TONE[role] ?? "neutral"} dot={false}>{roleLabel(role)}</Badge>;
}

function ActiveBadge({ active }: { active: boolean }) {
  const t = useT(STAFF);
  return <Badge tone={active ? "success" : "neutral"}>{active ? t("active") : t("disabled")}</Badge>;
}

function BookingLink({ id, label }: { id: string; label?: string }) {
  const t = useT(STAFF);
  return (
    <a
      href={bookingStaffUrl(id)}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(e) => e.stopPropagation()}
      title={t("bookingLinkTitle")}
      style={{
        display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12.5, fontWeight: 600,
        color: K.accentDeep, textDecoration: "none", whiteSpace: "nowrap", fontFamily: sans,
      }}
    >
      {label ?? t("openInBooking")} <ExternalLink size={12} />
    </a>
  );
}

function setUrl(id: string | null): void {
  if (typeof window === "undefined") return;
  window.history.replaceState(window.history.state, "", id ? `${BASE_PATH}/${encodeURIComponent(id)}` : BASE_PATH);
}

export default function StaffDirectory({ initialId = null }: { initialId?: string | null }) {
  const t = useT(STAFF);
  const { formatDate, formatDateTime } = useFormat();
  const { roleLabel, bookingRoleLabel } = useStaffWords();
  const [openId, setOpenId] = useState<string | null>(initialId);
  const [creating, setCreating] = useState(false);
  const [pageOutcome, setPageOutcome] = useState<Outcome | null>(null);

  const list = useQuery({
    queryKey: LIST_KEY,
    queryFn: () => apiGet<StaffListResp>("/api/admin/platform/staff"),
    staleTime: 60_000,
  });
  const staff = useMemo(() => list.data?.staff ?? [], [list.data]);
  // Disabled accounts are kept (audit history, Booking references) but hidden by default (S242).
  const [showDisabled, setShowDisabled] = useState(false);
  const disabledCount = useMemo(() => staff.filter((s) => !s.is_active).length, [staff]);
  const visibleStaff = useMemo(() => (showDisabled ? staff : staff.filter((s) => s.is_active)), [staff, showDisabled]);
  const viewer = list.data?.viewer;
  const canWrite = canWriteStaff(viewer);

  const open = (id: string) => { setOpenId(id); setUrl(id); };
  const close = () => { setOpenId(null); setUrl(null); };

  const columns = useMemo<ColumnDef<StaffAccount, unknown>[]>(() => [
    {
      id: "name",
      accessorFn: (r) => `${r.display_name ?? ""} ${r.username} ${r.email ?? ""}`,
      header: t("colName"),
      cell: (c) => <NameCell s={c.row.original} />,
    },
    { accessorKey: "role", header: t("colCisRole"), cell: (c) => <RoleBadge role={c.row.original.role} /> },
    {
      id: "booking_role",
      accessorFn: (r) => r.booking_role ?? "",
      header: t("colBookingRole"),
      cell: (c) => <span style={{ color: c.row.original.booking_role ? K.ink : K.muted }}>{bookingRoleLabel(c.row.original.booking_role)}</span>,
    },
    {
      id: "status",
      accessorFn: (r) => (r.is_active ? "active" : "inactive"),
      header: t("colStatus"),
      cell: (c) => <ActiveBadge active={c.row.original.is_active} />,
    },
    {
      accessorKey: "last_login_at",
      header: t("colLastSignIn"),
      cell: (c) => (c.row.original.last_login_at ? formatDateTime(c.row.original.last_login_at) : <span style={{ color: K.muted }}>{t("never")}</span>),
    },
    {
      id: "invited",
      accessorFn: (r) => r.last_invited_at ?? r.last_invite_requested_at ?? "",
      header: t("colLastInvited"),
      cell: (c) => {
        const s = c.row.original;
        if (!s.invite_status) return <span style={{ color: K.muted }}>{t("never")}</span>;
        if (s.invite_status === "queued") return <Badge tone="info" dot={false}>{t("sending")}</Badge>;
        if (s.invite_status === "not_sent") return <Badge tone="warning" dot={false}>{t("notSent")}</Badge>;
        return s.last_invited_at ? formatDate(s.last_invited_at) : t("sent");
      },
    },
    { accessorKey: "created_at", header: t("colCreated"), cell: (c) => formatDate(c.row.original.created_at) },
    {
      id: "booking",
      header: t("colBooking"),
      enableSorting: false,
      cell: (c) => <BookingLink id={c.row.original.id} />,
    },
  ], [t, formatDate, formatDateTime, bookingRoleLabel]);

  const counts = list.data;

  return (
    <AdminMain>
      <PageHeader
        title={t("title")}
        description={
          <>
            {t("signsInTo")}{" "}
            <a href={BOOKING_ADMIN_URL} target="_blank" rel="noopener noreferrer" style={{ color: K.accentDeep, fontWeight: 600 }}>
              {t("bookingAdmin")}
            </a>
            . {canWrite
              ? t("canWrite")
              : viewer?.auth === "legacy"
                ? t("readOnlyLegacy")
                : t("readOnly")}
          </>
        }
        actions={
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {canWrite && (
              <Button variant="primary" size="sm" onClick={() => { setPageOutcome(null); setCreating(true); }}>
                <UserPlus size={13} /> {t("addStaff")}
              </Button>
            )}
            <Button variant="secondary" size="sm" onClick={() => list.refetch()} disabled={list.isFetching}>
              <RefreshCw size={13} /> {t("refresh")}
            </Button>
          </div>
        }
      />

      {pageOutcome && (
        <div style={{ marginBottom: 14 }}>
          <OutcomeBanner outcome={pageOutcome} onRetried={setPageOutcome} />
        </div>
      )}
      {canWrite && (
        <CreateStaffModal open={creating} onClose={() => setCreating(false)}
          onDone={(o) => { setCreating(false); setPageOutcome(o); }} />
      )}

      {/* Two totals, then one "Active by role" chip holding every role count, so the strip is at most
          two rows at 390px (S241 UI verify: five separate chips wrapped to three rows). */}
      {counts && (
        <div data-testid="staff-counts" style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 16 }}>
          <CountChip label={t("accounts")} value={counts.total} />
          <CountChip label={t("active")} value={counts.active} />
          {Object.keys(counts.active_by_role).length > 0 && (
            <div data-testid="staff-counts-by-role" style={{ ...chipStyle, flexWrap: "wrap", rowGap: 2 }}>
              <span style={{ fontSize: 12, color: K.muted }}>{t("activeByRole")}</span>
              {Object.entries(counts.active_by_role).map(([role, n]) => (
                <span key={role} style={{ display: "inline-flex", alignItems: "baseline", gap: 5, whiteSpace: "nowrap" }}>
                  <span style={{ fontSize: 15, fontWeight: 600, color: K.ink }}>{n}</span>
                  <span style={{ fontSize: 12, color: K.muted }}>{roleLabel(role).toLowerCase()}</span>
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      <DirectoryHealth />

      {disabledCount > 0 && (
        <label data-testid="staff-show-disabled"
          style={{ display: "inline-flex", alignItems: "center", gap: 8, margin: "4px 0 10px", fontSize: 13, color: K.ink, cursor: "pointer", fontFamily: sans }}>
          <input type="checkbox" checked={showDisabled} onChange={(e) => setShowDisabled(e.target.checked)} />
          {t("showDisabled", disabledCount)}
        </label>
      )}

      {list.isError ? (
        <ErrorState message={(list.error as Error)?.message || t("loadError")} onRetry={() => list.refetch()} />
      ) : (
        <DataTable<StaffAccount>
          tableId="platform-staff"
          data={visibleStaff}
          columns={columns}
          getRowId={(r) => r.id}
          loading={list.isLoading}
          searchable
          searchPlaceholder={t("searchPlaceholder")}
          enableCsv
          csvFilename="cis-staff.csv"
          emptyTitle={t("emptyTitle")}
          emptyDescription={t("emptyDescription")}
          onRowClick={(r) => open(r.id)}
          tableMinWidth={960}
        />
      )}

      <StaffDrawer id={openId} fallback={staff.find((s) => s.id === openId) ?? null} onClose={close}
        canWrite={canWrite} selfId={viewer?.admin_id ?? null} />
    </AdminMain>
  );
}

const chipStyle: React.CSSProperties = {
  display: "flex", alignItems: "baseline", gap: 8, padding: "7px 12px", minWidth: 0,
  background: K.card, border: `1px solid ${K.line}`, borderRadius: RADIUS.lg, fontFamily: sans,
};

function CountChip({ label, value }: { label: string; value: number }) {
  return (
    <div style={chipStyle}>
      <span style={{ fontSize: 17, fontWeight: 600, color: K.ink }}>{value}</span>
      <span style={{ fontSize: 12, color: K.muted }}>{label}</span>
    </div>
  );
}

function NameCell({ s }: { s: StaffAccount }) {
  const t = useT(STAFF);
  return (
    <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
      <span style={{ display: "inline-flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        <span style={{ fontWeight: 600, color: K.ink }}>{s.display_name || s.username}</span>
        {s.is_owner && <Badge tone="accent" dot={false}>{t("owner")}</Badge>}
        {s.breakglass && <Badge tone="warning" dot={false}>{t("breakGlass")}</Badge>}
      </span>
      <span style={{ fontSize: 12, color: K.muted }}>{s.email ?? t("noWorkEmail", s.username)}</span>
    </span>
  );
}

function StaffDrawer({ id, fallback, onClose, canWrite, selfId }: {
  id: string | null;
  fallback: StaffAccount | null;
  onClose: () => void;
  canWrite: boolean;
  selfId: string | null;
}) {
  const t = useT(STAFF);
  const { formatDateTime } = useFormat();
  const { roleLabel, bookingRoleLabel, inviteLabel } = useStaffWords();
  // The last write's outcome, kept per account id so it survives the panel remounting after a refresh.
  const [outcome, setOutcome] = useState<{ id: string; outcome: Outcome } | null>(null);
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
      title={s ? (s.display_name || s.username) : notFound ? t("staffAccount") : t("loading")}
      footer={id && s ? <BookingLink id={s.id} label={t("openThisInBooking")} /> : undefined}
    >
      {notFound && (
        <ErrorState message={(detail.error as Error)?.message || t("notFound")} />
      )}
      {s && (
        <div data-testid="staff-detail-ready" style={{ display: "flex", flexDirection: "column", gap: 18, fontFamily: sans }}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <RoleBadge role={s.role} />
            <ActiveBadge active={s.is_active} />
            {s.is_owner && <Badge tone="accent" dot={false}>{t("owner")}</Badge>}
            {s.breakglass && <Badge tone="warning" dot={false}>{t("breakGlass")}</Badge>}
          </div>
          <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "10px 18px", margin: 0, fontSize: 13 }}>
            <Field label={t("fName")} value={s.display_name || "—"} />
            <Field label={t("fWorkEmail")} value={s.email || t("noCompanySignIn")} />
            <Field label={t("fUsername")} value={s.username} />
            <Field label={t("colCisRole")} value={roleLabel(s.role)} />
            {/* AA-816 C6: the Booking role is the shared.staff_app_grant row, written here together with the
                booking-<role> Cognito group. */}
            <Field label={t("colBookingRole")} value={bookingRoleLabel(s.booking_role)} />
            <Field label={t("fStatus")} value={s.is_active ? t("active") : t("disabledLong")} />
            <Field label={t("fCompanySignIn")} value={s.linked ? t("linked") : t("notLinked")} />
            <Field label={t("colLastSignIn")} value={s.last_login_at ? formatDateTime(s.last_login_at) : t("never")} />
            {/* S242: the invitation e-mail (SES), read from the audit log; "Resend invite" is in Manage access. */}
            <Field label={t("colLastInvited")} value={inviteLabel(s, formatDateTime)} />
            <Field label={t("colCreated")} value={formatDateTime(s.created_at)} />
            <Field label={t("fUpdated")} value={formatDateTime(s.updated_at)} />
            {/* Full width, one line, with a copy button: in the label/value grid the UUID wrapped at
                390px and left one character alone on the second line (S241 UI verify). */}
            <Field wide label={t("fAccountId")} value={<AccountId id={s.id} />} />
          </dl>
          {outcome && outcome.id === s.id && (
            <OutcomeBanner outcome={outcome.outcome} onRetried={(o) => setOutcome({ id: s.id, outcome: o })} />
          )}
          {canWrite ? (
            <StaffManagePanel key={`${s.id}:${s.role}:${s.booking_role ?? ""}:${s.is_active}`} staff={s} selfId={selfId}
              onOutcome={(o) => setOutcome({ id: s.id, outcome: o })} />
          ) : (
            <div style={{ fontSize: 12.5, color: K.muted, lineHeight: 1.5 }}>
              {t("ownerOnly")}
            </div>
          )}
        </div>
      )}
    </Drawer>
  );
}

type CopyState = "idle" | "copied" | "failed";

function AccountId({ id }: { id: string }) {
  const t = useT(STAFF);
  const [state, setState] = useState<CopyState>("idle");
  const idRef = useRef<HTMLSpanElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flash = (next: CopyState, ms: number) => {
    setState(next);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setState("idle"), ms);
  };
  // Fallback when the Clipboard API is missing (non-secure origin, old browser) or refuses: select
  // the id so a Ctrl/Cmd+C copies it, and say so next to the button.
  const selectId = () => {
    const el = idRef.current;
    const sel = typeof window !== "undefined" ? window.getSelection() : null;
    if (!el || !sel) return;
    const range = document.createRange();
    range.selectNodeContents(el);
    sel.removeAllRanges();
    sel.addRange(range);
  };
  const copy = () => {
    const clip = typeof navigator !== "undefined" ? navigator.clipboard : undefined;
    if (!clip?.writeText) {
      selectId();
      flash("failed", 6000);
      return;
    }
    clip.writeText(id).then(
      () => flash("copied", 2000),
      () => { selectId(); flash("failed", 6000); },
    );
  };

  return (
    <span style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0, flexWrap: "wrap", rowGap: 4 }}>
      <span ref={idRef} data-testid="staff-account-id" title={id} style={{
        fontFamily: mono, fontSize: 11.5, color: K.ink2, whiteSpace: "nowrap", overflow: "hidden",
        textOverflow: "ellipsis", minWidth: 0, maxWidth: "100%", padding: "3px 7px", background: K.line2,
        borderRadius: RADIUS.sm, userSelect: "all",
      }}>
        {id}
      </span>
      {/* 28×28 minimum hit area (the icon alone was ~22px tall). */}
      <Button variant="ghost" size="sm" onClick={copy} ariaLabel={t("copyAria")}
        title={t("copyTitle")} style={{ flexShrink: 0, minWidth: 28, minHeight: 28, padding: "4px 8px" }}>
        {state === "copied" ? <Check size={12} /> : <Copy size={12} />}
      </Button>
      <span role="status" aria-live="polite" data-testid="staff-account-id-copy-status"
        style={{ fontSize: 12, fontWeight: 500, color: state === "failed" ? K.danger : K.success }}>
        {state === "copied" && t("copied")}
        {state === "failed" && t("copyFailed")}
      </span>
    </span>
  );
}

function Field({ label, value, hint, wide = false }: { label: string; value: React.ReactNode; hint?: string; wide?: boolean }) {
  // `wide`: label above the value, across both grid columns (long values such as the account id).
  const span = wide ? { gridColumn: "1 / -1" } : undefined;
  return (
    <>
      <dt style={{ color: K.muted, fontWeight: 500, ...span, ...(wide ? { marginBottom: -6 } : null) }}>{label}</dt>
      <dd style={{ margin: 0, color: K.ink, minWidth: 0, ...span }} title={hint}>{value}</dd>
    </>
  );
}
