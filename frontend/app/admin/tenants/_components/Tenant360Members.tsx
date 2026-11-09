"use client";
// app/admin/tenants/_components/Tenant360Members.tsx — AA-1031 (S264): the Tenant 360 "Members" tab, the staff UI of
// the CIS Content studio memberships (shared.tenant_members). Root ADR 0010a decision 4: "Staff invite from Tenant 360"
// — until now the routes existed (AA-978, AA-979 P2b) but no page called them, so a tenant could only get a Content
// studio member through staff tooling. Over the EXISTING admin routes only (lib/tenantMembers.ts):
//   list (role, status, invited / last change, the identity as a 12-hex fingerprint), invite (e-mail + role + mail
//   language; the CRM delivers the AA system mail), send an invitation again (the address is typed again: CIS never
//   stores it), change the role, remove (final; the partner CRM signs the person out, and locks the agency when that
//   was its last active owner).
// Write controls follow the API's own answer for THIS session (AA-1031 F3, GET .../members/access through
// useMembersAccess): a read-only session (the Dev E2E admin) sees no Invite and no row menu, the E2E session on a
// synthetic tenant only what its invitation rule allows. Hidden while the read loads; the role (tenant.write) decides
// only when it failed. The BFF API_WRITE_RULES "tenants" and the backend re-check every write.
// No CRM role here: CRM roles live in the partner CRM (root ADR 0010a decision 3).

import { useCallback, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, UserPlus } from "lucide-react";
import {
  ApiError, Badge, Button, Checkbox, ConfirmModal, DataTable, ErrorState, Field, Input, K, Modal, RowMenu, Select,
  Spinner,
  ToastProvider, apiGet, apiSend, mono, useFormat, useT, useToast, type ColumnDef,
} from "../../../_kit";
import { useMembersAccess } from "../../_components/useMembersAccess";
import {
  INVITE_LOCALES, anyMemberWrite, deliveryOk, deliveryProblem, invitationsPath, isInviteEmail,
  isLastActiveOwner, isStudioRole, memberPath, memberRowActions, membersPath, removePath, roleChoices, sendAgainBody,
  sendAgainPath, statusTone,
  type InvitationResult, type InviteLocale, type MemberWriteResult, type StudioRole, type TenantMember,
  type TenantMembers,
} from "@/lib/tenantMembers";
import { MEM } from "./members.messages";

export const membersKey = (tenantId: string) => ["tenant360", tenantId, "members"] as const;

type Dialog =
  | { kind: "invite" }
  | { kind: "send"; member: TenantMember }
  | { kind: "role"; member: TenantMember }
  | { kind: "remove"; member: TenantMember }
  | null;

/** The outcome of the last invitation, kept on the page (a toast is gone too soon to read the mail result). */
type Outcome = { tone: "success" | "warning"; text: string } | null;

function errMessage(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : String(e);
}

function MembersInner({ tenantId }: { tenantId: string }) {
  const t = useT(MEM);
  const toast = useToast();
  const qc = useQueryClient();
  const { formatDateTime } = useFormat();
  const perm = useMembersAccess(tenantId);
  const canWrite = anyMemberWrite(perm);
  const rowMenu = perm.changeRole || perm.sendAgain || perm.remove;
  const [dialog, setDialog] = useState<Dialog>(null);
  const [outcome, setOutcome] = useState<Outcome>(null);

  const q = useQuery({
    queryKey: membersKey(tenantId),
    queryFn: () => apiGet<TenantMembers>(membersPath(tenantId), { admin: true }),
  });
  const members = useMemo(() => q.data?.members ?? [], [q.data]);
  const reload = () => void qc.invalidateQueries({ queryKey: membersKey(tenantId) });

  const roleLabel = useCallback((r: string) => (isStudioRole(r) ? t(`role_${r}`) : r), [t]);
  const statusLabel = useCallback(
    (s: string) => (s === "invited" || s === "active" || s === "removed" ? t(`status_${s}`) : s), [t]);

  const columns = useMemo<ColumnDef<TenantMember, unknown>[]>(() => {
    const cols: ColumnDef<TenantMember, unknown>[] = [
      {
        accessorKey: "identity_fingerprint", header: t("colIdentity"),
        cell: (c) => (c.row.original.identity_fingerprint
          ? <span style={{ fontFamily: mono, fontSize: 12 }}>{c.row.original.identity_fingerprint}</span>
          : <span style={{ color: K.muted }}>{t("notSignedIn")}</span>),
      },
      { accessorKey: "content_studio_role", header: t("colRole"), cell: (c) => roleLabel(c.row.original.content_studio_role) },
      {
        accessorKey: "status", header: t("colStatus"),
        cell: (c) => <Badge tone={statusTone(c.row.original.status)}>{statusLabel(c.row.original.status)}</Badge>,
      },
      { accessorKey: "created_at", header: t("colInvited"), cell: (c) => formatDateTime(c.row.original.created_at) },
      {
        accessorKey: "updated_at", header: t("colChanged"),
        cell: (c) => formatDateTime(c.row.original.removed_at ?? c.row.original.updated_at),
      },
      {
        accessorKey: "created_by", header: t("colBy"),
        cell: (c) => <span style={{ color: K.muted, overflowWrap: "anywhere" }}>
          {c.row.original.removed_by ?? c.row.original.created_by ?? "—"}</span>,
      },
    ];
    if (rowMenu) {
      cols.push({
        id: "actions", header: "", enableSorting: false,
        cell: (c) => {
          const m = c.row.original;
          const a = memberRowActions(m, perm);
          if (!a.changeRole && !a.sendAgain && !a.remove) return null;
          return (
            <RowMenu label={t("actions")} testId={`member-actions-${m.membership_id}`} items={[
              ...(a.sendAgain ? [{ label: t("sendAgain"), onSelect: () => setDialog({ kind: "send", member: m }) }] : []),
              ...(a.changeRole ? [{ label: t("changeRole"), onSelect: () => setDialog({ kind: "role", member: m }) }] : []),
              ...(a.remove ? [{ label: t("remove"), danger: true, onSelect: () => setDialog({ kind: "remove", member: m }) }] : []),
            ]} />
          );
        },
      });
    }
    return cols;
  }, [t, formatDateTime, rowMenu, perm, roleLabel, statusLabel]);

  /** Invitation answered (new or send again): keep the mail result on the page and toast it. */
  const delivered = (res: InvitationResult, again: boolean) => {
    if (deliveryOk(res.delivery)) {
      const text = again ? t("sentAgain")
        : res.delivery.expires_at ? t("invitedExpires", formatDateTime(res.delivery.expires_at)) : t("invited");
      setOutcome({ tone: "success", text });
      toast.success(text);
    } else {
      const text = t("deliveryFailed", deliveryProblem(res.delivery));
      setOutcome({ tone: "warning", text });
      toast.error(text);
    }
    reload();
  };

  const d = dialog;
  return (
    <section style={{ minWidth: 0, display: "grid", gap: 12 }} data-testid="tenant360-members">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
        <div style={{ minWidth: 0, flex: "1 1 320px" }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: K.ink }}>{t("title")}</div>
          <div style={{ fontSize: 12.5, color: K.muted, marginTop: 4, lineHeight: 1.5 }}>{t("help")}</div>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <Button size="sm" variant="ghost" onClick={reload} disabled={q.isFetching}>
            <RefreshCw size={13} /> {t("refresh")}
          </Button>
          {perm.invite && (
            <span data-testid="tenant360-members-invite">
              <Button size="sm" variant="primary" onClick={() => setDialog({ kind: "invite" })}>
                <UserPlus size={13} /> {t("invite")}
              </Button>
            </span>
          )}
        </div>
      </div>
      {perm.settled && !canWrite && (
        <div style={{ fontSize: 12, color: K.muted }} data-testid="tenant360-members-read-only">{t("readOnly")}</div>
      )}
      {outcome && (
        <div role="status" data-testid="tenant360-members-outcome" style={{
          padding: "9px 12px", borderRadius: 8, fontSize: 12.5, lineHeight: 1.5, color: K.ink,
          background: outcome.tone === "success" ? K.successSoft : K.amberSoft,
          border: `1px solid ${outcome.tone === "success" ? K.success : K.amber}`,
        }}>{outcome.text}</div>
      )}
      {q.isError ? (
        <ErrorState message={t("loadError", errMessage(q.error))} onRetry={() => q.refetch()} />
      ) : q.isLoading ? (
        <div style={{ fontSize: 12, color: K.muted, display: "flex", gap: 6, alignItems: "center" }}>
          <Spinner size={12} /> {t("loading")}</div>
      ) : (
        <DataTable<TenantMember> tableId="tenant360-members" data={members} columns={columns}
          getRowId={(r) => r.membership_id} paginate={false} compact tableMinWidth={680}
          emptyTitle={t("emptyTitle")} emptyDescription={perm.invite ? t("emptyWrite") : t("emptyRead")} />
      )}

      {d?.kind === "invite" && (
        <InviteDialog tenantId={tenantId} roles={perm.inviteRoles} onClose={() => setDialog(null)}
          onDone={(res) => { setDialog(null); delivered(res, false); }} />
      )}
      {d?.kind === "send" && (
        <InviteDialog tenantId={tenantId} member={d.member} roles={perm.inviteRoles} onClose={() => setDialog(null)}
          sameAddress={perm.sendAgainSame} newAddress={perm.sendAgainNew}
          onDone={(res) => { setDialog(null); delivered(res, true); }} />
      )}
      {d?.kind === "role" && (
        <RoleDialog tenantId={tenantId} member={d.member} lastOwner={isLastActiveOwner(members, d.member.membership_id)}
          roles={roleChoices(perm)} roleLabel={roleLabel} onClose={() => setDialog(null)}
          onDone={(res) => {
            setDialog(null);
            toast.success(res.changed ? t("roleSaved", roleLabel(res.member.content_studio_role)) : t("roleUnchanged"));
            reload();
          }} />
      )}
      {d?.kind === "remove" && (
        <RemoveDialog tenantId={tenantId} member={d.member}
          lastOwner={isLastActiveOwner(members, d.member.membership_id)} onClose={() => setDialog(null)}
          onDone={() => { setDialog(null); toast.success(t("removed")); reload(); }} />
      )}
    </section>
  );
}

// ── dialogs ──────────────────────────────────────────────────────────────────────────────────────────────────────

/** A new invitation (no `member`), or the send again of an invited member (`member`: role fixed). AA-1035 close-out:
 *  a send again goes to the address the CRM kept when `sameAddress` (support: only that); `newAddress` lets the
 *  session type another one instead. `roles`: the roles this session may invite as (the access read; the Dev E2E
 *  admin: editor / viewer only). */
function InviteDialog({ tenantId, member, roles, sameAddress = false, newAddress = true, onClose, onDone }: {
  tenantId: string; member?: TenantMember; roles: readonly StudioRole[]; sameAddress?: boolean; newAddress?: boolean;
  onClose: () => void; onDone: (res: InvitationResult) => void;
}) {
  const t = useT(MEM);
  const [email, setEmail] = useState("");
  // A send again starts on the kept address when the session may use it; a new invitation always types one.
  const [toKept, setToKept] = useState(!!member && sameAddress);
  const [role, setRole] = useState<StudioRole>(roles.includes("owner") ? "owner" : roles[0] ?? "viewer");
  const [locale, setLocale] = useState<InviteLocale>("en");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const emailOk = toKept || isInviteEmail(email);

  const submit = async () => {
    if (!emailOk || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = member
        ? await apiSend<InvitationResult>(sendAgainPath(tenantId, member.membership_id), {
          method: "POST", body: sendAgainBody(toKept, email, locale), admin: true })
        : await apiSend<InvitationResult>(invitationsPath(tenantId), {
          method: "POST", body: { email: email.trim(), content_studio_role: role, locale }, admin: true });
      onDone(res);
    } catch (e) {
      setError(t("failed", errMessage(e)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open onClose={busy ? () => {} : onClose} width={500} testId="tenant360-member-invite"
      title={member ? t("sendAgainTitle") : t("inviteTitle")}
      footer={<>
        <Button variant="ghost" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
        <Button variant="primary" onClick={() => void submit()} disabled={!emailOk || busy}>
          {busy ? t("sending") : t("send")}
        </Button>
      </>}>
      <form onSubmit={(e) => { e.preventDefault(); void submit(); }} style={{ display: "grid", gap: 14 }}>
        {member && (
          <div style={{ fontSize: 12.5, color: K.muted, lineHeight: 1.5 }}>
            {toKept ? t("sendAgainKeptNote") : t("sendAgainNote")}
          </div>
        )}
        {member && sameAddress && newAddress && (
          <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12.5 }}>
            <Checkbox id="tenant360-member-new-address" checked={!toKept} onChange={(e) => setToKept(!e.target.checked)}
              data-testid="tenant360-member-new-address" />
            {t("sendAgainNewAddress")}
          </label>
        )}
        {!toKept && (
          <Field label={t("email")} required error={email && !emailOk ? t("emailBad") : undefined} hint={t("emailHint")}>
            <Input type="email" autoComplete="off" value={email} maxLength={254} data-testid="tenant360-member-email"
              onChange={(e) => setEmail(e.target.value)} style={{ width: "100%" }} />
          </Field>
        )}
        {!member && (
          <Field label={t("role")} hint={t("roleHint")}>
            <Select value={role} onChange={(e) => setRole(e.target.value as StudioRole)} style={{ width: "100%" }}
              data-testid="tenant360-member-role">
              {roles.map((r) => <option key={r} value={r}>{t(`role_${r}`)}</option>)}
            </Select>
          </Field>
        )}
        <Field label={t("mailLanguage")}>
          <Select value={locale} onChange={(e) => setLocale(e.target.value as InviteLocale)} style={{ width: "100%" }}>
            {INVITE_LOCALES.map((l) => <option key={l} value={l}>{t(`lang_${l}`)}</option>)}
          </Select>
        </Field>
        {error && <div role="alert" style={{ fontSize: 12.5, color: K.danger, overflowWrap: "anywhere" }}>{error}</div>}
      </form>
    </Modal>
  );
}

function RoleDialog({ tenantId, member, lastOwner, roles, roleLabel, onClose, onDone }: {
  tenantId: string; member: TenantMember; lastOwner: boolean; roles: StudioRole[]; roleLabel: (r: string) => string;
  onClose: () => void; onDone: (res: MemberWriteResult) => void;
}) {
  const t = useT(MEM);
  const [role, setRole] = useState<StudioRole>(member.content_studio_role);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      onDone(await apiSend<MemberWriteResult>(memberPath(tenantId, member.membership_id), {
        method: "PATCH", body: { content_studio_role: role }, admin: true }));
    } catch (e) {
      setError(t("failed", errMessage(e)));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open onClose={busy ? () => {} : onClose} width={460} testId="tenant360-member-role-dialog" title={t("roleTitle")}
      footer={<>
        <Button variant="ghost" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
        <Button variant="primary" onClick={() => void submit()} disabled={busy || role === member.content_studio_role}>
          {busy ? t("sending") : t("save")}
        </Button>
      </>}>
      <div style={{ display: "grid", gap: 14 }}>
        <Field label={t("role")} hint={t("roleNote")}>
          <Select value={role} onChange={(e) => setRole(e.target.value as StudioRole)} style={{ width: "100%" }}>
            {roles.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
          </Select>
        </Field>
        {lastOwner && role !== "owner" && (
          <div style={{ fontSize: 12.5, color: K.amber, lineHeight: 1.5 }}>{t("noOwnerWarn")}</div>
        )}
        {error && <div role="alert" style={{ fontSize: 12.5, color: K.danger, overflowWrap: "anywhere" }}>{error}</div>}
      </div>
    </Modal>
  );
}

function RemoveDialog({ tenantId, member, lastOwner, onClose, onDone }: {
  tenantId: string; member: TenantMember; lastOwner: boolean; onClose: () => void; onDone: () => void;
}) {
  const t = useT(MEM);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await apiSend<MemberWriteResult>(removePath(tenantId, member.membership_id), { method: "POST", admin: true });
      onDone();
    } catch (e) {
      setError(t("failed", errMessage(e)));
    } finally {
      setBusy(false);
    }
  };
  return (
    <ConfirmModal open onClose={onClose} onConfirm={() => void confirm()} destructive busy={busy} error={error}
      title={t("removeTitle")} confirmLabel={t("removeConfirm")} testId="tenant360-member-remove"
      body={<div style={{ display: "grid", gap: 8 }}>
        <span>{t("removeBody")}</span>
        {lastOwner && <span style={{ color: K.danger }}>{t("lastOwnerWarn")}</span>}
      </div>} />
  );
}

export function Tenant360Members({ tenantId }: { tenantId: string }) {
  return (
    <ToastProvider>
      <MembersInner tenantId={tenantId} />
    </ToastProvider>
  );
}
