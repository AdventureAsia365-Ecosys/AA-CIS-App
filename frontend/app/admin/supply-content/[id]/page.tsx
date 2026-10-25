"use client";
// app/admin/supply-content/[id]/page.tsx — AA-835: one Booking hotel or service (SupplyContentDetail.tsx). The path
// catalog.v_supply_content.edit_url names (CIS 278), so Booking's "Edit in CIS" link lands here.
import { useParams } from "next/navigation";
import { ToastProvider } from "../../../_kit";
import SupplyContentDetail from "../SupplyContentDetail";

export default function SupplyContentEntityPage() {
  const params = useParams<{ id: string }>();
  const id = typeof params?.id === "string" ? decodeURIComponent(params.id) : "";
  return (
    <ToastProvider>
      <SupplyContentDetail key={id} entityId={id} />
    </ToastProvider>
  );
}
