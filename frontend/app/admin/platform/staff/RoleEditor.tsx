"use client";
// app/admin/platform/staff/RoleEditor.tsx — AA-1155 (spec task T13, root spec R5.6): the Staff page's CIS role editor.
// An owner on a company sign-in creates a CIS custom role by copying a built-in one (its capabilities without the
// approvals), ticks its capabilities, renames, archives or restores it; everyone else on this page reads it. Reads
// GET /api/admin/platform/roles/cis, writes POST / PATCH there (api/routers/admin_platform_roles.py): the backend
// re-checks the owner, refuses an approve-class or unknown capability (400) and audits every change before and
// after. Kit components only.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useState } from "react";
import {
  Badge, Button, Checkbox, ConfirmModal, Field, Input, K, Modal, Section, Select, apiGet, mono, useT,
} from "../../../_kit";
import {
  CIS_ROLE_EDITOR_PATH, CUSTOM_ROLE_CODE_RE, assignableByArea, cloneCapabilities, roleEditBody, roleFormOk,
  type EditorRole, type RoleEditorView,
} from "@/lib/staffRoleEditor";
import { ROLE_EDITOR } from "./editors.messages";
import { CHOICE_ROW, EditorRow, EditorStatus, useEditorWrite } from "./EditorParts";

export const ROLE_EDITOR_KEY = ["platform-roles-cis"] as const;

type Editing = null | { kind: "new" } | { kind: "edit"; role: EditorRole } | { kind: "archive"; role: EditorRole };

const rolePath = (r: EditorRole) => `${CIS_ROLE_EDITOR_PATH}/${encodeURIComponent(r.code)}`;

/** The editor's data, its writes (the editor and the role catalogue the pickers read are refreshed after each). */
function useRoleEditorState() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ROLE_EDITOR_KEY, queryFn: () => apiGet<RoleEditorView>(CIS_ROLE_EDITOR_PATH) });
  const write = useEditorWrite(() => Promise.all([qc.invalidateQueries({ queryKey: ROLE_EDITOR_KEY }),
    qc.invalidateQueries({ queryKey: ["platform-roles"] })]));
  const view = q.data;
  return { q, view, write, canEdit: !!view && view.can_edit && view.custom_available };
}

export function RoleEditor() {
  const t = useT(ROLE_EDITOR);
  const { q, view, write, canEdit } = useRoleEditorState();
  const [editing, setEditing] = useState<Editing>(null);
  const open = (next: Editing) => { write.setOutcome(null); setEditing(next); };
  const run = async (path: string, method: "POST" | "PATCH", body: unknown, ok: string) => {
    if (await write.run(path, method, body, ok)) setEditing(null);
  };
  return (
    <Section testId="staff-role-editor" title={t("title")}
      description={view && !view.can_edit ? `${t("description")} ${t("readOnly")}` : t("description")}
      actions={canEdit && (
        <Button size="sm" variant="secondary" testId="role-editor-new" disabled={write.busy}
          onClick={() => open({ kind: "new" })}>
          <Plus size={13} /> {t("newRole")}
        </Button>
      )}
      bodyStyle={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <EditorStatus outcome={write.outcome} onOutcome={write.setOutcome} loading={q.isLoading} error={q.error}
        unavailable={!!view && !view.custom_available} empty={!!view && view.custom.length === 0}
        words={{ loading: t("loading"), unavailable: t("unavailable"), none: t("none") }} />
      {(view?.custom ?? []).map((r) => (
        <RoleRow key={r.code} role={r} view={view as RoleEditorView} canEdit={canEdit} busy={write.busy}
          onEdit={() => open({ kind: "edit", role: r })} onArchive={() => open({ kind: "archive", role: r })}
          onRestore={() => void run(rolePath(r), "PATCH", { archived: false }, t("restoredDone", r.label))} />
      ))}
      {view && <RoleDialogs view={view} editing={editing} busy={write.busy} onClose={() => setEditing(null)} run={run} />}
    </Section>
  );
}

/** The create / edit form and the archive confirmation of the role being edited. */
function RoleDialogs({ view, editing, busy, onClose, run }: {
  view: RoleEditorView;
  editing: Editing;
  busy: boolean;
  onClose: () => void;
  run: (path: string, method: "POST" | "PATCH", body: unknown, ok: string) => Promise<void>;
}) {
  const t = useT(ROLE_EDITOR);
  const archiving = editing?.kind === "archive" ? editing.role : null;
  const edit = (role: EditorRole, f: RoleFormValue) => {
    const body = roleEditBody(role, f.label, f.capabilities);
    if (Object.keys(body).length === 0) return Promise.resolve(onClose());
    return run(rolePath(role), "PATCH", body, t("saved", f.label.trim()));
  };
  return (
    <>
      {editing?.kind === "new" && (
        <RoleForm view={view} busy={busy} onClose={onClose}
          onSubmit={(f) => run(CIS_ROLE_EDITOR_PATH, "POST", f, t("created", f.label.trim()))} />
      )}
      {editing?.kind === "edit" && (
        <RoleForm view={view} role={editing.role} busy={busy} onClose={onClose} onSubmit={(f) => edit(editing.role, f)} />
      )}
      <ConfirmModal open={archiving !== null} onClose={onClose} destructive busy={busy}
        title={t("archiveQ", archiving?.label ?? "")} confirmLabel={t("archive")} body={t("archiveBody")}
        testId="role-editor-archive-confirm"
        onConfirm={() => archiving && void run(rolePath(archiving), "PATCH", { archived: true },
          t("archivedDone", archiving.label))} />
    </>
  );
}

function RoleRow({ role, view, canEdit, busy, onEdit, onArchive, onRestore }: {
  role: EditorRole;
  view: RoleEditorView;
  canEdit: boolean;
  busy: boolean;
  onEdit: () => void;
  onArchive: () => void;
  onRestore: () => void;
}) {
  const t = useT(ROLE_EDITOR);
  const base = view.builtin.find((b) => b.code === role.based_on)?.label ?? role.based_on;
  return (
    <EditorRow testId={`role-editor-row-${role.code}`} dim={role.archived}
      title={<>
        <span style={{ fontSize: 13, fontWeight: 600, color: K.ink3 }}>{role.label}</span>
        <Badge tone={role.archived ? "neutral" : "success"}>{role.archived ? t("archived") : t("active")}</Badge>
      </>}
      details={<>
        <span style={{ fontFamily: mono }}>{role.code}</span> · {t("basedOn", base)} ·{" "}
        {t("capabilities", role.capabilities.length)} · {t("holders", role.holders)}
      </>}
      actions={canEdit && <RoleActions role={role} busy={busy} onEdit={onEdit} onArchive={onArchive} onRestore={onRestore} />} />
  );
}

/** Edit and Archive for an active role (Archive is off while an active account holds it), Restore for an archived one. */
function RoleActions({ role, busy, onEdit, onArchive, onRestore }: {
  role: EditorRole;
  busy: boolean;
  onEdit: () => void;
  onArchive: () => void;
  onRestore: () => void;
}) {
  const t = useT(ROLE_EDITOR);
  if (role.archived) {
    return (
      <Button size="sm" variant="ghost" testId={`role-editor-restore-${role.code}`} onClick={onRestore} disabled={busy}>
        {t("restore")}
      </Button>
    );
  }
  const held = role.holders > 0;
  return (
    <>
      <Button size="sm" variant="secondary" testId={`role-editor-edit-${role.code}`} onClick={onEdit} disabled={busy}>
        {t("edit")}
      </Button>
      <Button size="sm" variant="ghost" testId={`role-editor-archive-${role.code}`} onClick={onArchive}
        disabled={busy || held} title={held ? t("archiveBody") : undefined}>
        {t("archive")}
      </Button>
    </>
  );
}

interface RoleFormValue {
  code: string;
  label: string;
  based_on: string;
  capabilities: string[];
}

/** A form's first values: the role's own, or for a new role the first built-in role it may copy and its capabilities. */
function initialRoleForm(view: RoleEditorView, role?: EditorRole): { label: string; basedOn: string; capabilities: string[] } {
  if (role) return { label: role.label, basedOn: role.based_on, capabilities: role.capabilities };
  const basedOn = view.builtin.find((b) => b.clonable)?.code ?? "";
  return { label: "", basedOn, capabilities: cloneCapabilities(view, basedOn) };
}

/** Create (code, name, copy-from, capabilities) or edit (name, capabilities) one custom role. */
function RoleForm({ view, role, busy, onClose, onSubmit }: {
  view: RoleEditorView;
  role?: EditorRole;
  busy: boolean;
  onClose: () => void;
  onSubmit: (f: RoleFormValue) => Promise<void>;
}) {
  const t = useT(ROLE_EDITOR);
  const isNew = role === undefined;
  const start = initialRoleForm(view, role);
  const [code, setCode] = useState("custom_");
  const [label, setLabel] = useState(start.label);
  const [basedOn, setBasedOn] = useState(start.basedOn);
  const [caps, setCaps] = useState<string[]>(start.capabilities);
  const ok = roleFormOk({ code: isNew ? code : undefined, label, capabilities: caps });
  const pickBase = (b: string) => { setBasedOn(b); setCaps(cloneCapabilities(view, b)); };
  return (
    <Modal open onClose={onClose} width={620} pinFooter testId="role-editor-form"
      title={isNew ? t("createTitle") : t("editTitle", role.label)}
      footer={<>
        <Button variant="ghost" onClick={onClose} disabled={busy}>{t("cancel")}</Button>
        <Button variant="primary" testId="role-editor-submit" disabled={!ok || busy}
          onClick={() => void onSubmit({ code, label, based_on: basedOn, capabilities: caps })}>
          {busy ? t("saving") : t(isNew ? "create" : "save")}
        </Button>
      </>}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {isNew && <NewRoleFields code={code} onCode={setCode} basedOn={basedOn} onBase={pickBase} view={view} />}
        <Field label={t("label")} hint={t("labelHint")} required>
          <Input data-testid="role-editor-label" value={label} maxLength={80} autoComplete="off"
            onChange={(e) => setLabel(e.target.value)} />
        </Field>
        <CapabilityChecks view={view} value={caps} onChange={setCaps} />
      </div>
    </Modal>
  );
}

/** A new role's code and the built-in role it is copied from. */
function NewRoleFields({ code, onCode, basedOn, onBase, view }: {
  code: string;
  onCode: (v: string) => void;
  basedOn: string;
  onBase: (v: string) => void;
  view: RoleEditorView;
}) {
  const t = useT(ROLE_EDITOR);
  return (
    <>
      <Field label={t("code")} hint={t("codeHint")} required>
        <Input data-testid="role-editor-code" value={code} maxLength={54} autoComplete="off"
          invalid={code !== "custom_" && !CUSTOM_ROLE_CODE_RE.test(code)}
          onChange={(e) => onCode(e.target.value.trim().toLowerCase())} />
      </Field>
      <Field label={t("base")} hint={t("baseHint")}>
        <Select data-testid="role-editor-base" value={basedOn}
          options={view.builtin.filter((b) => b.clonable).map((b) => ({ value: b.code, label: b.label }))}
          onChange={(e) => onBase(e.target.value)} />
      </Field>
    </>
  );
}

/** The capabilities a custom role may hold, one kit checkbox each, grouped by area. */
function CapabilityChecks({ view, value, onChange }: {
  view: RoleEditorView;
  value: string[];
  onChange: (v: string[]) => void;
}) {
  const t = useT(ROLE_EDITOR);
  const toggle = (c: string, on: boolean) => onChange(on ? [...new Set([...value, c])] : value.filter((x) => x !== c));
  return (
    <Field as="div" label={t("capabilitiesTitle")} hint={t("capabilitiesHint")}>
      <div data-testid="role-editor-caps" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {assignableByArea(view).map(([area, names]) => (
          <div key={area} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span style={{ fontSize: 12, fontWeight: 600, color: K.muted, textTransform: "capitalize" }}>{area}</span>
            <div style={CHOICE_ROW}>
              {names.map((c) => (
                <Checkbox key={c} data-testid={`role-editor-cap-${c}`} label={<span style={{ fontFamily: mono }}>{c}</span>}
                  checked={value.includes(c)} onChange={(e) => toggle(c, e.target.checked)} style={{ fontSize: 12.5 }} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </Field>
  );
}
