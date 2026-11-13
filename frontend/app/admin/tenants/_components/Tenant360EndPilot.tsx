"use client";
// app/admin/tenants/_components/Tenant360EndPilot.tsx — AA-1086 G2 (root ADR 0019 d16): the "End pilot" button next to
// the pilot badge in Tenant 360 › Subscription. Admin only (tenant.write); renders nothing for a tenant that is not
// the pilot. It asks for a reason in a kit ConfirmModal and calls POST /api/admin/tenants/{id}/pilot/end, which clears
// the pilot marker (audited); the subscription snapshot is then reloaded. Kit only (Button, ConfirmModal, Toast).

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button, ConfirmModal, useT, useToast } from "../../../_kit";
import { END_PILOT_REASON_MAX, endPilotPath, sendJson, subscriptionKey, type EndPilotResp } from "@/lib/tenantPlans";
import { can } from "@/lib/rbac";
import { useAdminIdentity } from "../../../_kit/useAdminIdentity";
import { T360_SUB } from "./subscription.messages";

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function Tenant360EndPilot({ tenantId, tenantName, isPilot }: {
  tenantId: string; tenantName: string; isPilot: boolean;
}) {
  const t = useT(T360_SUB);
  const toast = useToast();
  const qc = useQueryClient();
  const { role } = useAdminIdentity();
  const [open, setOpen] = useState(false);
  const endPilot = useMutation({
    mutationFn: (reason: string) => sendJson<EndPilotResp>(endPilotPath(tenantId), "POST", { reason }),
    onSuccess: (r) => {
      setOpen(false);
      void qc.invalidateQueries({ queryKey: subscriptionKey(tenantId) });
      toast.success(r.changed ? t("endPilotDone") : t("endPilotNoChange"));
    },
  });

  if (!isPilot || !can(role, "tenant.write")) return null;
  return (
    <>
      <Button size="sm" variant="secondary" testId="end-pilot"
        onClick={() => { endPilot.reset(); setOpen(true); }}>{t("endPilot")}</Button>
      <ConfirmModal open={open} onClose={() => setOpen(false)}
        onConfirm={(reason) => endPilot.mutate(reason)}
        title={t("endPilotTitle", tenantName)} body={t("endPilotBody")} confirmLabel={t("endPilot")} destructive
        reason={{ label: t("endPilotReason"), required: true, maxLength: END_PILOT_REASON_MAX }}
        busy={endPilot.isPending}
        error={endPilot.error ? t("failed", errText(endPilot.error)) : null}
        testId="end-pilot-confirm" />
    </>
  );
}
