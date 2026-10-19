"use client";
// portal/app/portal/dashboard/page.tsx — AA-430 route migration.
// Was the "dashboard" tab (default) in the old portal/page.tsx.
import { useRouter } from "next/navigation";
import DashboardTab from "../_components/DashboardTab";

export default function DashboardPage() {
  const router = useRouter();
  return <DashboardTab onNavigate={href => router.push(href)} />;
}
