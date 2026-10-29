#!/bin/bash
# Fixture test for kit-sync.sh (AA-994, root ADR 0016 decision 6). No network, no real app checkout.
# Run: bash .github/scripts/test_kit_sync.sh   (exit 0 = all pass; tests/unit/test_aa994_kit_sync_bot.py runs it in CI)
# Moved from the root repo's tools/kit/ with the script (AA-994 S3s); every call names the fixture CIS with --cis,
# because this script's own repo is the real AA-CIS-App.
set -u
S="$(cd "$(dirname "$0")" && pwd)/kit-sync.sh"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
ok() { pass=$((pass + 1)); }
miss() { fail=$((fail + 1)); echo "MISS: $*"; }
# expect <code> <grep pattern or -> -- <kit-sync args...>
expect() {
  local want=$1 pat=$2; shift 3
  local got
  AA_WORKSPACE="$TMP/ws" bash "$S" "$@" --cis "$TMP/ws/apps/AA-CIS-App" >"$TMP/o" 2>&1; got=$?
  if [ "$got" != "$want" ]; then miss "kit-sync $* -> exit $got, want $want"; sed 's/^/    /' "$TMP/o"; return; fi
  if [ "$pat" != - ] && ! grep -q -- "$pat" "$TMP/o"; then miss "kit-sync $* output lacks '$pat'"; sed 's/^/    /' "$TMP/o"; return; fi
  ok
}
g() { git -C "$TMP/ws/apps/AA-CIS-App" -c user.name=t -c user.email=t@t -c commit.gpgsign=false "$@"; }
manifest() { # write core/MANIFEST.json with the sha256 of every other file in core/
  python3 - "$TMP/ws/apps/AA-CIS-App/frontend/app/_kit/core" <<'PY'
import hashlib, json, os, sys
c = sys.argv[1]
files = {}
for d, _, fs in os.walk(c):
    for f in fs:
        p = os.path.relpath(os.path.join(d, f), c)
        if p != "MANIFEST.json":
            files[p] = hashlib.sha256(open(os.path.join(c, p), "rb").read()).hexdigest()
json.dump({"version": 1, "files": dict(sorted(files.items())),
           "profiles": {"tokens": ["tokens.css", "tokens.ts", "brand.ts", "theme.ts", "check-kit-copy.mjs",
                                   "eslint-kit.mjs", "MANIFEST.json"]}},
          open(os.path.join(c, "MANIFEST.json"), "w"), indent=2)
PY
}

# Workspace fixture: CIS repo without core/, consumer checkouts.
mkdir -p "$TMP/ws/apps/AA-CIS-App/frontend/app/_kit" "$TMP/ws/apps/AA-Partner/portal/app/_kit" \
  "$TMP/ws/apps/AA-Booking/frontend/ui" "$TMP/ws/apps/AA-TripPlanner-Web/frontend/ui"
git -c init.defaultBranch=main init -q "$TMP/ws/apps/AA-CIS-App"
echo "export {};" > "$TMP/ws/apps/AA-CIS-App/frontend/app/_kit/index.ts"
g add -A && g commit -qm init && g update-ref refs/remotes/origin/main HEAD

# 1. before S1a: no core/ -> exit 3 with a clear message, nothing written
expect 3 "does not exist" -- portal --no-fetch
expect 3 "does not exist" -- portal --from-local
[ ! -e "$TMP/ws/apps/AA-Partner/portal/app/_kit/core" ] && [ ! -e "$TMP/ws/apps/AA-Partner/portal/kit.lock.json" ] && ok || miss "wrote files without a core"
expect 2 "unknown consumer" -- nope
expect 2 - -- portal --from-local --ref HEAD

# 2. core/ committed on origin/main
C="$TMP/ws/apps/AA-CIS-App/frontend/app/_kit/core"
mkdir -p "$C/visual"
for f in tokens.css tokens.ts brand.ts theme.ts check-kit-copy.mjs eslint-kit.mjs primitives.tsx DataTable.tsx; do
  echo "// UI kit core (root ADR 0016). $f" > "$C/$f"
done
echo "// probe" > "$C/visual/probe.ts"
manifest
g add -A && g commit -qm core && g update-ref refs/remotes/origin/main HEAD
SHA=$(g rev-parse HEAD)

# a stale file in the consumer's core/ must disappear
mkdir -p "$TMP/ws/apps/AA-Partner/portal/app/_kit/core" && echo stale > "$TMP/ws/apps/AA-Partner/portal/app/_kit/core/old.tsx"
expect 0 "10 files" -- portal --no-fetch
P="$TMP/ws/apps/AA-Partner/portal"
diff -r "$C" "$P/app/_kit/core" >/dev/null && ok || miss "portal copy differs from CIS core"
python3 - "$P/kit.lock.json" "$SHA" "$(sha256sum "$C/MANIFEST.json" | cut -d' ' -f1)" <<'PY' && ok || miss "portal lock content"
import json, sys
lock = json.load(open(sys.argv[1]))
assert lock["ref"] == sys.argv[2], lock
assert lock["profile"] == "full" and lock["dest"] == "portal/app/_kit/core", lock
assert lock["manifest_sha256"] == sys.argv[3], lock
assert lock["canonical"] == {"repo": "AdventureAsia365-Ecosys/AA-CIS-App", "path": "frontend/app/_kit/core"}, lock
PY
cp "$P/kit.lock.json" "$TMP/lock1"
expect 0 - -- portal --no-fetch
cmp -s "$P/kit.lock.json" "$TMP/lock1" && ok || miss "lock not deterministic on re-run"

# 3. tokens profile: only the token files
expect 0 "profile tokens, 7 files" -- tripplanner --no-fetch
T="$TMP/ws/apps/AA-TripPlanner-Web/frontend/ui/core"
[ "$(cd "$T" && ls | sort | tr '\n' ' ')" = "MANIFEST.json brand.ts check-kit-copy.mjs eslint-kit.mjs theme.ts tokens.css tokens.ts " ] \
  && ok || miss "tokens profile file set: $(ls "$T" | tr '\n' ' ')"

# 4. --dry-run writes nothing
expect 0 "dry run" -- booking --no-fetch --dry-run
[ ! -e "$TMP/ws/apps/AA-Booking/frontend/ui/core" ] && ok || miss "dry run wrote files"

# 5. --from-local takes the working tree and records ref "local"
echo "// local change" >> "$C/primitives.tsx"; manifest
expect 0 - -- booking --from-local --dest-repo "$TMP/ws/apps/AA-Booking"
grep -q '"ref": "local"' "$TMP/ws/apps/AA-Booking/frontend/kit.lock.json" && ok || miss "from-local lock ref"
grep -q "local change" "$TMP/ws/apps/AA-Booking/frontend/ui/core/primitives.tsx" && ok || miss "from-local content"

# 6. a stale manifest (file changed, MANIFEST.json not regenerated) is refused
echo "// edited without manifest" >> "$C/DataTable.tsx"
expect 4 "does not match" -- booking --from-local
# 7. --ref of an older commit works; the portal copy goes back to that commit
expect 0 - -- portal --ref "$SHA" --no-fetch
grep -q "$SHA" "$P/kit.lock.json" && ok || miss "--ref lock"

echo "pass=$pass fail=$fail"; [ "$fail" = 0 ]
