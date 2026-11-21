"use client";
// app/admin/platform/staff/PeoplePresets.tsx — the people-role presets of the account panel (AA-1150), one row each
// with what it gives and "Current" or Apply, and their confirm dialog. StaffManage.tsx sends the preset
// (PATCH …/roles {"preset": code}); the backend (api/services/staff_presets.py) is the authority.
//
// AA-1155 (spec task T14, root spec R5.7): the presets are data. The rows, their names and descriptions come from
// GET /api/admin/platform/presets (shared.staff_role_preset, edited by an owner in the "People-role presets" section
// of this page); lib/staffPresets.ts plans them on the account like the backend does. No preset is named in code.

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { Button, ConfirmModal, K, RADIUS, apiGet, useT } from "../../../_kit";
import {
  PRESETS_PATH, activePresets, currentPreset, planChanges, presetBlockedWhy, presetPlan, type PresetList,
  type StaffPreset,
} from "@/lib/staffPresets";
import { MANAGE } from "./messages";
import { useStaffWords, type StaffAccount } from "./StaffManage";

export const PRESETS_KEY = ["platform-presets"] as const;

/** AA-1155 T14: the presets from the API (every row, archived included; `available` false before CIS 465). */
export function usePresets() {
  return useQuery({ queryKey: PRESETS_KEY, queryFn: () => apiGet<PresetList>(PRESETS_PATH), staleTime: 60_000 });
}

/** AA-1155 T14: the active presets the Staff page offers, by sort order. */
export function useActivePresets(): { presets: StaffPreset[]; available: boolean; loading: boolean } {
  const q = usePresets();
  return useMemo(() => ({
    presets: activePresets(q.data?.presets),
    available: q.data?.available ?? false,
    loading: q.isLoading,
  }), [q.data, q.isLoading]);
}

/** The people-role presets, one row each: what it gives, then "Current" or Apply (disabled with the reason when the
 *  preset would demote oneself, replace a held role, or needs the CRM table). */
export function PeoplePresets({ staff, role, isSelf, busy, crmAvailable, onApply }: {
  staff: StaffAccount;
  /** The account's CIS role code (its custom role for a custom holder). */
  role: string;
  isSelf: boolean;
  busy: boolean;
  crmAvailable: boolean;
  onApply: (p: StaffPreset) => void;
}) {
  const m = useT(MANAGE);
  const { presets, available, loading } = useActivePresets();
  const held = { ...staff, role };
  const current = currentPreset(presets, held);
  return (
    <div role="group" aria-label={m("rolesTitle")} data-testid="staff-presets"
      style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        <span style={{ fontSize: 12.5, fontWeight: 600, color: K.ink3 }}>{m("rolesTitle")}</span>
        <span style={{ fontSize: 12, color: K.muted, lineHeight: 1.45 }}>
          {loading ? m("presetsLoading") : available ? m("rolesNote") : m("presetsUnavailable")}
        </span>
      </span>
      {presets.map((p) => (
        <PresetRow key={p.code} preset={p} current={current?.code === p.code} busy={busy} onApply={onApply}
          why={presetBlockedWhy(p, held, isSelf, crmAvailable)} held={held} />
      ))}
    </div>
  );
}

/** One people-role preset: its name and description, then "Current" or Apply. */
function PresetRow({ preset, current, busy, why, held, onApply }: {
  preset: StaffPreset;
  current: boolean;
  busy: boolean;
  why: "ownRole" | "wouldReplace" | "crmUnavailable" | null;
  held: StaffAccount;
  onApply: (p: StaffPreset) => void;
}) {
  const m = useT(MANAGE);
  const { roleLabel, bookingRoleLabel } = useStaffWords();
  const blocked = why === "wouldReplace"
    ? m("presetBlocked", presetPlan(preset, held).lost
      .map((l) => (l.app === "cis" ? m("presetCisRole", roleLabel(l.role)) : m("presetBookingRole", bookingRoleLabel(l.role))))
      .join(", "))
    : null;
  return (
    <div data-testid={`staff-preset-${preset.code}`} style={{
      display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", padding: "10px 12px",
      background: current ? K.successSoft : K.line2, borderRadius: RADIUS.sm,
    }}>
      <span style={{ flex: "1 1 220px", minWidth: 0, display: "flex", flexDirection: "column", gap: 2 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: K.ink3 }}>{preset.label}</span>
        <span style={{ fontSize: 12, color: K.muted, lineHeight: 1.45 }}>{blocked ?? preset.description}</span>
      </span>
      {current ? (
        <span data-testid={`staff-preset-${preset.code}-current`} style={{ fontSize: 12.5, fontWeight: 600, color: K.success }}>
          {m("presetCurrent")}
        </span>
      ) : (
        <Button size="sm" variant="secondary" testId={`staff-preset-${preset.code}-apply`} onClick={() => onApply(preset)}
          disabled={busy || why !== null} title={why === "ownRole" || why === "crmUnavailable" ? m(why) : undefined}>
          {m("presetApply")}
        </Button>
      )}
    </div>
  );
}

/** Confirm one people-role preset: each role it changes on this account (from → to). */
export function PresetConfirm({ staff, role, preset, busy, onClose, onConfirm }: {
  staff: StaffAccount;
  role: string;
  preset: StaffPreset | null;
  busy: boolean;
  onClose: () => void;
  onConfirm: (p: StaffPreset, done: string) => void;
}) {
  const m = useT(MANAGE);
  const { roleLabel, bookingRoleLabel, crmRolesLabel } = useStaffWords();
  const held = { ...staff, role };
  const changes = preset ? planChanges(presetPlan(preset, held), held) : {};
  const replaces = !!preset && !preset.additive_only && Object.keys(changes).length > 0;
  return (
    <ConfirmModal
      open={preset !== null}
      onClose={onClose}
      busy={busy}
      title={m("presetQ", preset?.label ?? "")}
      confirmLabel={m("presetApply")}
      body={preset && <>
        <strong>{staff.display_name || staff.username}</strong> {m("presetBody")}
        {changes.role && m("cisChange", roleLabel(changes.role.from), roleLabel(changes.role.to))}
        {changes.booking && m("bookingChange", bookingRoleLabel(changes.booking.from), bookingRoleLabel(changes.booking.to))}
        {changes.crm && m("crmChange", crmRolesLabel(changes.crm.from), crmRolesLabel(changes.crm.to))}
        {replaces && m("presetReplaces")}
      </>}
      onConfirm={() => preset && onConfirm(preset, m("presetDone", preset.label))}
    />
  );
}
