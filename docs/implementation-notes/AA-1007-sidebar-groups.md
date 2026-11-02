# AA-1007 (sidebar lane, S264): collapsible nav groups in every app's sidebar

Tech lead request (S264): in the CIS admin sidebar only the two sections (Platform, CIS Content) fold; the groups
under them (Overview, People & access, Pipeline, Assets…) are always expanded and the menu looks cluttered. Same in
the other apps. Built once in the kit core and used by every app's sidebar (CIS admin, Booking admin, CRM web,
Content studio portal). Refs AA-1007 (and AA-1006 for the CIS part).

## Decisions

- **Store in core `sidebarStore.ts`, hook in core `useSidebar.ts`, component in new core `NavGroup.tsx`.**
  One localStorage key per app: `aa_nav_groups:<scope>` (scopes `cis-admin`, `booking`, `crm-web`, `portal`), value a
  sorted comma list `<group id>=open|closed`. Group id = slug of the English label (`navGroupId`). Same
  useSyncExternalStore model as the collapsed rail and the CIS section fold: raw string snapshot, server snapshot `""`.
- **Open rule** (`resolveNavGroupOpen`): the group holding the current page is open on every arrival; every other
  group follows the stored choice and is folded when nothing is stored. The user can still fold the active group:
  that is kept in component state keyed by the route, so it holds on this page and is forgotten on navigation
  (no effect, React-Compiler safe). The fold of the active group is also stored, so it stays folded once the user
  leaves its pages.
- **Folded header carries the item counts**: `badge` = sum of the item badges (CIS: the Review Queue pending count on
  Pipeline), shown as a pill only while folded; the header turns accent while folded with the active page in it.
- **Icon rail** (`rail` prop): no header, every item shown, folded or not (the rail has no title to unfold with) —
  same rule the CIS section fold already used. Each app passes its own rail condition.
- **Untitled groups** (`label` null: a one-item group named like its item, the portal Overview, CRM Home/Settings)
  have no header and are always open.
- Items of a folded group stay in the DOM with `hidden` (so `aria-controls` always resolves); inline `display` is only
  set while shown, because an inline display would beat the `hidden` attribute.
- With localStorage blocked the state lives in memory for the page's lifetime (toggle still works, no persistence).
- CIS keeps its section fold (`useNavSections`, `cis_nav_closed`) unchanged; groups fold inside a section.

## Changed

- Core: `sidebarStore.ts` (group store), `useSidebar.ts` (`useNavGroups`), `NavGroup.tsx` (new: `NavGroup`,
  `NavGroupHeader`, `navBadgeText`), `kit.messages.ts` (`showGroup`, `hideGroup`, `groupBadge`, EN + VI),
  `MANIFEST.json` regenerated.
- CIS: `_kit/Sidebar.tsx` renders each group with the kit `NavGroup` (scope `cis-admin`); `_kit/index.ts` exports the
  new pieces.
- Tests: `frontend/lib/kitNavGroup.test.mjs` (store, open rule, header ARIA, badge, rail, SSR snapshot, toggle
  persistence); smoke `tests/e2e/smoke/aa752-shell.spec.ts` (f).

## Tradeoffs

- Default folded (except the active group) gives the short menu the tech lead asked for; the cost is one click to
  open a group the first time. Opened groups are remembered per browser.
- No pre-paint restore script for groups (unlike the rail width): SSR renders the server snapshot, so a group the
  user opened earlier appears open right after hydration (a small height change in the sidebar, no width change).
  A restore script would need per-app CSS for every group; not worth it for a sidebar list.
- The header is a new core component rather than restyling each app's title: one look and one behaviour everywhere,
  apps only pass spacing (`headerStyle`) and CSS hooks (`headerProps`).

## Should know

- Consumer repos get the core through the CIS Kit Sync bot after this PR merges; each consumer's wiring PR must be
  rebased onto its merged `bot/kit-sync` PR (lesson S263: a consumer PR using a new core file opens only after the
  sync is on its main).
- Group ids derive from the English labels in each app's nav model: renaming a group label resets its saved state
  (harmless: it falls back to folded / active-open).
