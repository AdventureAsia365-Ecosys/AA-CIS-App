// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
// app/_kit/core/PageHeaderStyles.tsx — the kit PageHeader's (primitives.tsx) layout rules, hoisted once.
// AA-1131 (S283 UI audit, batch K1): the header's layout rules are the kit's own hoisted stylesheet (they used to be
// copied into each app's CSS: CIS globals.css, Booking ui/local/shell.css, the studio css). A header with a description
// AND actions gives its text a 420 px basis (it was 240 px): with a long action row that just fitted beside a 240 px
// text, the description was squeezed into a 7-line column (CIS Writing memory at 1440 px); now the actions wrap under
// the text instead. Below 768 px such a header stacks (text, then the actions on their own row, left aligned). The
// selectors match the app copies' specificity and come in the same order, so an app copy left in place changes nothing.
export const PAGE_HEADER_CSS =
  `.aa-page-header-text{flex:1 1 240px}` +
  `.aa-page-header-actions{justify-content:flex-end;max-width:100%}` +
  `.aa-page-header--stack>.aa-page-header-text{flex:1 1 420px}` +
  `@media (max-width: 767px){` +
  `.aa-page-header--stack>.aa-page-header-text{flex-basis:100%}` +
  `.aa-page-header--stack>.aa-page-header-actions{justify-content:flex-start;flex-basis:100%;min-width:0}` +
  `}`;

export function PageHeaderStyles() {
  return <style href="aa-kit-page-header" precedence="default">{PAGE_HEADER_CSS}</style>;
}
