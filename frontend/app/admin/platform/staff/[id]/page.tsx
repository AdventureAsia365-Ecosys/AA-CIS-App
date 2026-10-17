"use client";
// app/admin/platform/staff/[id]/page.tsx — AA-883: deep link to one staff account. Renders the same
// directory with that account's drawer open (Booking's "managed in CIS" link can point here, §6.3).
import { useParams } from "next/navigation";
import StaffDirectory from "../StaffDirectory";

export default function StaffAccountPage() {
  const params = useParams<{ id: string }>();
  const id = typeof params?.id === "string" ? decodeURIComponent(params.id) : null;
  return <StaffDirectory key={id ?? ""} initialId={id} />;
}
