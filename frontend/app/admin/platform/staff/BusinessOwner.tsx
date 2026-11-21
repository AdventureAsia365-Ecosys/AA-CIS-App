"use client";
// app/admin/platform/staff/BusinessOwner.tsx — AA-1155 (spec task T9, R3.3): the business-owner marker on the Staff
// page's manage panel. At most one staff member holds it; an owner sets or clears it (PUT
// /api/admin/platform/staff/{id}/business-owner, owners only, audited before and after by the backend,
// api/services/staff_custom_roles.py). Kit components only.

import { useState } from "react";
import { Button, ConfirmModal, K, RADIUS, useT } from "../../../_kit";
import { MANAGE } from "./messages";
import type { StaffAccount } from "./StaffManage";

/** AA-1155 (R3.3): the business-owner marker of this account (at most one person; set by an owner, audited): the
 *  row and its confirmation. Shown for an active account, or one that still holds the marker (to clear it). */
export function BusinessOwnerControl({ staff, busy, onRun }: {
  staff: StaffAccount;
  busy: string | null;
  onRun: (okMessage: string, body: { value: boolean }) => Promise<void>;
}) {
  const m = useT(MANAGE);
  const [asking, setAsking] = useState(false);
  if (!staff.is_active && !staff.is_business_owner) return null;
  const holds = !!staff.is_business_owner;
  const confirm = async () => {
    await onRun(holds ? m("businessOwnerCleared") : m("businessOwnerDone"), { value: !holds });
    setAsking(false);
  };
  return (
    <>
      <BusinessOwnerRow holds={holds} busy={!!busy} onAsk={() => setAsking(true)} />
      <ConfirmModal open={asking} onClose={() => setAsking(false)} busy={busy === "business-owner"}
        title={holds ? m("businessOwnerClearQ") : m("businessOwnerSetQ")}
        confirmLabel={holds ? m("businessOwnerClear") : m("businessOwnerSet")}
        body={<><strong>{staff.display_name || staff.username}</strong>{" "}
          {holds ? m("businessOwnerClearBody") : m("businessOwnerSetBody")}</>}
        onConfirm={() => void confirm()} />
    </>
  );
}

function BusinessOwnerRow({ holds, busy, onAsk }: { holds: boolean; busy: boolean; onAsk: () => void }) {
  const m = useT(MANAGE);
  return (
    <div data-testid="staff-business-owner" style={{
      display: "flex", gap: 10, alignItems: "flex-start", flexWrap: "wrap", padding: "10px 12px", background: K.line2,
      borderRadius: RADIUS.sm,
    }}>
      <span style={{ flex: "1 1 220px", minWidth: 0, display: "flex", flexDirection: "column", gap: 2 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: K.ink3 }}>{m("businessOwnerTitle")}</span>
        <span style={{ fontSize: 12, color: K.muted, lineHeight: 1.45 }}>
          {holds ? m("businessOwnerIs") : m("businessOwnerNote")}
        </span>
      </span>
      <Button size="sm" variant="secondary" testId="staff-business-owner-toggle" onClick={onAsk} disabled={busy}>
        {holds ? m("businessOwnerClear") : m("businessOwnerSet")}
      </Button>
    </div>
  );
}
