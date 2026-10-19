// portal/app/portal/page.tsx — AA-430 route migration.
// /portal (no sub-path) used to render the whole shell + default to the Dashboard tab.
// Now every former tab is its own route (see the sibling directories) — this file is
// just the redirect for anyone landing on the bare /portal URL (old bookmarks, the
// tenant-login page's `router.push("/portal")`, etc). Dashboard was the default tab
// before (`useState<ExtTab>("dashboard")` in the old page.tsx), kept as the default here.
import { redirect } from "next/navigation";

export default function PortalRootPage() {
  redirect("/portal/dashboard");
}
