// app/admin/layout.tsx
// AA-752 — one shared admin shell. Every page under /admin renders inside the AdminShell
// (collapsible sidebar + topbar + ⌘K command palette + phone bottom nav); no page renders its own
// sidebar any more. Each page still supplies its own <main className="aa-admin-main"> as the scroll
// region (kept from before the shell).
import { AdminShell } from "../_kit/AdminShell";

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return <AdminShell>{children}</AdminShell>;
}
