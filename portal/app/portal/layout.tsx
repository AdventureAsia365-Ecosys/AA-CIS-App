// portal/app/portal/layout.tsx
// AA-430 made every portal "tab" its own route under /portal/*, with the shell state in this layout
// (it stays mounted across sibling routes, so nothing refetches on navigation).
// AA-759 — the shell is now one kit-based component, `_shell/PortalShell.tsx` (grouped nav, ⌘K,
// phone bottom bar, theme switch, kit toast, shell data through react-query). This file only mounts
// it; pages still read the shell state through usePortalShell() (PortalShellContext).
import { PortalShell } from "./_shell/PortalShell";

export default function PortalLayout({ children }: { children: React.ReactNode }) {
  return <PortalShell>{children}</PortalShell>;
}
