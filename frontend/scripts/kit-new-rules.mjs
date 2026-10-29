// frontend/scripts/kit-new-rules.mjs — which lint rules does this change switch on? (AA-994 S6a, plan guard G11)
//
// The "Suppressions do not grow" step of Frontend Lint passes the answer to
// `check-kit-copy.mjs --suppressions … --new-rules <list>`, so a rule turned on in this change (a staged kit rule such
// as aa-kit/no-raw-form-control) can freeze its existing violations in eslint-suppressions.json once, while every rule
// that already ran at the base still only shrinks.
//
// A rule is new when, for every file the head suppressions file lists it under, it is on at the head and off at the
// base. The base config is rebuilt from git (eslint.config.mjs + the kit core folder at <base-sha>) in a scratch folder
// inside node_modules/.cache, so its package imports resolve from this app's node_modules; ESLint computes both configs
// per file (calculateConfigForFile). Prints the comma-separated rule list (empty line when none).
// Fails closed: any error exits 2 and prints nothing on stdout, and the caller passes no --new-rules.
//
// Run from frontend/:   node scripts/kit-new-rules.mjs <base-sha> [eslint-suppressions.json]

import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { ESLint } from "eslint";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
/** What the config imports from this repo, relative to frontend/ (the kit rules live in the core). */
const CONFIG = "eslint.config.mjs";
const CORE = "app/_kit/core";

const on = (entry) => {
  const sev = Array.isArray(entry) ? entry[0] : entry;
  return sev === 2 || sev === 1 || sev === "error" || sev === "warn";
};

/** Rules switched on at the head and off at the base for every file that lists them in `supp` ({file: {rule: …}}). */
export async function newRules(supp, headCfg, baseCfg) {
  const byRule = {};
  for (const [file, rules] of Object.entries(supp)) for (const rule of Object.keys(rules)) (byRule[rule] ??= []).push(file);
  const out = [];
  for (const [rule, files] of Object.entries(byRule)) {
    let fresh = true;
    for (const file of files) {
      const head = (await headCfg(file))?.rules?.[rule];
      const base = (await baseCfg(file))?.rules?.[rule];
      if (!head || !on(head) || (base && on(base))) { fresh = false; break; }
    }
    if (fresh) out.push(rule);
  }
  return out.sort();
}

/** Write the base commit's ESLint config and kit core into `dir` (same relative layout as frontend/). */
function materializeBase(sha, dir) {
  const git = (...args) => execFileSync("git", args, { cwd: FRONTEND, encoding: "utf8", maxBuffer: 64 << 20 });
  const files = [CONFIG, ...git("ls-tree", "-r", "--name-only", sha, "--", CORE).split("\n").filter(Boolean)
    .map((p) => p.slice(p.indexOf(CORE)))];
  for (const rel of files) {
    const text = git("show", `${sha}:frontend/${rel}`);
    mkdirSync(dirname(join(dir, rel)), { recursive: true });
    writeFileSync(join(dir, rel), text);
  }
}

async function main(argv) {
  const [sha, suppPath = "eslint-suppressions.json"] = argv;
  if (!sha) throw new Error("usage: node scripts/kit-new-rules.mjs <base-sha> [eslint-suppressions.json]");
  const supp = JSON.parse(readFileSync(join(FRONTEND, suppPath), "utf8"));
  const scratch = join(FRONTEND, "node_modules", ".cache", `aa-kit-base-${process.pid}`);
  try {
    materializeBase(sha, scratch);
    const head = new ESLint({ cwd: FRONTEND });
    const base = new ESLint({ cwd: FRONTEND, overrideConfigFile: join(scratch, CONFIG) });
    const rules = await newRules(supp, (f) => head.calculateConfigForFile(f), (f) => base.calculateConfigForFile(f));
    console.log(rules.join(","));
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main(process.argv.slice(2)).catch((e) => {
    console.error(`kit-new-rules: ${e.message}`);
    process.exit(2);
  });
}
