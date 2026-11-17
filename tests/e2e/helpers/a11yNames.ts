// tests/e2e/helpers/a11yNames.ts
// AA-1131 (S283 UI audit, batch K1) — the role-name check: every visible form control, dialog and icon button on the
// page must have an accessible name, or a screen reader announces only "combobox, 25 / page" (the audit's most
// common finding: 31 a11y rows, most of them the kit DataTable page-size select). Dependency-free (no axe package: the
// visual job installs Playwright only); it runs in the page and follows the parts of the accessible-name computation
// these controls use: aria-labelledby, aria-label, a <label> (for= or wrapping), title, and for a text field its
// placeholder; for a button its text (an icon's <title> / alt counts). Used by the visual job (visual.spec.ts) and
// reusable by any audit script: `const unnamed = await unnamedControls(page)`.
//
// `kit` marks a control the kit core renders itself (a `data-kit-*` hook on it or on its dialog): those must always be
// named, on every page; a page's own unnamed filter is reported but fails only on the kit gallery.
import type { Page } from '@playwright/test';

export type UnnamedControl = {
  /** tag + role + the first hooks, e.g. `select[data-kit-page-size]` */
  what: string;
  /** Short visible text (the selected option, the button glyph), to find it on the page. */
  text: string;
  /** Rendered by the kit core (a data-kit-* hook on it, or it is a kit drawer). */
  kit: boolean;
};

export async function unnamedControls(page: Page): Promise<UnnamedControl[]> {
  return page.evaluate(() => {
    const SELECTOR = [
      'select', 'textarea', 'input:not([type="hidden"])', 'button', '[role="combobox"]', '[role="listbox"]',
      '[role="dialog"]', '[role="alertdialog"]', '[role="button"]', '[role="switch"]', '[role="checkbox"]',
    ].join(', ');
    const visible = (el: Element) => {
      if (el.closest('[aria-hidden="true"], [hidden], [inert]')) return false;
      const r = (el as HTMLElement).getBoundingClientRect();
      if (r.width === 0 && r.height === 0) return false;
      const cs = getComputedStyle(el);
      return cs.visibility !== 'hidden' && cs.display !== 'none';
    };
    const textOf = (el: Element | null): string => {
      if (!el) return '';
      const own = (el as HTMLElement).innerText ?? el.textContent ?? '';
      const alts = [...el.querySelectorAll('img[alt], svg title')].map((n) =>
        n.tagName.toLowerCase() === 'img' ? (n as HTMLImageElement).alt : n.textContent ?? '');
      return `${own} ${alts.join(' ')}`.trim();
    };
    const name = (el: Element): string => {
      const ids = el.getAttribute('aria-labelledby');
      if (ids) {
        const t = ids.split(/\s+/).map((id) => textOf(document.getElementById(id))).join(' ').trim();
        if (t) return t;
      }
      const label = el.getAttribute('aria-label')?.trim();
      if (label) return label;
      const labels = (el as HTMLInputElement).labels;
      if (labels && labels.length) {
        const t = [...labels].map((l) => textOf(l)).join(' ').trim();
        if (t) return t;
      }
      const tag = el.tagName.toLowerCase();
      if (tag === 'button' || el.getAttribute('role') === 'button') {
        const t = textOf(el);
        if (t) return t;
      }
      if (tag === 'input' && ['submit', 'button', 'reset'].includes((el as HTMLInputElement).type)) {
        if ((el as HTMLInputElement).value) return (el as HTMLInputElement).value;
      }
      if ((tag === 'input' || tag === 'textarea') && el.getAttribute('placeholder')?.trim()) {
        return el.getAttribute('placeholder')!.trim();
      }
      return el.getAttribute('title')?.trim() ?? '';
    };
    const hooks = (el: Element) =>
      [...el.attributes].filter((a) => a.name.startsWith('data-kit') || a.name === 'data-testid').slice(0, 2)
        .map((a) => (a.value ? `[${a.name}=${a.value}]` : `[${a.name}]`)).join('');
    const isKit = (el: Element) =>
      [...el.attributes].some((a) => a.name.startsWith('data-kit')) || (el.matches('[role="dialog"]') && !!el.closest('[data-kit-drawer]'));
    return [...document.querySelectorAll(SELECTOR)]
      .filter((el) => visible(el) && !name(el))
      .map((el) => ({
        what: `${el.tagName.toLowerCase()}${el.getAttribute('role') ? `[role=${el.getAttribute('role')}]` : ''}${hooks(el)}`,
        text: (el.tagName.toLowerCase() === 'select'
          ? (el as HTMLSelectElement).selectedOptions[0]?.textContent ?? ''
          : textOf(el)).trim().slice(0, 60),
        kit: isKit(el),
      }));
  });
}
