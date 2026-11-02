# AA-1006 — /admin/upload stepper at 390 px (S264, lane l4-ui-fixes-2)

The S264 UI check found `/admin/upload` wider than a phone: the page frame (`main.aa-admin-main`) measured 675 px in a
390 px viewport. The cause is the five-step stepper at the top of the page (`StepIndicator` in
`frontend/app/admin/upload/page.tsx`): five labelled pills (14 px side padding, 12 px nowrap label) plus 20 px
connectors in one nowrap flex row, about 675 px whatever the screen.

## Decisions

- **Wrap, not scroll.** The kit `ScrollX` would keep the page width but hide steps (often the current one) off to the
  right. The row is now a flex-wrap list (`<ol>`, `min-width: 0`, `max-width: 100%`), so it never grows wider than its
  container and every step stays visible.
- **Phone: only the current step shows its label.** The other steps are numbered (or ticked) dots with
  `aria-label` + `title` set to their label, so a screen reader and a long press still name them; the current step
  carries `aria-current="step"`. Pills use 8 px side padding and 8 px connectors on a phone. Measured on a replica in
  Chromium: one 34 px line inside 358 px for EN and VI ("Phân tích & xem lại" is the longest active label) at every
  step; at 768 px (not a phone, labels shown) the EN row wraps to two lines instead of overflowing; 1440 px unchanged.
- **Kit only**: `useIsPhone`, `K` / `KX` tokens, no kit core change (no kit sync PR in other repos).

## Changed

- Nothing beyond the stepper. Desktop look is the same (labels, colours, 20 px connectors); the row is an `<ol>`
  of `<li>` instead of fragments (list semantics for the steps).

## Tradeoffs

- Hiding the labels of the other steps on a phone loses some at-a-glance context; the alternative (two or three
  wrapped rows of full pills) pushes the step content down by ~80 px on every step. The numbers and the tick for
  done steps keep the progress readable.

## Should know

- Guards: `tests/unit/test_aa1006_upload_stepper.py` (pytest, runs in CI) and the same checks in
  `frontend/lib/uploadStepper.test.mjs` (`npm run test:unit`, local).
- The staff UI smoke (`tests/e2e/smoke`, `assertMobileLayout`) checks the document scroll width at 390 px; the live
  check for this fix also reads the width of `main.aa-admin-main` (it was the frame, not the document, that grew).
