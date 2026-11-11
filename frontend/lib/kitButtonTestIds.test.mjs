// frontend/lib/kitButtonTestIds.test.mjs
// AA-1069 guard: a kit button takes its test id through the `testId` prop, never a plain `data-testid` attribute.
// The kit's button family (Button, LinkButton, IconButton, ChipButton, Switch, ListButton, Pressable in
// app/_kit/core/Buttons.tsx) builds its DOM props with `domProps()`. Until AA-1069 it set `data-testid` from `testId`
// AFTER the native props, so a plain `data-testid="..."` on one of them was silently dropped (nine CRM call sites lost
// their ids; Playwright waited forever). `domProps()` now keeps the native attribute when `testId` is unset
// (lib/kitButtons.test.mjs), and this test keeps the call sites on the one documented prop, the same AST scan as the
// AA-Partner web guard: it parses every runtime .tsx file of the admin with the TypeScript compiler and refuses a
// `data-testid` JSX attribute on any kit component whose body calls `domProps(`. The canonical core is scanned too
// (a core component passing `data-testid` to a kit button would ship the same mistake to every consumer), except
// Buttons.tsx itself. Run: npm run test:unit.

import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, sep } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const require = createRequire(join(FRONTEND, "package.json"));
const ts = require("typescript");

const SCAN = ["app", "components", "lib"];
const SKIP_DIRS = new Set(["node_modules", ".next"]);
const BUTTONS = join(FRONTEND, "app", "_kit", "core", "Buttons.tsx");
// A kit import: the barrel or any file under `_kit/`, and inside core the sibling `./Buttons` / `./primitives`.
const KIT_IMPORT = /(^|\/)(_kit|Buttons|primitives)(\/|$)/;

function sourceFiles(rel) {
  const abs = join(FRONTEND, rel);
  if (!existsSync(abs)) return [];
  if (statSync(abs).isFile()) return [rel];
  const out = [];
  for (const entry of readdirSync(abs, { withFileTypes: true })) {
    const child = join(rel, entry.name);
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name) && !entry.name.startsWith(".")) out.push(...sourceFiles(child));
    } else if (entry.name.endsWith(".tsx") && !/\.test\.tsx$/.test(entry.name) && join(FRONTEND, child) !== BUTTONS) {
      out.push(child);
    }
  }
  return out;
}

function parse(file, code) {
  return ts.createSourceFile(file, code, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
}

/** The exported kit components whose DOM props come from `domProps()`. */
export function domPropsComponents(code = readFileSync(BUTTONS, "utf8")) {
  const sf = parse(BUTTONS, code);
  const names = new Set();
  for (const s of sf.statements) {
    if (!ts.isFunctionDeclaration(s) || !s.name || !s.body) continue;
    const exported = s.modifiers?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword);
    if (exported && /\bdomProps\(/.test(s.body.getText(sf))) names.add(s.name.text);
  }
  return names;
}

/** `data-testid` attributes on kit button elements in one file, as "file:line <Tag>". */
export function plainTestIds(rel, kit, code) {
  const sf = parse(rel, code ?? readFileSync(join(FRONTEND, rel), "utf8"));
  const local = new Map(); // local name -> kit name
  for (const s of sf.statements) {
    if (!ts.isImportDeclaration(s) || !ts.isStringLiteral(s.moduleSpecifier) || !KIT_IMPORT.test(s.moduleSpecifier.text)) {
      continue;
    }
    const bindings = s.importClause?.namedBindings;
    if (!bindings || !ts.isNamedImports(bindings)) continue;
    for (const el of bindings.elements) {
      const original = (el.propertyName ?? el.name).text;
      if (kit.has(original)) local.set(el.name.text, original);
    }
  }
  if (!local.size) return [];
  const found = [];
  const visit = (node) => {
    if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && local.has(node.tagName.getText(sf))) {
      for (const attr of node.attributes.properties) {
        if (ts.isJsxAttribute(attr) && attr.name.getText(sf) === "data-testid") {
          const line = sf.getLineAndCharacterOfPosition(attr.getStart(sf)).line + 1;
          found.push(`${rel.split(sep).join("/")}:${line} <${node.tagName.getText(sf)}>`);
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return found;
}

test("the kit's domProps family is found (every kit button that builds its DOM props with domProps)", () => {
  const kit = domPropsComponents();
  for (const name of ["Button", "LinkButton", "IconButton", "ChipButton", "Switch", "ListButton", "Pressable"]) {
    assert.ok(kit.has(name), `${name} is in the domProps family`);
  }
  assert.ok(!kit.has("SegmentedControl"), "SegmentedControl takes testId itself (not domProps)");
});

test("the scanner catches a data-testid on a kit button, aliased or not, and leaves testId and native tags alone", () => {
  const kit = domPropsComponents();
  const code = [
    'import { ChipButton, Button as KitButton } from "../../_kit";',
    'import { Pressable } from "./primitives";',
    "export function X() {",
    "  return (<div>",
    '    <ChipButton selected data-testid="chip-a">A</ChipButton>',
    '    <KitButton data-testid="btn-b">B</KitButton>',
    '    <ChipButton selected testId="chip-c">C</ChipButton>',
    '    <button data-testid="native-d">D</button>',
    '    <Pressable data-testid="tile-e" />',
    "  </div>);",
    "}",
  ].join("\n");
  assert.deepEqual(plainTestIds(join("components", "x.tsx"), kit, code), [
    "components/x.tsx:5 <ChipButton>",
    "components/x.tsx:6 <KitButton>",
    "components/x.tsx:9 <Pressable>",
  ]);
  const other = 'import { Button } from "some-ui-lib";\nexport const Y = () => <Button data-testid="z" />;';
  assert.deepEqual(plainTestIds(join("components", "y.tsx"), kit, other), [], "a non-kit Button is not ours");
});

test("no kit button in the admin (or the canonical core) takes a data-testid attribute (use the testId prop)", () => {
  const kit = domPropsComponents();
  const files = SCAN.flatMap(sourceFiles);
  assert.ok(files.length > 50, `scanned ${files.length} files`);
  const offenders = files.flatMap((rel) => plainTestIds(rel, kit));
  assert.deepEqual(offenders, []);
});
