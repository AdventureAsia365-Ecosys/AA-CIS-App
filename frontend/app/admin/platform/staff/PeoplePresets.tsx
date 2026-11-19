"use client";
// app/admin/platform/staff/PeoplePresets.tsx — AA-1150 (lean staff roles step 1): the three people-role presets of the
// account panel ("Administrator" = the platform_owner preset, "Business owner", "Content + Product & operations"), one
// row each with what it gives and "Current" or Apply, and their confirm dialog. StaffManage.tsx sends the preset
// (PATCH …/roles {"preset": …}); the backend (api/services/staff_presets.py) is the authority.

import { useMemo } from "react";
import { Button, ConfirmModal, K, RADIUS, useT } from "../../../_kit";
import { PEOPLE_PRESETS, PEOPLE_PRESET_ROLES, peoplePresetOf, type PeoplePreset } from "@/lib/staffPresets";
import { MANAGE } from "./messages";
import type { StaffAccount } from "./StaffManage";

const PRESET_WORDS = {
  platform_owner: { title: "adminTitle", note: "adminNote" },
  business_owner: { title: "ownerTitle", note: "ownerNote" },
  content_ops: { title: "contentOpsTitle", note: "contentOpsNote" },
} as const;

/** A people-role preset's name and what it gives, in the UI language. */
export function usePresetWords() {
  const m = useT(MANAGE);
  return useMemo(() => ({
    presetTitle: (p: PeoplePreset) => m(PRESET_WORDS[p].title),
    presetNote: (p: PeoplePreset) => m(PRESET_WORDS[p].note),
  }), [m]);
}

/** AA-1150: a preset that writes CRM roles (Administrator adds Agency admin, Content + Product & operations removes
 *  them) needs the CRM table; Business owner leaves the CRM roles alone. */
function presetNeedsCrm(p: PeoplePreset): boolean {
  return PEOPLE_PRESET_ROLES[p].crm !== null;
}

/** AA-1150: the three people-role presets, one row each: what it gives, then "Current" or Apply. Content + Product &
 *  operations demotes, so it is not offered on one's own row (the backend refuses a self-demotion too). */
export function PeoplePresets({ staff, isSelf, busy, crmAvailable, onApply }: {
  staff: StaffAccount;
  isSelf: boolean;
  busy: boolean;
  crmAvailable: boolean;
  onApply: (p: PeoplePreset) => void;
}) {
  const m = useT(MANAGE);
  const current = peoplePresetOf(staff);
  return (
    <div role="group" aria-label={m("rolesTitle")} data-testid="staff-presets"
      style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        <span style={{ fontSize: 12.5, fontWeight: 600, color: K.ink3 }}>{m("rolesTitle")}</span>
        <span style={{ fontSize: 12, color: K.muted, lineHeight: 1.45 }}>{m("rolesNote")}</span>
      </span>
      {PEOPLE_PRESETS.map((p) => (
        <PresetRow key={p} preset={p} current={current === p} busy={busy} onApply={onApply}
          why={presetBlockedWhy(p, isSelf, crmAvailable)} />
      ))}
    </div>
  );
}

/** Why a preset cannot be applied to this row, or null: Content + Product & operations would demote oneself; a preset
 *  that writes CRM roles needs the CRM table. */
function presetBlockedWhy(p: PeoplePreset, isSelf: boolean, crmAvailable: boolean): "ownRole" | "crmUnavailable" | null {
  if (isSelf && p === "content_ops") return "ownRole";
  if (presetNeedsCrm(p) && !crmAvailable) return "crmUnavailable";
  return null;
}

/** One people-role preset: what it gives, then "Current" or Apply. */
function PresetRow({ preset, current, busy, why, onApply }: {
  preset: PeoplePreset;
  current: boolean;
  busy: boolean;
  why: "ownRole" | "crmUnavailable" | null;
  onApply: (p: PeoplePreset) => void;
}) {
  const m = useT(MANAGE);
  const { presetTitle, presetNote } = usePresetWords();
  return (
    <div data-testid={`staff-preset-${preset}`} style={{
      display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", padding: "10px 12px",
      background: current ? K.successSoft : K.line2, borderRadius: RADIUS.sm,
    }}>
      <span style={{ flex: "1 1 220px", minWidth: 0, display: "flex", flexDirection: "column", gap: 2 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: K.ink3 }}>{presetTitle(preset)}</span>
        <span style={{ fontSize: 12, color: K.muted, lineHeight: 1.45 }}>{presetNote(preset)}</span>
      </span>
      {current ? (
        <span data-testid={`staff-preset-${preset}-current`} style={{ fontSize: 12.5, fontWeight: 600, color: K.success }}>
          {m("presetCurrent")}
        </span>
      ) : (
        <Button size="sm" variant="secondary" testId={`staff-preset-${preset}-apply`} onClick={() => onApply(preset)}
          disabled={busy || why !== null} title={why ? m(why) : undefined}>
          {m("presetApply")}
        </Button>
      )}
    </div>
  );
}

/** AA-1150: confirm one people-role preset (what it gives; Content + Product & operations replaces roles). */
export function PresetConfirm({ staff, preset, busy, onClose, onConfirm }: {
  staff: StaffAccount;
  preset: PeoplePreset | null;
  busy: boolean;
  onClose: () => void;
  onConfirm: (p: PeoplePreset, done: string) => void;
}) {
  const m = useT(MANAGE);
  const { presetTitle, presetNote } = usePresetWords();
  const name = preset ? presetTitle(preset) : "";
  return (
    <ConfirmModal
      open={preset !== null}
      onClose={onClose}
      busy={busy}
      title={m("presetQ", name)}
      confirmLabel={m("presetApply")}
      body={preset && <>
        <strong>{staff.display_name || staff.username}</strong> {m("presetBody")} {presetNote(preset)}
        {preset === "content_ops" && m("presetReplaces")}
      </>}
      onConfirm={() => preset && onConfirm(preset, m("presetDone", name))}
    />
  );
}
