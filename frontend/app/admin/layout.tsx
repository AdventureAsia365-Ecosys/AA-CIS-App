// app/admin/layout.tsx
// AA-752 — one shared admin shell. Every page under /admin renders inside the AdminShell
// (collapsible sidebar + topbar + ⌘K command palette + phone bottom nav); no page renders its own
// sidebar any more. Each page renders the kit AdminMain exactly once as its frame and scroll region (S252;
// variant "split" for a sticky header band; guarded by lib/adminFrame.test.mjs). The frame stays per page because
// only the page knows whether it needs a split header.
import { AdminShell } from "../_kit/AdminShell";

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return <AdminShell>{children}</AdminShell>;
}
