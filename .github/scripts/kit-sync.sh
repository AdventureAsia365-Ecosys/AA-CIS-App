#!/usr/bin/env bash
# Copy the canonical UI kit core of AA-CIS-App into one consumer app and rewrite its kit.lock.json
# (AA-994, root ADR 0016 decisions 2, 5 and 6). It writes files in the consumer checkout and stops; committing,
# pushing and the PR stay with the caller (an agent by hand, or .github/scripts/kit_sync_bot.py in kit-sync.yml).
#
# Usage:
#   .github/scripts/kit-sync.sh <consumer> [--from-local] [--ref <git-ref>] [--cis <path>] [--dest-repo <path>]
#                                          [--no-fetch] [--dry-run]
#   .github/scripts/kit-sync.sh --list      one line per consumer: consumer, repo, destination, lock file, profile
#
# This file is canonical since AA-994 S3s (it was the root repo's tools/kit/kit-sync.sh, which now runs this one),
# so the CIS workflows (kit-sync.yml, kit-consumers.yml) and the agents copy with the same code.
#
# Consumers (ADR 0016 decision 2):
#   portal       AA-Partner          portal/app/_kit/core   portal/kit.lock.json     full
#   crm-web      AA-Partner          web/app/_kit/core      web/kit.lock.json        full
#   booking      AA-Booking          frontend/ui/core       frontend/kit.lock.json   full
#   tripplanner  AA-TripPlanner-Web  frontend/ui/core       frontend/kit.lock.json   tokens
#
# Source (AA-CIS-App `frontend/app/_kit/core/`):
#   default        the commit at CIS `origin/main` (fetched first unless --no-fetch); the lock records its sha.
#   --ref <ref>    a given CIS commit instead (it should be on CIS main: the Provenance check rejects anything else).
#   --from-local   the files in the CIS checkout as they are (tracked + untracked, .gitignore respected), for a
#                  try-out; the lock records `"ref": "local"`, which the Provenance check rejects in CI by design.
# Checkouts: --cis defaults to the AA-CIS-App checkout this script is in. --dest-repo defaults to
# $AA_WORKSPACE/apps/<repo> when AA_WORKSPACE is set, else to the sibling folder <repo> of the CIS main checkout
# (<workspace>/apps/<repo>, also when this script runs from a CIS worktree); pass a worktree path to sync into a
# branch checkout.
#
# Profiles: `full` copies every file of core/. `tokens` copies only the token files: the list in MANIFEST.json
# `profiles.tokens` when the manifest has one, else ADR 0016's list (tokens.css, tokens.ts, brand.ts, theme.ts,
# check-kit-copy.mjs, eslint-kit.mjs, MANIFEST.json).
#
# MANIFEST.json (written by CIS `frontend/scripts/kit-manifest.mjs`, AA-994 S1a) must be in core/. When it lists file
# hashes as `"files": {"<path>": "<sha256>" | {"sha256": "<sha256>"}}` or `"files": [{"path": …, "sha256": …}]`,
# every copied file is checked against it and a `full` copy must hold exactly the listed files; otherwise only a
# warning is printed.
#
# The consumer's core/ folder is replaced as a whole (stale files are removed). The lock is deterministic (no time
# stamp), so re-running on the same ref changes nothing.
#
# Exit codes: 0 synced (or dry run, or --list), 2 usage error, 3 CIS has no core/ yet (before AA-994 S1a), 4 source
# or manifest problem, 5 destination problem.
set -euo pipefail

die() { local code=$1; shift; echo "kit-sync: $*" >&2; exit "$code"; }
usage() { sed -n '2,9p' "$0" >&2; exit 2; }

CANON_REPO="AdventureAsia365-Ecosys/AA-CIS-App"
CANON_PATH="frontend/app/_kit/core"
TOKENS_DEFAULT="tokens.css tokens.ts brand.ts theme.ts check-kit-copy.mjs eslint-kit.mjs MANIFEST.json"

consumer="" from_local=0 ref_arg="" cis="" dest_repo="" fetch=1 dry=0 list=0
while [ $# -gt 0 ]; do
  case "$1" in
    --from-local) from_local=1 ;;
    --ref) [ $# -ge 2 ] || usage; ref_arg=$2; shift ;;
    --cis) [ $# -ge 2 ] || usage; cis=$2; shift ;;
    --dest-repo) [ $# -ge 2 ] || usage; dest_repo=$2; shift ;;
    --no-fetch) fetch=0 ;;
    --dry-run) dry=1 ;;
    --list) list=1 ;;
    -h|--help) sed -n '2,46p' "$0"; exit 0 ;;
    -*) echo "kit-sync: unknown option $1" >&2; usage ;;
    *) [ -z "$consumer" ] || usage; consumer=$1 ;;
  esac
  shift
done
# The consumer table (ADR 0016 decision 2). kit_sync_bot.py reads it through --list, so this is its only copy.
CONSUMERS="portal AA-Partner portal/app/_kit/core portal/kit.lock.json full
crm-web AA-Partner web/app/_kit/core web/kit.lock.json full
booking AA-Booking frontend/ui/core frontend/kit.lock.json full
tripplanner AA-TripPlanner-Web frontend/ui/core frontend/kit.lock.json tokens"
if [ "$list" = 1 ]; then
  [ -z "$consumer" ] || usage
  printf '%s\n' "$CONSUMERS" | tr ' ' '\t'
  exit 0
fi
[ -n "$consumer" ] || usage
[ "$from_local" = 1 ] && [ -n "$ref_arg" ] && die 2 "--from-local and --ref exclude each other"

row=$(printf '%s\n' "$CONSUMERS" | awk -v c="$consumer" '$1 == c')
[ -n "$row" ] || die 2 "unknown consumer '$consumer' (portal, crm-web, booking, tripplanner)"
read -r _ repo dest_rel lock_rel profile <<<"$row"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "$cis" ]; then
  cis=$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null) \
    || die 2 "cannot find the AA-CIS-App checkout from $script_dir (use --cis)"
fi
if [ -z "$dest_repo" ]; then
  if [ -n "${AA_WORKSPACE:-}" ]; then
    dest_repo=$AA_WORKSPACE/apps/$repo
  else
    # <workspace>/apps/AA-CIS-App/.git -> <workspace>/apps; a worktree shares the main checkout's common dir.
    common=$(git -C "$cis" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) \
      || die 2 "AA-CIS-App checkout not found at $cis (use --cis)"
    dest_repo=$(dirname "$(dirname "$common")")/$repo
  fi
fi
git -C "$cis" rev-parse --git-dir >/dev/null 2>&1 || die 2 "AA-CIS-App checkout not found at $cis (use --cis)"
[ -d "$dest_repo" ] || die 5 "$repo checkout not found at $dest_repo (use --dest-repo)"

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
src="$stage/core"
mkdir -p "$src"

# --- 1. source files into $stage/core ----------------------------------------------------------------------------
if [ "$from_local" = 1 ]; then
  ref=local
  label="local checkout $cis"
  [ -d "$cis/$CANON_PATH" ] || die 3 "AA-CIS-App $CANON_PATH/ does not exist in $cis yet. It is created by AA-994 slice S1a (root ADR 0016 decision 1); nothing to sync."
  (cd "$cis" && git ls-files -z --cached --others --exclude-standard -- "$CANON_PATH") \
    | while IFS= read -r -d '' f; do
        [ -f "$cis/$f" ] || continue # deleted in the working tree
        mkdir -p "$src/$(dirname "${f#"$CANON_PATH"/}")"
        cp -p "$cis/$f" "$src/${f#"$CANON_PATH"/}"
      done
else
  if [ -n "$ref_arg" ]; then
    want=$ref_arg
  else
    want=origin/main
    if [ "$fetch" = 1 ]; then
      git -C "$cis" fetch -q origin main || die 4 "git fetch origin main failed in $cis (use --no-fetch to sync from the last fetched origin/main)"
    fi
  fi
  ref=$(git -C "$cis" rev-parse --verify -q "$want^{commit}") || die 4 "CIS ref '$want' not found in $cis"
  label="AA-CIS-App $want ($ref)"
  if [ -n "$ref_arg" ] && git -C "$cis" rev-parse -q --verify origin/main >/dev/null \
     && ! git -C "$cis" merge-base --is-ancestor "$ref" origin/main; then
    echo "kit-sync: warning: $ref is not on CIS origin/main; the Provenance check will reject this lock" >&2
  fi
  if [ -z "$(git -C "$cis" ls-tree -r --name-only "$ref" -- "$CANON_PATH/")" ]; then
    die 3 "AA-CIS-App $CANON_PATH/ does not exist at $label yet. It is created by AA-994 slice S1a (root ADR 0016 decision 1); nothing to sync."
  fi
  git -C "$cis" archive --format=tar "$ref" -- "$CANON_PATH" | tar -x -C "$stage"
  rm -rf "$src"
  mv "$stage/$CANON_PATH" "$src"
fi
[ -n "$(find "$src" -type f -print -quit)" ] \
  || die 3 "AA-CIS-App $CANON_PATH/ is empty at $label. It is created by AA-994 slice S1a (root ADR 0016 decision 1); nothing to sync."
[ -f "$src/MANIFEST.json" ] || die 4 "$CANON_PATH/MANIFEST.json is missing at $label (CIS: run frontend/scripts/kit-manifest.mjs and merge)"

# --- 2. profile selection + manifest check (python: stdlib only) -------------------------------------------------
out="$stage/out"
mkdir -p "$out"
python3 - "$src" "$out" "$profile" "$TOKENS_DEFAULT" <<'PY' || exit $?
import hashlib, json, os, shutil, sys

src, out, profile, tokens_default = sys.argv[1:5]

def fail(msg):
    print(f"kit-sync: {msg}", file=sys.stderr)
    sys.exit(4)

files = sorted(
    os.path.relpath(os.path.join(d, f), src).replace(os.sep, "/")
    for d, _, fs in os.walk(src) for f in fs
)
try:
    manifest = json.load(open(os.path.join(src, "MANIFEST.json"), encoding="utf-8"))
except ValueError as e:
    fail(f"MANIFEST.json is not valid JSON: {e}")

if profile == "tokens":
    listed = (manifest.get("profiles") or {}).get("tokens") if isinstance(manifest, dict) else None
    selected = sorted(set(listed if isinstance(listed, list) else tokens_default.split()) | {"MANIFEST.json"})
    missing = [f for f in selected if f not in files]
    if missing:
        fail("tokens profile files missing from core: " + ", ".join(missing))
else:
    selected = files

hashes = None
mf = manifest.get("files") if isinstance(manifest, dict) else None
if isinstance(mf, dict):
    hashes = {k: (v.get("sha256") if isinstance(v, dict) else v) for k, v in mf.items()}
elif isinstance(mf, list):
    hashes = {e.get("path"): e.get("sha256") for e in mf if isinstance(e, dict)}

if hashes is None:
    print("kit-sync: warning: MANIFEST.json has no recognised 'files' hash list; copied files were not verified",
          file=sys.stderr)
else:
    def sha(p):
        return hashlib.sha256(open(os.path.join(src, p), "rb").read()).hexdigest()
    bad = [p for p in selected if p != "MANIFEST.json" and hashes.get(p) != sha(p)]
    if bad:
        fail("MANIFEST.json does not match core/ for: " + ", ".join(bad)
             + " (CIS: re-run frontend/scripts/kit-manifest.mjs; never sync a stale manifest)")
    if profile == "full":
        extra = sorted(set(hashes) - set(files))
        if extra:
            fail("MANIFEST.json lists files that are not in core/: " + ", ".join(extra))

for p in selected:
    os.makedirs(os.path.dirname(os.path.join(out, p)), exist_ok=True)
    shutil.copy2(os.path.join(src, p), os.path.join(out, p))
PY
count=$(find "$out" -type f | wc -l | tr -d ' ')
manifest_sha=$(sha256sum "$out/MANIFEST.json" | cut -d' ' -f1)

lock_json=$(python3 - "$CANON_REPO" "$CANON_PATH" "$ref" "$profile" "$dest_rel" "$manifest_sha" <<'PY'
import json, sys
repo, path, ref, profile, dest, msha = sys.argv[1:7]
print(json.dumps({
    "adr": "root ADR 0016",
    "canonical": {"repo": repo, "path": path},
    "ref": ref,
    "profile": profile,
    "dest": dest,
    "manifest_sha256": msha,
}, indent=2))
PY
)

echo "kit-sync: $consumer <- $label"
echo "kit-sync: profile $profile, $count files -> $dest_repo/$dest_rel"
if [ "$dry" = 1 ]; then
  (cd "$out" && find . -type f | sed 's|^\./|  |' | sort)
  echo "kit-sync: dry run; $lock_rel would be:"
  echo "$lock_json"
  exit 0
fi

# --- 3. replace the consumer's core/ and write the lock -----------------------------------------------------------
case "$dest_rel" in */_kit/core|*/ui/core) ;; *) die 5 "refusing to replace $dest_rel (not a kit core folder)" ;; esac
dest="$dest_repo/$dest_rel"
[ -L "$dest" ] && die 5 "$dest is a symlink; refusing to replace it"
mkdir -p "$(dirname "$dest")"
rm -rf "$dest"
cp -R "$out" "$dest"
printf '%s\n' "$lock_json" > "$dest_repo/$lock_rel"

echo "kit-sync: wrote $dest_rel/ and $lock_rel (ref $ref)"
if git -C "$dest_repo" rev-parse --git-dir >/dev/null 2>&1; then
  changes=$(git -C "$dest_repo" status --porcelain -- "$dest_rel" "$lock_rel" | wc -l | tr -d ' ')
  echo "kit-sync: $changes path(s) changed in $repo; review with: git -C $dest_repo status -- $dest_rel $lock_rel"
fi
echo "kit-sync: next: node $dest_rel/check-kit-copy.mjs (integrity), the app build, then a PR 'chore: sync UI kit core to AA-CIS-App ${ref:0:7}' with Refs AA-994"
