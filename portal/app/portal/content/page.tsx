"use client";
// portal/app/portal/content/page.tsx — My Content (AA-501 / AA-614): every piece the tenant wrote,
// where it came from and whether it is ready, before publishing.
// AA-759: was /portal/t10-review (308 from the old slug, next.config.ts). Publish is an action of
// this page now (header button → /portal/content/publish), not its own nav item.
import { Suspense } from "react";
import Link from "next/link";
import { Send } from "lucide-react";
import { K, PageHeader, sans } from "@/app/_kit";
import { ReviewList } from "../_components/ReviewList";
import { AiContentNotice } from "../_components/AiContentNotice";
import { ROUTES } from "../_shell/portalNav";

export default function MyContentPage() {
  return (
    // AA-524 — the header is deliberately NOT sticky: ReviewList's own StickyBar is the one sticky
    // point (a second sticky header would collide at the same offset inside <main>).
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <PageHeader
        title="My Content"
        description={<>
          See everything you&rsquo;ve written — where it came from and whether it&rsquo;s ready — before you publish.
          {/* AA-819 — AUP notice; covers the list's bulk export/publish too. */}
          <AiContentNotice style={{ marginTop: 8 }} />
        </>}
        actions={
          <Link href={ROUTES.publish} data-testid="my-content-publish" style={{
            display: "inline-flex", alignItems: "center", gap: 6, padding: "8px 16px", borderRadius: 999,
            background: K.accent, color: "var(--aa-on-accent)", fontSize: 12.5, fontWeight: 600,
            textDecoration: "none", fontFamily: sans, textTransform: "uppercase", letterSpacing: "0.06em",
          }}>
            <Send size={13} aria-hidden /> Publish
          </Link>
        }
      />

      {/* ReviewList reads useSearchParams() (?piece= from the write-done popup and ⌘K) — needs a
          Suspense boundary or `next build` fails prerendering this route. */}
      <Suspense>
        <ReviewList />
      </Suspense>
    </div>
  );
}
