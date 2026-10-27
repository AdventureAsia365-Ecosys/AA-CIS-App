// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
// app/_kit/core/eslint-kit.mjs — the UI kit conformance rules (AA-994 S1c, root ADR 0016 decision 7).
// Dependency-free ESLint 9 flat-config plugin, vendored with the core into every consumer app, so the rule text is
// byte-identical everywhere. No plugin package: the rules are plain ESTree/JSX visitors.
//
// Use (eslint.config.mjs of an app):
//   import { kitConfig } from "./app/_kit/core/eslint-kit.mjs";          // Booking / TripPlanner: ./ui/core/…
//   export default defineConfig([
//     …,
//     kitConfig({ preset: "inline", files: ["app/**/*.{ts,tsx}"], ignores: ["app/_kit/**"] }),
//   ]);
// Presets: `inline` (CIS admin, portal, CRM web: inline styles + `K`, also no `className` in pages) and `tailwind`
// (Booking, TripPlanner). Every rule is `error`; existing violations live in the app's eslint-suppressions.json
// (ESLint >= 9.24 bulk suppressions), which CI only lets shrink (check-kit-copy.mjs --suppressions).
// An app turns a rule off with `rules`, e.g. TripPlanner: { "aa-kit/no-raw-table": "off" } (ADR 0016 plan G2, G5).
//
// Rules (ADR 0016 decision 7; rule 8 `no-raw-form-control` ships with the form controls, AA-994 S6a):
//   1 no-raw-main            no <main> in pages: PageFrame (alias AdminMain) owns the page frame
//   2 no-raw-table           no <table> outside DataTable
//   3 no-raw-dialog          no <dialog> / role="dialog" outside Drawer / Modal
//   4 no-hex-colour          no hex / rgb() / hsl() colour literal in page code: K (inline) or a token utility
//   5 no-hand-styled-button  no <button style=…> / <button className=…>: the kit Button
//   6 no-raw-aa-var          no `var(--aa-…)` string in page code: K (inline) or the Tailwind token
//   7 no-aa-var-definition   no `--aa-*` custom property set from JS (style key, setProperty, CSS text); CSS files are
//                            checked by check-kit-copy.mjs --css, since ESLint does not lint CSS
//   9 no-legacy-kit          no import of a legacy primitive module the kit replaces (CIS adminUi, portal ui.tsx)
//   - no-class-name          (inline preset only) no `className` on JSX in pages

import { dirname, isAbsolute, relative, resolve, sep } from "node:path";

const KIT = "the UI kit (barrel app/_kit, Booking/TripPlanner ui/)";
const REF = "(root ADR 0016 decision 7)";

// ---- helpers -------------------------------------------------------------------------------------------------------

/** The plain name of a JSX element or attribute (`main`, `role`, `aria-label`); "" for member/namespaced names. */
function jsxName(name) {
  if (!name) return "";
  if (name.type === "JSXIdentifier") return name.name;
  if (name.type === "JSXNamespacedName") return `${name.namespace.name}:${name.name.name}`;
  return "";
}

/** The static string of a JSX attribute value: "x", {"x"} or {`x`} (no expressions); null otherwise. */
function attrString(value) {
  if (!value) return null;
  if (value.type === "Literal" && typeof value.value === "string") return value.value;
  if (value.type === "JSXExpressionContainer") {
    const e = value.expression;
    if (e.type === "Literal" && typeof e.value === "string") return e.value;
    if (e.type === "TemplateLiteral" && e.expressions.length === 0) return e.quasis[0].value.cooked ?? "";
  }
  return null;
}

/** The text of a string literal or a template literal (expressions become one space); null for other nodes. */
function stringText(node) {
  if (node.type === "Literal" && typeof node.value === "string") return node.value;
  if (node.type === "TemplateLiteral") return node.quasis.map((q) => q.value.cooked ?? q.value.raw).join(" ");
  return null;
}

/** True when a string node is the source of an import/export or a TypeScript literal type (not page values). */
function isModuleOrType(node) {
  const p = node.parent;
  if (!p) return false;
  if ((p.type === "ImportDeclaration" || p.type === "ExportAllDeclaration" || p.type === "ExportNamedDeclaration"
    || p.type === "ImportExpression") && p.source === node) return true;
  if (p.type === "TSLiteralType" || p.type === "TSExternalModuleReference" || p.type === "TSImportType") return true;
  if (p.type === "Property" && p.key === node && !p.computed) return true; // an object key is not a value
  return false;
}

/** Visit every string-like value once: string literals and template literals (not tagged templates' tags). */
function stringVisitors(check) {
  return {
    Literal(node) {
      if (typeof node.value === "string" && !isModuleOrType(node)) check(node, node.value);
    },
    TemplateLiteral(node) {
      if (node.parent && node.parent.type === "TSLiteralType") return;
      check(node, stringText(node));
    },
  };
}

function ancestors(context, node) {
  const sc = context.sourceCode ?? context.getSourceCode();
  return sc.getAncestors ? sc.getAncestors(node) : context.getAncestors();
}

// ---- rule 4: colour literals ---------------------------------------------------------------------------------------

const HEX = "#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})";
/** A string that is only a colour: "#fff", " #1A2B3C ". */
const HEX_ALONE = new RegExp(`^\\s*${HEX}\\s*$`);
/** A hex colour inside a longer CSS-ish text: after start, space, comma, paren, colon or [ ; not followed by a word. */
const HEX_IN_TEXT = new RegExp(`(?:^|[\\s,(:\\[])${HEX}(?![\\w-])`);
/** A Tailwind arbitrary value holding a colour: bg-[#fff], text-[rgb(0,0,0)], [color:#000]. */
const HEX_IN_BRACKETS = new RegExp(`\\[[^\\]]*(?:${HEX}|(?:rgba?|hsla?)\\()[^\\]]*\\]`);
/** rgb()/rgba()/hsl()/hsla() with a literal first value (not `rgb(var(--x))`). */
const COLOUR_FN = /\b(?:rgba?|hsla?)\(\s*[\d.]/i;
/** Words that make a string CSS text, so a "#abc" inside it is a colour and not "Order #abc". */
const CSS_WORDS = /\b(?:solid|dashed|dotted|double|inset|(?:linear|radial|conic)-gradient)\b|\d(?:px|rem|em)\b/;
/** JSX attributes whose values are styling. */
const STYLE_ATTRS = new Set([
  "style", "className", "class", "fill", "stroke", "color", "stopColor", "floodColor", "lightingColor", "background",
]);
/** JSX attributes whose values are never colours (anchors, ids, routes, visible or spoken text). */
const NON_COLOUR_ATTRS = new Set([
  "href", "to", "id", "htmlFor", "key", "name", "src", "target", "rel", "type", "placeholder", "title", "alt", "label",
]);
/** An all-digit hex that reads as a number ("#123", "#1024"), not a colour: 3 or 4 digits that are not one repeated
 *  digit ("#000" and "#3333" stay colours). Only outside a style attribute or a colour-ish key. */
const NUMBER_LIKE_HEX = /^\s*#(?!(\d)\1+\s*$)\d{3,4}\s*$/;
/** Object keys whose values are colours. */
const COLOUR_KEY = /colou?r|background|border|outline|fill|stroke|shadow|caret|decoration|gradient/i;

function keyName(prop) {
  if (!prop || prop.type !== "Property") return "";
  if (prop.key.type === "Identifier" && !prop.computed) return prop.key.name;
  if (prop.key.type === "Literal") return String(prop.key.value);
  return "";
}

/** Why a string is a hard-coded colour in this position, or null. */
function colourProblem(context, node, text) {
  if (text == null || text.length > 2000) return null;
  const anc = ancestors(context, node);
  let attr = null;
  for (let i = anc.length - 1; i >= 0; i--) {
    if (anc[i].type === "JSXAttribute") { attr = jsxName(anc[i].name); break; }
  }
  if (attr && (NON_COLOUR_ATTRS.has(attr) || attr.startsWith("aria-") || attr.startsWith("data-"))) return null;
  if (COLOUR_FN.test(text)) return "an rgb()/hsl() colour";
  if (HEX_IN_BRACKETS.test(text) && (attr === "className" || attr === "class")) return "a Tailwind arbitrary colour";
  const parent = node.parent;
  const inStyle = attr != null && STYLE_ATTRS.has(attr);
  const colourKey = parent && parent.type === "Property" && parent.value === node && COLOUR_KEY.test(keyName(parent));
  if (HEX_ALONE.test(text)) return inStyle || colourKey || !NUMBER_LIKE_HEX.test(text) ? "a hex colour" : null;
  if (!HEX_IN_TEXT.test(text)) return null;
  return inStyle || colourKey || CSS_WORDS.test(text) ? "a hex colour" : null;
}

// ---- rule 9: legacy kit modules ------------------------------------------------------------------------------------

/** Legacy primitive layers the kit replaces (ADR 0016 context 5), as paths from the app root without extension. */
export const LEGACY_MODULES = [
  { path: "app/admin/_components/adminUi", use: "Button, Badge, Tabs, DataTable, Section… from the kit barrel" },
  { path: "app/portal/_components/ui", use: "the kit barrel (PageHeader, Button, Badge, DataTable…)" },
];
const CODE_SUFFIX = /(?:\/index)?\.(?:tsx?|jsx?|mjs|cjs)$|\/index$/;

/** The path an import resolves to, "/"-separated without extension: app-root-relative for the `@/` alias and for
 *  relative imports inside root, absolute otherwise; null for packages. */
export function resolveImport(spec, filename, root) {
  let abs;
  if (spec.startsWith("@/")) abs = resolve(root, spec.slice(2));
  else if (spec.startsWith("./") || spec.startsWith("../")) abs = resolve(dirname(filename), spec);
  else if (isAbsolute(spec)) abs = spec;
  else return null;
  const rel = relative(root, abs);
  const out = rel.startsWith("..") || isAbsolute(rel) ? abs : rel;
  return out.split(sep).join("/").replace(CODE_SUFFIX, "");
}

/** True when a resolved import is the legacy module `path` (exact, or ending in /path when ESLint runs from another
 *  folder than the app root, e.g. an editor at the repo root). */
export function isModule(target, path) {
  return target === path || target.endsWith(`/${path}`);
}

// ---- rules ---------------------------------------------------------------------------------------------------------

const rule = (description, message, create, schema = []) => ({
  meta: { type: "problem", docs: { description }, messages: { kit: message }, schema },
  create,
});

export const RULES = {
  "no-raw-main": rule(
    "No raw <main> in pages; the kit PageFrame (AdminMain) renders the page frame",
    `No raw <main> in page code: render the kit PageFrame (alias AdminMain) from ${KIT}. ${REF}`,
    (context) => ({
      JSXOpeningElement(node) {
        if (jsxName(node.name) === "main") context.report({ node, messageId: "kit" });
      },
    }),
  ),

  "no-raw-table": rule(
    "No raw <table> outside the kit DataTable",
    `No raw <table> in page code: use the kit DataTable from ${KIT}. ${REF}`,
    (context) => ({
      JSXOpeningElement(node) {
        if (jsxName(node.name) === "table") context.report({ node, messageId: "kit" });
      },
    }),
  ),

  "no-raw-dialog": rule(
    "No hand-rolled dialog (<dialog> or role=\"dialog\") outside the kit Modal / Drawer",
    `No hand-rolled dialog in page code: use the kit Modal, ConfirmModal or Drawer from ${KIT}. ${REF}`,
    (context) => ({
      JSXOpeningElement(node) {
        if (jsxName(node.name) === "dialog") context.report({ node, messageId: "kit" });
      },
      JSXAttribute(node) {
        if (jsxName(node.name) !== "role") return;
        const v = attrString(node.value);
        if (v === "dialog" || v === "alertdialog") context.report({ node, messageId: "kit" });
      },
    }),
  ),

  "no-hex-colour": {
    meta: {
      type: "problem",
      docs: { description: "No hex / rgb() / hsl() colour literal in page code" },
      messages: {
        kit: `No hard-coded colour ({{what}}) in page code: use K.<token> (inline styles) or a token utility class, so both themes and the contrast checks apply. ${REF}`,
      },
      schema: [],
    },
    create: (context) => stringVisitors((node, text) => {
      const what = colourProblem(context, node, text);
      if (what) context.report({ node, messageId: "kit", data: { what } });
    }),
  },

  "no-hand-styled-button": rule(
    "No <button> with its own style or className; use the kit Button",
    `No hand-styled <button> in page code: use the kit Button (icon-only: variant="ghost") from ${KIT}. ${REF}`,
    (context) => ({
      JSXOpeningElement(node) {
        if (jsxName(node.name) !== "button") return;
        const styled = node.attributes.some((a) => a.type === "JSXAttribute"
          && ["style", "className", "class"].includes(jsxName(a.name)));
        if (styled) context.report({ node, messageId: "kit" });
      },
    }),
  ),

  "no-raw-aa-var": rule(
    "No var(--aa-*) string in page code; use K (inline) or the Tailwind token",
    `No raw var(--aa-…) in page code: use K.<token> from ${KIT} (inline preset) or the Tailwind token class. ${REF}`,
    (context) => stringVisitors((node, text) => {
      if (text && /var\(\s*--aa-/.test(text)) context.report({ node, messageId: "kit" });
    }),
  ),

  "no-aa-var-definition": rule(
    "No --aa-* custom property defined from JS; tokens live only in core/tokens.css",
    "No --aa-* definition outside the kit core tokens.css: give app variables the app's own prefix (--bk-*, " +
      `--tp-*, --crm-*) or change the token in AA-CIS-App frontend/app/_kit/core/tokens.css. ${REF}`,
    (context) => {
      const definesInText = (text) => text != null && /(?<![\w-])--aa-[\w-]+\s*:/.test(text);
      return {
        Property(node) {
          const k = node.key;
          const name = k.type === "Literal" ? k.value : k.type === "TemplateLiteral" ? stringText(k) : null;
          if (typeof name === "string" && name.trim().startsWith("--aa-")) context.report({ node: k, messageId: "kit" });
        },
        CallExpression(node) {
          const c = node.callee;
          const isSet = c.type === "MemberExpression" && !c.computed && c.property.name === "setProperty";
          const arg = node.arguments[0];
          const name = arg ? stringText(arg) : null;
          if (isSet && typeof name === "string" && name.trim().startsWith("--aa-")) {
            context.report({ node: arg, messageId: "kit" });
          }
        },
        ...stringVisitors((node, text) => {
          if (definesInText(text)) context.report({ node, messageId: "kit" });
        }),
      };
    },
  ),

  "no-legacy-kit": {
    meta: {
      type: "problem",
      docs: { description: "No import of a legacy primitive module the kit replaces (CIS adminUi, portal ui.tsx)" },
      messages: {
        kit: `"{{spec}}" is a legacy primitive layer the kit replaces: import {{use}}. Existing imports are in eslint-suppressions.json until AA-994 migrates them. ${REF}`,
      },
      schema: [{
        type: "object",
        properties: {
          root: { type: "string" },
          modules: {
            type: "array",
            items: {
              type: "object",
              properties: { path: { type: "string" }, use: { type: "string" } },
              required: ["path"],
              additionalProperties: false,
            },
          },
        },
        additionalProperties: false,
      }],
    },
    create(context) {
      const opts = context.options[0] || {};
      const root = opts.root || context.cwd || (context.getCwd && context.getCwd()) || process.cwd();
      const modules = opts.modules || LEGACY_MODULES;
      const filename = context.filename || context.getFilename();
      const check = (source) => {
        if (!source || source.type !== "Literal" || typeof source.value !== "string") return;
        const target = resolveImport(source.value, filename, root);
        const hit = target && modules.find((m) => isModule(target, m.path));
        if (hit) context.report({ node: source, messageId: "kit", data: { spec: source.value, use: hit.use || KIT } });
      };
      return {
        ImportDeclaration: (node) => check(node.source),
        ExportNamedDeclaration: (node) => check(node.source),
        ExportAllDeclaration: (node) => check(node.source),
        ImportExpression: (node) => check(node.source),
      };
    },
  },

  "no-class-name": rule(
    "No className on JSX in pages (inline preset: inline style + K tokens)",
    `No className on JSX in page code: use inline style with K.<token> and the kit components from ${KIT}. ${REF}`,
    (context) => ({
      JSXAttribute(node) {
        const n = jsxName(node.name);
        if (n === "className" || n === "class") context.report({ node, messageId: "kit" });
      },
    }),
  ),
};

export const plugin = { meta: { name: "aa-kit", version: "1.0.0" }, rules: RULES };

const SHARED = [
  "no-raw-main", "no-raw-table", "no-raw-dialog", "no-hex-colour", "no-hand-styled-button", "no-raw-aa-var",
  "no-aa-var-definition", "no-legacy-kit",
];
/** ADR 0016 decision 7: `inline` = CIS admin, portal, CRM web; `tailwind` = Booking, TripPlanner. */
export const PRESETS = { inline: [...SHARED, "no-class-name"], tailwind: SHARED };

/** The rule map of a preset, every rule at `severity` (default "error"). */
export function kitRules(preset, severity = "error") {
  const names = PRESETS[preset];
  if (!names) throw new Error(`eslint-kit: unknown preset "${preset}" (${Object.keys(PRESETS).join(", ")})`);
  return Object.fromEntries(names.map((n) => [`aa-kit/${n}`, severity]));
}

/** One flat-config object: the plugin plus a preset on `files` minus `ignores`; `rules` overrides single rules. */
export function kitConfig({ preset, files, ignores = [], severity = "error", rules = {} }) {
  if (!Array.isArray(files) || files.length === 0) throw new Error("eslint-kit: kitConfig needs `files`");
  return {
    name: `aa-kit/${preset}`,
    files,
    ignores,
    plugins: { "aa-kit": plugin },
    rules: { ...kitRules(preset, severity), ...rules },
  };
}

export default plugin;
