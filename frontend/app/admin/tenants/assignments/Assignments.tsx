"use client";
// app/admin/tenants/assignments/Assignments.tsx — AA-1035 R2 slice 2 (root ADR 0019 d4): Tenants › Assignments.
// One kit DataTable row per partner tenant of the caller's scope (owner, backups, support, pool flag); a row opens a
// Drawer with its assignments. Only a role with tenant.assign (admin) sees the add / remove controls; the backend
// (GET /admin/tenant-assignments, PUT / DELETE /admin/tenants/{id}/assignments/{staff}) re-checks every call.

import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Trash2, UserPlus } from "lucide-react";
import {
  Badge, Button, ChipButton, DataTable, Drawer, ErrorState, Field, Input, K, PageHeader, Section, Select,
  useAdminIdentity, useT, useToast, type ColumnDef, type Tone,
} from "../../../_kit";
import { can, staffErrorMessage } from "@/lib/rbac";
import {
  ASSIGNABLE_STAFF_API, ASSIGNMENTS_API, ASSIGNMENTS_QUERY_KEY, addableStaff, assignmentPath, availableKinds,
  filterTenants, ofKind, staffLabel,
  type AssignableStaff, type AssignmentFilter, type AssignmentKind, type AssignmentsResp, type TenantAssignments,
} from "@/lib/tenantAssignments";
import { ASSIGN } from "./messages";

const STATUS_TONE: Record<string, Tone> = { active: "success", suspended: "warning", offboarded: "danger" };

async function send<T>(url: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method, headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status, detail: (data as { detail?: unknown }).detail });
  }
  return data as T;
}

function names(t: TenantAssignments, kind: AssignmentKind, none: string): string {
  const list = ofKind(t, kind);
  return list.length ? list.map(staffLabel).join(", ") : none;
}

function AssignmentDrawer({ tenant, canAssign, onClose }: {
  tenant: TenantAssignments; canAssign: boolean; onClose: () => void;
}) {
  const t = useT(ASSIGN);
  const qc = useQueryClient();
  const toast = useToast();
  const [staffId, setStaffId] = useState("");
  const [kind, setKind] = useState<AssignmentKind | "">("");
  const [busy, setBusy] = useState<string | null>(null);
  const staffQ = useQuery({
    queryKey: [...ASSIGNMENTS_QUERY_KEY, "staff"], enabled: canAssign, staleTime: 60_000,
    queryFn: () => send<{ staff: AssignableStaff[] }>(ASSIGNABLE_STAFF_API, "GET").then((r) => r.staff),
  });
  const addable = addableStaff(staffQ.data ?? [], tenant);
  const kinds = availableKinds(tenant);
  const chosenKind: AssignmentKind = kind && kinds.includes(kind) ? kind : kinds[0];

  const fail = (e: unknown) => {
    const err = e as { status?: number; detail?: unknown };
    toast.error(staffErrorMessage(err.status ?? 0, err.detail, t("failed")));
  };
  const done = async (msg: string) => {
    await qc.invalidateQueries({ queryKey: ASSIGNMENTS_QUERY_KEY });
    toast.success(msg);
  };
  const assign = async () => {
    if (!staffId) return;
    setBusy("add");
    try {
      await send(assignmentPath(tenant.tenant_id, staffId), "PUT", { kind: chosenKind });
      setStaffId("");
      await done(t("saved"));
    } catch (e) { fail(e); } finally { setBusy(null); }
  };
  const remove = async (adminUserId: string) => {
    setBusy(adminUserId);
    try {
      await send(assignmentPath(tenant.tenant_id, adminUserId), "DELETE");
      await done(t("removed"));
    } catch (e) { fail(e); } finally { setBusy(null); }
  };

  return (
    <Drawer open onClose={onClose} title={t("drawerTitle", tenant.name)} width={560}
      footer={<Button variant="secondary" onClick={onClose}>{t("close")}</Button>}>
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <p style={{ margin: 0, fontSize: 13, color: K.body }}>{t("drawerIntro")}</p>
        <Section title={t("current")} padded={false}>
          {tenant.assignments.length === 0 ? (
            <p style={{ margin: 0, padding: 14, fontSize: 13, color: K.muted }}>{t("nobody")}</p>
          ) : (
            <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {tenant.assignments.map((a) => (
                <li key={a.admin_user_id} style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 14px",
                  borderTop: `1px solid ${K.line}`, flexWrap: "wrap" }}>
                  <Badge tone={a.kind === "owner" ? "accent" : "neutral"} dot={false}>{t(`kind_${a.kind}`)}</Badge>
                  <span style={{ fontWeight: 600, color: K.ink, minWidth: 0, overflowWrap: "anywhere" }}>{staffLabel(a)}</span>
                  <span style={{ fontSize: 12, color: K.muted }}>{t("roleOf", a.staff_role)}</span>
                  {canAssign && (
                    <span style={{ marginLeft: "auto" }}>
                      <Button variant="ghost" size="sm" onClick={() => remove(a.admin_user_id)} disabled={busy !== null}>
                        <Trash2 size={13} /> {busy === a.admin_user_id ? t("removing") : t("remove")}
                      </Button>
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Section>
        {canAssign ? (
          <Section title={t("add")}>
            {addable.length === 0 && !staffQ.isLoading ? (
              <p style={{ margin: 0, fontSize: 13, color: K.muted }}>{t("noStaff")}</p>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 2fr) minmax(0, 1fr)", gap: 10 }}>
                <Field label={t("staffLabel")}>
                  <Select value={staffId} onChange={(e) => setStaffId(e.target.value)} placeholder={t("staffPick")}
                    options={addable.map((s) => ({ value: s.admin_user_id,
                      label: `${staffLabel(s)}${s.email ? ` (${s.email})` : ""} · ${s.role}` }))} />
                </Field>
                <Field label={t("kindLabel")}>
                  <Select value={chosenKind} onChange={(e) => setKind(e.target.value as AssignmentKind)}
                    options={kinds.map((k) => ({ value: k, label: t(`kind_${k}`) }))} />
                </Field>
                <div style={{ gridColumn: "1 / -1" }}>
                  <Button variant="primary" size="sm" onClick={assign} disabled={!staffId || busy !== null}
                    testId="assignment-add">
                    <UserPlus size={13} /> {busy === "add" ? t("assigning") : t("assign")}
                  </Button>
                </div>
              </div>
            )}
          </Section>
        ) : (
          <p style={{ margin: 0, fontSize: 12, color: K.muted }}>{t("onlyAdmin")}</p>
        )}
      </div>
    </Drawer>
  );
}

export default function Assignments() {
  const t = useT(ASSIGN);
  const qc = useQueryClient();
  const role = useAdminIdentity().role;
  const [filter, setFilter] = useState<AssignmentFilter>("all");
  const [query, setQuery] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);
  const q = useQuery({ queryKey: ASSIGNMENTS_QUERY_KEY, queryFn: () => send<AssignmentsResp>(ASSIGNMENTS_API, "GET") });
  const rows = useMemo(() => filterTenants(q.data?.tenants ?? [], filter, query), [q.data, filter, query]);
  const canAssign = (q.data?.can_assign ?? false) && can(role, "tenant.assign");
  const open = q.data?.tenants.find((x) => x.tenant_id === openId) ?? null;

  const statusText = useMemo(() => (s: string) => (
    s === "suspended" ? t("status_suspended") : s === "offboarded" ? t("status_offboarded") : t("status_active")
  ), [t]);
  const columns = useMemo<ColumnDef<TenantAssignments, unknown>[]>(() => [
    {
      accessorKey: "name", header: t("colTenant"),
      cell: (c) => (
        <span style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
          <span style={{ fontWeight: 600, color: K.ink }}>{c.row.original.name}</span>
          <span style={{ fontSize: 11, color: K.muted }}>{c.row.original.slug}</span>
        </span>
      ),
    },
    {
      id: "owner", header: t("colOwner"), accessorFn: (r) => names(r, "owner", ""),
      cell: (c) => ofKind(c.row.original, "owner").length
        ? <span>{names(c.row.original, "owner", t("none"))}</span>
        : <Badge tone="warning" dot={false}>{t("noOwner")}</Badge>,
    },
    { id: "backup", header: t("colBackup"), accessorFn: (r) => names(r, "backup", t("none")) },
    { id: "support", header: t("colSupport"), accessorFn: (r) => names(r, "support", t("none")) },
    {
      id: "state", header: t("colState"), accessorFn: (r) => r.status,
      cell: (c) => (
        <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
          <Badge tone={STATUS_TONE[c.row.original.status] ?? "neutral"}>{statusText(c.row.original.status)}</Badge>
          {c.row.original.in_pool && <Badge tone="info" dot={false}>{t("inPool")}</Badge>}
        </span>
      ),
    },
  ], [t, statusText]);

  const refresh = () => { void qc.invalidateQueries({ queryKey: ASSIGNMENTS_QUERY_KEY }); };
  return (
    <>
      <PageHeader
        title={t("title")}
        breadcrumbs={[{ label: t("crumbTenants"), href: "/admin/tenants" }, { label: t("title") }]}
        description={q.data?.scope === "scoped" ? t("descriptionScoped") : t("description")}
        actions={
          <Button variant="secondary" size="sm" onClick={refresh} disabled={q.isFetching}>
            <RefreshCw size={13} /> {q.isFetching ? t("loading") : t("refresh")}
          </Button>
        }
      />
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 12 }}>
        {(["all", "no_owner", "pool"] as AssignmentFilter[]).map((f) => (
          <ChipButton key={f} size="sm" selected={filter === f} onClick={() => setFilter(f)}>
            {f === "all" ? t("filterAll") : f === "no_owner" ? t("filterNoOwner") : t("filterPool")}
          </ChipButton>
        ))}
        <div style={{ flex: "1 1 220px", maxWidth: 360 }}>
          <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("search")}
            aria-label={t("search")} />
        </div>
      </div>
      {q.error ? <ErrorState message={`${t("loadError")}: ${(q.error as Error).message}`} onRetry={refresh} /> : (
        <Section padded={false} testId="tenant-assignments">
          <DataTable<TenantAssignments>
            tableId="tenant-assignments" data={rows} columns={columns} getRowId={(r) => r.tenant_id}
            loading={q.isLoading} emptyTitle={t("emptyTitle")} emptyDescription={t("emptyDescription")}
            onRowClick={(r) => setOpenId(r.tenant_id)} tableMinWidth={760} compact />
        </Section>
      )}
      {open && <AssignmentDrawer key={open.tenant_id} tenant={open} canAssign={canAssign} onClose={() => setOpenId(null)} />}
    </>
  );
}
