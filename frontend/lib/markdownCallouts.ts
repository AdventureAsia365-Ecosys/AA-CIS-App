// lib/markdownCallouts.ts — AA-898: GitHub-style alerts ("> [!NOTE]" …) as callouts in the docs reader.
//
// A remark (mdast) plugin for react-markdown. A blockquote whose first paragraph starts with exactly one of
// [!NOTE] [!TIP] [!IMPORTANT] [!WARNING] [!CAUTION] (upper case, as GitHub writes them) becomes a callout: the marker is
// removed from the text and the blockquote's hast element gets `data-callout="<kind>"`, which MarkdownView styles and
// labels. Nothing else changes: the content stays ordinary Markdown children (no HTML strings, no new node types), so
// the renderer's escaping and link rules still apply. Any other blockquote is left as it is.

export const CALLOUT_KINDS = ["note", "tip", "important", "warning", "caution"] as const;
export type CalloutKind = (typeof CALLOUT_KINDS)[number];

export const CALLOUT_LABEL: Record<CalloutKind, string> = {
  note: "Note",
  tip: "Tip",
  important: "Important",
  warning: "Warning",
  caution: "Caution",
};

// "[!NOTE]" at the very start of the first text, then the end of that line (or of the text).
const MARKER = /^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*(?:\r?\n|$)/;

type MdNode = {
  type: string;
  value?: string;
  children?: MdNode[];
  data?: { hProperties?: Record<string, unknown>; [k: string]: unknown };
};

/** The callout kind of a blockquote node, removing its marker; null (and the node untouched) when it is not one. */
export function takeCallout(blockquote: MdNode): CalloutKind | null {
  const para = blockquote.children?.[0];
  if (!para || para.type !== "paragraph") return null;
  const first = para.children?.[0];
  if (!first || first.type !== "text" || typeof first.value !== "string") return null;
  const m = MARKER.exec(first.value);
  if (!m) return null;
  const kind = m[1].toLowerCase() as CalloutKind;
  first.value = first.value.slice(m[0].length);
  const rest = para.children ?? [];
  // "[!NOTE]" alone on its line: drop the text node if empty, and a following hard line break.
  if (first.value === "") rest.shift();
  if (rest[0]?.type === "break") rest.shift();
  if (rest.length === 0) blockquote.children?.shift();
  return kind;
}

function walk(node: MdNode): void {
  for (const child of node.children ?? []) {
    if (child.type === "blockquote") {
      const kind = takeCallout(child);
      if (kind) {
        child.data = { ...(child.data ?? {}), hProperties: { ...(child.data?.hProperties ?? {}), "data-callout": kind } };
      }
    }
    walk(child);
  }
}

/** The remark plugin: `remarkPlugins={[remarkGfm, remarkCallouts]}`. */
export function remarkCallouts() {
  return (tree: unknown) => {
    walk(tree as MdNode);
  };
}

/** The kind from a rendered blockquote's hast properties (react-markdown passes `node`), or null. */
export function calloutKindOf(node: unknown): CalloutKind | null {
  const props = (node as { properties?: Record<string, unknown> } | undefined)?.properties;
  const v = props?.dataCallout ?? props?.["data-callout"];
  return typeof v === "string" && (CALLOUT_KINDS as readonly string[]).includes(v) ? (v as CalloutKind) : null;
}
