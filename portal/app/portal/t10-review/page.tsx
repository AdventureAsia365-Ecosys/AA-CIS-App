"use client";
// portal/app/portal/t10-review/page.tsx — AA-501
//
// The screen that sits between T10's automatic quality gate and T11 publish. Route slug
// confirmed with Nghiệp (AA-501 STEP0 §6): "t10" is the one number in the existing
// t0/t1/t4/t6/t7/t8/t11 sequence with no page of its own (T9/T10 were folded into t8-angle-gate's
// wizard by AA-450) — this reads naturally as "the review of what T10 decided" and slots exactly
// where it belongs: between t8-angle-gate ("Write Content") and t11-publish ("Publish").
import { Suspense } from "react";
import { T, serif } from "../_components/ui";
import { ReviewList } from "../_components/ReviewList";
import { AiContentNotice } from "../_components/AiContentNotice";

export default function T10ReviewPage() {
  return (
    // AA-569 — was maxWidth:720 (the "hẹp/nhỏ" complaint); no cap now, matches PlanningTab.tsx/
    // t7-planning's own full-width convention (the portal layout's <main> has no width cap of
    // its own, so a page either sets one or fills the available flex space).
    //
    // AA-524 — this H1 is deliberately NOT sticky (tried it, reverted): `<main>`'s scroll
    // container gives every sticky descendant the SAME top:0 offset regardless of nesting depth,
    // so a sticky page header here + ReviewList's own sticky CardHead/filter bar would both stick
    // at the identical position and visually collide. ReviewList's own StickyBar (the one that
    // actually matters for "which channel am I filtered to" while scrolling a long list) covers
    // the real complaint; the breadcrumb above already shows the page name while scrolled.
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div>
        <h1 style={{ fontFamily: serif, fontSize: 24, fontWeight: 500, color: T.ink, margin: "0 0 6px" }}>
          My Content
        </h1>
        <p style={{ fontSize: 13, color: T.muted, margin: 0, lineHeight: 1.5 }}>
          See everything you&rsquo;ve written — where it came from and whether it&rsquo;s ready —
          before you publish.
        </p>
        {/* AA-819 — AUP notice; covers the list's bulk export/publish too. */}
        <AiContentNotice style={{ marginTop: 8 }} />
      </div>

      {/* AA-569 — ReviewList now reads useSearchParams() (?piece= from the write-done popup's
          "Open in My Content" link) — needs a Suspense boundary or `next build` fails
          prerendering this route, same pattern t8-angle-gate/page.tsx already established. */}
      <Suspense>
        <ReviewList />
      </Suspense>
    </div>
  );
}
