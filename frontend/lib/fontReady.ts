// lib/fontReady.ts — the docs reader's Mermaid diagrams wait for the brand font (app/_kit/MermaidDiagram.tsx).
//
// Mermaid measures every label (getBBox) while it draws and sizes the boxes from that. If it draws before Poppins
// has loaded, it measures the fallback face; once Poppins swaps in (Google Fonts, display=swap) the wider glyphs no
// longer fit and labels break mid-word or overflow their boxes. So: wait for the font before drawing, and draw again
// if the font only arrives afterwards. Kept pure (a FontFaceSet-like argument) so it is unit-tested
// (lib/fontReady.test.mjs).

/** The part of the browser's FontFaceSet (document.fonts) used here. */
export type FontSetLike = {
  check(font: string): boolean;
  load(font: string): Promise<unknown>;
  ready: Promise<unknown>;
};

/** The weights mermaid labels use: normal text, and bold titles (class / ER entity names). */
export const LABEL_WEIGHTS = [400, 700] as const;

/** Upper bound on the wait: a font that never arrives must not keep a diagram from being drawn. */
export const FONT_WAIT_MS = 3_000;

/** The first family of a CSS font-family list, quotes kept ("'Poppins', system-ui" → "'Poppins'"). */
export function primaryFamily(fontFamily: string): string {
  return fontFamily.split(",")[0].trim();
}

/** CSS font shorthands for the label weights of a family list ("400 16px 'Poppins'", "700 16px 'Poppins'"). */
export function labelFontSpecs(fontFamily: string): string[] {
  const family = primaryFamily(fontFamily);
  return LABEL_WEIGHTS.map((w) => `${w} 16px ${family}`);
}

/**
 * True when every label weight of the family can be drawn now without a pending download. With no font set
 * (server render, very old browser) there is nothing to wait for. FontFaceSet.check is true when the face is loaded
 * or when no @font-face matches at all (a system font), so a missing web font never blocks.
 */
export function fontIsReady(fonts: FontSetLike | undefined, fontFamily: string): boolean {
  if (!fonts) return true;
  try {
    return labelFontSpecs(fontFamily).every((spec) => fonts.check(spec));
  } catch {
    return true;
  }
}

/**
 * Resolves once the family's label weights are loaded (asked for explicitly, since a face nobody has used yet is
 * not downloaded) and the font set has settled, or after `timeoutMs`, whichever comes first. Never rejects.
 */
export async function waitForFont(
  fonts: FontSetLike | undefined,
  fontFamily: string,
  timeoutMs: number = FONT_WAIT_MS,
): Promise<void> {
  if (!fonts) return;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<void>((resolve) => {
    timer = setTimeout(resolve, timeoutMs);
  });
  const loaded = (async () => {
    try {
      await Promise.all(labelFontSpecs(fontFamily).map((spec) => fonts.load(spec)));
      await fonts.ready;
    } catch {
      // A failed font download: draw with the fallback face (the redraw on a later load still applies).
    }
  })();
  try {
    await Promise.race([loaded, timeout]);
  } finally {
    clearTimeout(timer);
  }
}
