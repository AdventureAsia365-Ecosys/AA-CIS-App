// frontend/scripts/kit-manifest.mjs — writes app/_kit/core/MANIFEST.json (AA-994 S1a, root ADR 0016 decision 5):
// the sha256 of every core file, the canonical source and the `tokens` profile list. Consumers copy it with the core
// (tools/kit/kit-sync.sh in the root repo) and their CI checks the copy against it (core/check-kit-copy.mjs).
//
// Run from frontend/ after any change under app/_kit/core/:   node scripts/kit-manifest.mjs
// `--check` only reports whether MANIFEST.json is current (exit 1 when stale); CI runs the fuller
// `node app/_kit/core/check-kit-copy.mjs --self`.

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { MANIFEST, manifestText } from "../app/_kit/core/check-kit-copy.mjs";

const CORE = fileURLToPath(new URL("../app/_kit/core/", import.meta.url));
const path = join(CORE, MANIFEST);
const want = manifestText(CORE);
const have = existsSync(path) ? readFileSync(path, "utf8") : null;

if (process.argv.includes("--check")) {
  if (have === want) {
    console.log(`kit-manifest: ${MANIFEST} is current`);
    process.exit(0);
  }
  const state = have === null ? "missing" : "stale";
  console.error(`kit-manifest: ${MANIFEST} is ${state}; run node scripts/kit-manifest.mjs`);
  process.exit(1);
}
if (have === want) {
  console.log(`kit-manifest: ${MANIFEST} already current (${Object.keys(JSON.parse(want).files).length} files)`);
} else {
  writeFileSync(path, want);
  console.log(`kit-manifest: wrote app/_kit/core/${MANIFEST} (${Object.keys(JSON.parse(want).files).length} files)`);
}
