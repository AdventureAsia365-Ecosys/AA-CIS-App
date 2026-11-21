"use client";
// app/admin/platform/staff/RoleEditor.tsx — AA-1155 (spec task T13, root spec R5.6): the Staff page's CIS role editor.
// An owner on a company sign-in creates a CIS custom role by copying a built-in one (its capabilities without the
// approvals), ticks its capabilities, renames, archives or restores it; everyone else on this page reads it. Reads
// GET /api/admin/platform/roles/cis, writes POST / PATCH there (api/routers/admin_platform_roles.py): the backend
// re-checks the owner, refuses an approve-class or unknown capability (400) and audits every change before and
// after. Kit components only.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import {
  Badge, Button, Checkbox, ConfirmModal, Field, Input, K, Modal, RADIUS, Section, Select, apiGet, apiSend, mono, useT,
} from "../../../_kit";
import {
  CIS_ROLE_EDITOR_PATH, CUSTOM_ROLE_CODE_RE, assignableByArea, cloneCapabilities, roleEditBody, roleFormOk,
  type EditorRole, type RoleEditorView,
} from "@/lib/staffRoleEditor";
import { ROLE_EDITOR } from "./editors.messages";
import { OutcomeBanner, type Outcome } from "./StaffManage";

export const ROLE_EDITOR_KEY = ["platform-roles-cis"] as const;

function useRoleEditor() {
  return useQuery({ queryKey: ROLE_EDITOR_KEY, queryFn: () => apiGet<RoleEditorView>(CIS_ROLE_EDITOR_PATH) });
}

/** Refresh the editor and the role catalogue the Staff page pickers and Roles & permissions read. */
function useRefreshRoles() {
  const qc = useQueryClient();
  return async () => {
    await Promise.all([qc.invalidateQueries({ queryKey: ROLE_EDITOR_KEY }),
      qc.invalidateQueries({ queryKey: ["platform-roles"] })]);
  };
}

async function send(path: string, method: "POST" | "PATCH", body: unknown, ok: string): Promise<Outcome> {
  try {
    await apiSend(path, { method, body });
    return { kind: "ok", message: ok };
  } catch (e) {
    return { kind: "error", message: e instanceof Error ? e.message : String(e) };
  }
}

type Editing = null | { kind: "new" } | { kind: "edit"; role: EditorRole } | { kind: "archive"; role: EditorRole };

export function RoleEditor() {
  const t = useT(ROLE_EDITOR);
  const q = useRoleEditor();
  const refresh = useRefreshRoles();
  const [editing, setEditing] = useState<Editing>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [busy, setBusy] = useState(false);
  const view = q.data;
  const canEdit = !!view?.can_edit && !!view?.custom_available;
  const builtinLabel = useMemo(() => new Map((view?.builtin ?? []).map((b) => [b.code, b.label])), [view]);

  const run = async (path: string, method: "POST" | "PATCH", body: unknown, ok: string) => {
    setBusy(true);
    const o = await send(path, method, body, ok);
    setBusy(false);
    if (o.kind === "ok") { setEditing(null); await refresh(); }
    setOutcome(o);
    return o;
  };
  const restore = (r: EditorRole) => void run(`${CIS_ROLE_EDITOR_PATH}/${encodeURIComponent(r.code)}`, "PATCH",
    { archived: false }, t("restoredDone", r.label));

  return (
    <Section testId="staff-role-editor" title={t("title")}
      description={<>{t("description")}{view && !view.can_edit ? ` ${t("readOnly")}` : ""}</>}
      actions={canEdit && (
        <Button size="sm" variant="secondary" testId="role-editor-new" disabled={busy}
          onClick={() => { setOutcome(null); setEditing({ kind: "new" }); }}>
          <Plus size={13} /> {t("newRole")}
        </Button>
      )}
      bodyStyle={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {outcome && <OutcomeBanner outcome={outcome} onRetried={setOutcome} />}
      {q.isLoading && <span style={{ fontSize: 13, color: K.muted }}>{t("loading")}</span>}
      {q.error && <OutcomeBanner outcome={{ kind: "error", message: (q.error as Error).message }} onRetried={() => {}} />}
      {view && !view.custom_available && <span style={{ fontSize: 13, color: K.muted }}>{t("unavailable")}</span>}
      {view?.custom_available && view.custom.length === 0 && (
        <span style={{ fontSize: 13, color: K.muted }}>{t("none")}</span>
      )}
      {view?.custom.map((r) => (
        <RoleRow key={r.code} role={r} base={builtinLabel.get(r.based_on) ?? r.based_on} canEdit={canEdit} busy={busy}
          onEdit={() => { setOutcome(null); setEditing({ kind: "edit", role: r }); }}
          onArchive={() => { setOutcome(null); setEditing({ kind: "archive", role: r }); }}
          onRestore={() => restore(r)} />
      ))}
      {view && editing?.kind === "new" && (
        <RoleForm view={view} busy={busy} onClose={() => setEditing(null)}
          onSubmit={(f) => run(CIS_ROLE_EDITOR_PATH, "POST", f, t("created", f.label.trim()))} />
      )}
      {view && editing?.kind === "edit" && (
        <RoleForm view={view} role={editing.role} busy={busy} onClose={() => setEditing(null)}
          onSubmit={(f) => {
            const body = roleEditBody(editing.role, f.label, f.capabilities);
            if (Object.keys(body).length === 0) { setEditing(null); return Promise.resolve(null); }
            return run(`${CIS_ROLE_EDITOR_PATH}/${encodeURIComponent(editing.role.code)}`, "PATCH", body,
              t("saved", f.label.trim()));
          }} />
      )}
      <ConfirmModal open={editing?.kind === "archive"} onClose={() => setEditing(null)} destructive busy={busy}
        title={editing?.kind === "archive" ? t("archiveQ", editing.role.label) : ""}
        confirmLabel={t("archive")} body={t("archiveBody")} testId="role-editor-archive-confirm"
        onConfirm={() => editing?.kind === "archive" && void run(
          `${CIS_ROLE_EDITOR_PATH}/${encodeURIComponent(editing.role.code)}`, "PATCH", { archived: true },
          t("archivedDone", editing.role.label))} />
    </Section>
  );
}

function RoleRow({ role, base, canEdit, busy, onEdit, onArchive, onRestore }: {
  role: EditorRole;
  base: string;
  canEdit: boolean;
  busy: boolean;
  onEdit: () => void;
  onArchive: () => void;
  onRestore: () => void;
}) {
  const t = useT(ROLE_EDITOR);
  return (
    <div data-testid={`role-editor-row-${role.code}`} style={{
      display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", padding: "10px 12px", background: K.line2,
      borderRadius: RADIUS.sm, opacity: role.archived ? 0.75 : 1,
    }}>
      <span style={{ flex: "1 1 240px", minWidth: 0, display: "flex", flexDirection: "column", gap: 3 }}>
        <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: K.ink3 }}>{role.label}</span>
          <Badge tone={role.archived ? "neutral" : "success"}>{role.archived ? t("archived") : t("active")}</Badge>
        </span>
        <span style={{ fontSize: 12, color: K.muted, lineHeight: 1.45, overflowWrap: "anywhere" }}>
          <span style={{ fontFamily: mono }}>{role.code}</span> · {t("basedOn", base)} ·{" "}
          {t("capabilities", role.capabilities.length)} · {t("holders", role.holders)}
        </span>
      </span>
      {canEdit && (
        <span style={{ display: "flex", gap: 6 }}>
          {!role.archived && (
            <Button size="sm" variant="secondary" testId={`role-editor-edit-${role.code}`} onClick={onEdit} disabled={busy}>
              {t("edit")}
            </Button>
          )}
          {role.archived ? (
            <Button size="sm" variant="ghost" testId={`role-editor-restore-${role.code}`} onClick={onRestore} disabled={busy}>
              {t("restore")}
            </Button>
          ) : (
            <Button size="sm" variant="ghost" testId={`role-editor-archive-${role.code}`} onClick={onArchive}
              disabled={busy || role.holders > 0} title={role.holders > 0 ? t("archiveBody") : undefined}>
              {t("archive")}
            </Button>
          )}
        </span>
      )}
    </div>
  );
}

interface RoleFormValue {
  code: string;
  label: string;
  based_on: string;
  capabilities: string[];
}

/** Create (code, name, copy-from, capabilities) or edit (name, capabilities) one custom role. */
function RoleForm({ view, role, busy, onClose, onSubmit }: {
  view: RoleEditorView;
  role?: EditorRole;
  busy: boolean;
  onClose: () => void;
  onSubmit: (f: RoleFormValue) => Promise<Outcome | null>;
}) {
  const t = useT(ROLE_EDITOR);
  const bases = view.builtin.filter((b) => b.clonable);
  const firstBase = bases[0]?.code ?? "";
  const [code, setCode] = useState("custom_");
  const [label, setLabel] = useState(role?.label ?? "");
  const [basedOn, setBasedOn] = useState(role?.based_on ?? firstBase);
  const [caps, setCaps] = useState<string[]>(role ? role.capabilities : cloneCapabilities(view, firstBase));
  const areas = assignableByArea(view);
  const isNew = !role;
  const ok = roleFormOk({ code: isNew ? code : undefined, label, capabilities: caps });
  const toggle = (c: string, on: boolean) => setCaps((cur) => (on ? [...new Set([...cur, c])] : cur.filter((x) => x !== c)));

  return (
    <Modal open onClose={onClose} width={620} pinFooter testId="role-editor-form"
      title={isNew ? t("createTitle") : t("editTitle", role.label)}
      footer={<>
        <Button variant="ghost" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
        <Button variant="primary" testId="role-editor-submit" disabled={!ok || busy}
          onClick={() => void onSubmit({ code, label, based_on: basedOn, capabilities: caps })}>
          {busy ? t("saving") : isNew ? t("create") : t("save")}
        </Button>
      </>}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {isNew && (
          <Field label={t("code")} hint={t("codeHint")} required>
            <Input data-testid="role-editor-code" value={code} maxLength={54} autoComplete="off"
              invalid={code !== "custom_" && !CUSTOM_ROLE_CODE_RE.test(code)}
              onChange={(e) => setCode(e.target.value.trim().toLowerCase())} />
          </Field>
        )}
        <Field label={t("label")} hint={t("labelHint")} required>
          <Input data-testid="role-editor-label" value={label} maxLength={80} autoComplete="off"
            onChange={(e) => setLabel(e.target.value)} />
        </Field>
        {isNew && (
          <Field label={t("base")} hint={t("baseHint")}>
            <Select data-testid="role-editor-base" value={basedOn}
              options={bases.map((b) => ({ value: b.code, label: b.label }))}
              onChange={(e) => { setBasedOn(e.target.value); setCaps(cloneCapabilities(view, e.target.value)); }} />
          </Field>
        )}
        <Field as="div" label={t("capabilitiesTitle")} hint={t("capabilitiesHint")}>
          <div data-testid="role-editor-caps" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {areas.map(([area, names]) => (
              <div key={area} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <span style={{ fontSize: 12, fontWeight: 600, color: K.muted, textTransform: "capitalize" }}>{area}</span>
                <div style={{ display: "flex", gap: "4px 16px", flexWrap: "wrap" }}>
                  {names.map((c) => (
                    <Checkbox key={c} data-testid={`role-editor-cap-${c}`} label={<span style={{ fontFamily: mono }}>{c}</span>}
                      checked={caps.includes(c)} onChange={(e) => toggle(c, e.target.checked)} style={{ fontSize: 12.5 }} />
                  ))}
                </div>
              </div>
            ))}
          </div>
        </Field>
      </div>
    </Modal>
  );
}
