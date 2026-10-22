"use client";
// portal/app/portal/tours/page.tsx — Browse Tours (T1 — tour selection; a rewrite runs T2–T5).
// AA-759: was /portal/t1-rewrite (308 from the old slug, next.config.ts).
import { useRouter } from "next/navigation";
import { PageHeader } from "@/app/_kit";
import PoolTab from "../_components/PoolTab";
import { usePortalShell } from "../_components/PortalShellContext";
import { ROUTES } from "../_shell/portalNav";

export default function BrowseToursPage() {
  const router = useRouter();
  const { globalSearch, showToast, refreshCatalogCount } = usePortalShell();

  function handleRewriteDone() {
    showToast("Rewrite started — follow it in My Tours.");
    refreshCatalogCount();
    router.push(ROUTES.myTours);
  }

  return (
    <>
      <PageHeader title="Browse Tours"
        description="Published Adventure Asia tours. Pick one and rewrite it in your brand voice — it lands in My Tours." />
      {/* AA-760 — remount on a new topbar / ⌘K search instead of syncing it in an effect. */}
      <PoolTab key={globalSearch} onRewriteDone={handleRewriteDone} externalSearch={globalSearch} />
    </>
  );
}
