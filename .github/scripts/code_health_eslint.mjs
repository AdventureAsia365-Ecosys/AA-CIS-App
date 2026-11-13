#!/usr/bin/env node
// AA-1097 (code-health ratchet): cyclomatic complexity of every TypeScript / JavaScript function, measured by ESLint's
// core `complexity` rule (classic variant), keyed by a stable qualified name. Called by .github/scripts/code_health.py;
// not meant to be run by hand (run `python3 .github/scripts/code_health.py check --kind typescript`).
//
//   node .github/scripts/code_health_eslint.mjs <frontend dir>   < JSON array of file paths relative to that dir
//   -> stdout: {"functions": [{"path", "name", "score"}], "errors": [{"path", "message"}]}
//
// ESLint, the TypeScript parser and the core rule come from <frontend dir>/node_modules (the frontend's own pinned
// versions, installed by `npm ci`), so nothing new is installed. Only the complexity rule runs (no type information,
// no other rule), with the frontend's eslint.config.mjs ignored: the score depends on the code alone.
//
// Qualified name: the chain of enclosing class and function names joined by "." (a method "Store.load", a callback
// inside a component "Page.useEffect()", a JSX handler "Page.onClick"). An anonymous function takes its name from
// where it sits: variable, property or method key, assignment target, the call it is an argument of ("map()"), the
// JSX attribute, "default" for a default export, else "<anonymous>". Two functions with the same name in one file get
// "#2", "#3"... in source order.
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

const frontendDir = path.resolve(process.argv[2] || ".");
const requireFromFrontend = createRequire(path.join(frontendDir, "package.json"));

async function load(name) {
  return import(pathToFileURL(requireFromFrontend.resolve(name)).href);
}

const { ESLint } = await load("eslint");
const { builtinRules } = await load("eslint/use-at-your-own-risk");
const tsParser = await load("@typescript-eslint/parser");
const coreComplexity = builtinRules.get("complexity");

const FUNCTION_TYPES = new Set(["FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression"]);

function keyName(key, computed, sourceCode) {
  if (!key) return "<anonymous>";
  if (!computed && key.type === "Identifier") return key.name;
  if (key.type === "PrivateIdentifier") return `#${key.name}`;
  if (key.type === "Literal") return String(key.value);
  return `[${sourceCode.getText(key)}]`;
}

function calleeName(callee) {
  if (!callee) return "call";
  if (callee.type === "Identifier") return callee.name;
  if (callee.type === "MemberExpression" && !callee.computed && callee.property.type === "Identifier") {
    return callee.property.name;
  }
  if (callee.type === "CallExpression") return calleeName(callee.callee);
  return "call";
}

// Name of one function-like or class node, from the node and its parent.
function ownName(node, sourceCode) {
  if (node.id && node.id.type === "Identifier") return node.id.name;
  if (node.type === "StaticBlock") return "<static>";
  let parent = node.parent;
  while (parent && (parent.type === "TSAsExpression" || parent.type === "TSSatisfiesExpression"
    || parent.type === "TSNonNullExpression" || parent.type === "ParenthesizedExpression")) {
    parent = parent.parent;
  }
  if (!parent) return "<anonymous>";
  switch (parent.type) {
    case "VariableDeclarator":
      return parent.id.type === "Identifier" ? parent.id.name : "<destructured>";
    case "MethodDefinition":
    case "TSAbstractMethodDefinition": {
      const name = keyName(parent.key, parent.computed, sourceCode);
      return parent.kind === "get" || parent.kind === "set" ? `${parent.kind} ${name}` : name;
    }
    case "Property": {
      const name = keyName(parent.key, parent.computed, sourceCode);
      return parent.kind === "get" || parent.kind === "set" ? `${parent.kind} ${name}` : name;
    }
    case "PropertyDefinition":
    case "AccessorProperty":
      return keyName(parent.key, parent.computed, sourceCode);
    case "AssignmentExpression":
      return parent.left.type === "Identifier" || parent.left.type === "MemberExpression"
        ? sourceCode.getText(parent.left)
        : "<assignment>";
    case "AssignmentPattern":
      return parent.left.type === "Identifier" ? parent.left.name : "<default>";
    case "CallExpression":
    case "NewExpression":
      return `${calleeName(parent.callee)}()`;
    case "ExportDefaultDeclaration":
      return "default";
    case "JSXExpressionContainer":
      if (parent.parent && parent.parent.type === "JSXAttribute") {
        const attr = parent.parent.name;
        return attr.type === "JSXIdentifier" ? attr.name : sourceCode.getText(attr);
      }
      return "<jsx>";
    default:
      return "<anonymous>";
  }
}

function qualifiedName(node, sourceCode) {
  const parts = [];
  for (let cur = node; cur; cur = cur.parent) {
    if (FUNCTION_TYPES.has(cur.type) || cur.type === "ClassDeclaration" || cur.type === "ClassExpression"
      || cur.type === "StaticBlock") {
      parts.push(ownName(cur, sourceCode));
    } else if (cur.type === "PropertyDefinition" && cur.value && cur === node.parent && !FUNCTION_TYPES.has(node.type)) {
      parts.push(keyName(cur.key, cur.computed, sourceCode));
    }
  }
  return parts.reverse().join(".");
}

// The core rule, run with threshold 0 (every function reports) through a context whose report() records the node
// and its score instead of reporting a problem; one problem per function then carries the score and the name.
const scoreRule = {
  meta: { ...coreComplexity.meta, defaultOptions: [0], messages: { score: "{{payload}}" } },
  create(context) {
    const inner = Object.create(context, {
      options: { value: [{ max: 0, variant: "classic" }] },
      report: {
        value(descriptor) {
          const node = descriptor.node;
          let name;
          if (node.type === "StaticBlock" || FUNCTION_TYPES.has(node.type)) {
            name = qualifiedName(node, context.sourceCode);
          } else {
            // class field initializer: the value expression; name it after its class and field.
            const field = node.parent;
            const owner = qualifiedName(field.parent.parent, context.sourceCode);
            name = `${owner}.${keyName(field.key, field.computed, context.sourceCode)}`;
          }
          context.report({
            node,
            loc: node.loc,
            messageId: "score",
            data: { payload: JSON.stringify({ name, score: descriptor.data.complexity }) },
          });
        },
      },
    });
    return coreComplexity.create(inner);
  },
};

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const files = JSON.parse(Buffer.concat(chunks).toString("utf8") || "[]");

const eslint = new ESLint({
  cwd: frontendDir,
  overrideConfigFile: true,
  overrideConfig: [
    {
      files: ["**/*.{ts,tsx,mts,cts,js,jsx,mjs,cjs}"],
      languageOptions: {
        parser: tsParser,
        ecmaVersion: "latest",
        sourceType: "module",
        parserOptions: { ecmaFeatures: { jsx: true } },
      },
      plugins: { "code-health": { rules: { score: scoreRule } } },
      rules: { "code-health/score": "error" },
      linterOptions: { reportUnusedDisableDirectives: "off", noInlineConfig: true },
    },
  ],
  ignore: false,
});

const out = { functions: [], errors: [] };
const results = files.length ? await eslint.lintFiles(files) : [];
for (const result of results) {
  const rel = path.relative(frontendDir, result.filePath).split(path.sep).join("/");
  const seen = new Map();
  const rows = [];
  for (const msg of result.messages) {
    if (msg.ruleId !== "code-health/score") {
      if (msg.fatal || msg.severity === 2) out.errors.push({ path: rel, message: msg.message });
      continue;
    }
    rows.push({ line: msg.line, column: msg.column, ...JSON.parse(msg.message) });
  }
  rows.sort((a, b) => a.line - b.line || a.column - b.column);
  for (const row of rows) {
    const n = (seen.get(row.name) || 0) + 1;
    seen.set(row.name, n);
    out.functions.push({ path: rel, name: n === 1 ? row.name : `${row.name}#${n}`, score: row.score });
  }
}
process.stdout.write(JSON.stringify(out));
