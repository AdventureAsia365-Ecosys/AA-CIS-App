#!/usr/bin/env bash
# Copy the canonical AI gateway core of AA-CIS-App (shared/aa_llm/) into one consumer app and rewrite its
# aa_llm.lock.json (AA-642 S8, root ADR 0009 decision 13). Modelled on .github/scripts/kit-sync.sh. It writes files in
# the consumer checkout and stops; committing, pushing and the PR stay with the caller (an agent by hand, or
# .github/scripts/llm_core_sync_bot.py in llm-core-sync.yml).
#
# Usage:
#   .github/scripts/llm-core-sync.sh <consumer> [--from-local] [--ref <git-ref>] [--cis <path>] [--dest-repo <path>]
#                                               [--no-fetch] [--dry-run]
#   .github/scripts/llm-core-sync.sh --list   one line per consumer: consumer, repo, destination, lock file, test dir
#
# Consumers (ADR 0009 decision 13; the CRM vendors code only, ADR 0008a):
#   tripplanner  AA-TripPlanner-Web  backend/aa_llm   backend/aa_llm.lock.json   backend
#   crm          AA-Partner          api/app/aa_llm   api/aa_llm.lock.json       api
#
# Source (AA-CIS-App `shared/aa_llm/`):
#   default        the commit at CIS `origin/main` (fetched first unless --no-fetch); the lock records its sha.
#   --ref <ref>    a given CIS commit instead (it should be on CIS main).
#   --from-local   the files in the CIS checkout as they are (tracked + untracked, .gitignore respected), for a
#                  try-out or the consumers check of a CIS PR; the lock records `"ref": "local"`.
# Checkouts: --cis defaults to the AA-CIS-App checkout this script is in. --dest-repo defaults to
# $AA_WORKSPACE/apps/<repo> when AA_WORKSPACE is set, else to the sibling folder <repo> of the CIS main checkout.
#
# The copy is checked with the core's own check_copy.py against its MANIFEST.json before anything is written (a stale
# manifest never syncs). The consumer's aa_llm/ folder is replaced as a whole. The lock is deterministic (no time
# stamp), so re-running on the same ref changes nothing.
#
# Exit codes: 0 synced (or dry run, or --list), 2 usage error, 3 CIS has no core at that ref, 4 source or manifest
# problem, 5 destination problem.
set -euo pipefail

die() { local code=$1; shift; echo "llm-core-sync: $*" >&2; exit "$code"; }
usage() { sed -n '7,10p' "$0" >&2; exit 2; }

CANON_REPO="AdventureAsia365-Ecosys/AA-CIS-App"
CANON_PATH="shared/aa_llm"

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
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    -*) echo "llm-core-sync: unknown option $1" >&2; usage ;;
    *) [ -z "$consumer" ] || usage; consumer=$1 ;;
  esac
  shift
done
# The consumer table. llm_core_sync_bot.py reads it through --list, so this is its only copy.
CONSUMERS="tripplanner AA-TripPlanner-Web backend/aa_llm backend/aa_llm.lock.json backend
crm AA-Partner api/app/aa_llm api/aa_llm.lock.json api"
if [ "$list" = 1 ]; then
  [ -z "$consumer" ] || usage
  printf '%s\n' "$CONSUMERS" | tr ' ' '\t'
  exit 0
fi
[ -n "$consumer" ] || usage
[ "$from_local" = 1 ] && [ -n "$ref_arg" ] && die 2 "--from-local and --ref exclude each other"

row=$(printf '%s\n' "$CONSUMERS" | awk -v c="$consumer" '$1 == c')
[ -n "$row" ] || die 2 "unknown consumer '$consumer' (tripplanner, crm)"
read -r _ repo dest_rel lock_rel _ <<<"$row"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "$cis" ]; then
  cis=$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null) \
    || die 2 "cannot find the AA-CIS-App checkout from $script_dir (use --cis)"
fi
git -C "$cis" rev-parse --git-dir >/dev/null 2>&1 || die 2 "AA-CIS-App checkout not found at $cis (use --cis)"
if [ -z "$dest_repo" ]; then
  if [ -n "${AA_WORKSPACE:-}" ]; then
    dest_repo=$AA_WORKSPACE/apps/$repo
  else
    common=$(git -C "$cis" rev-parse --path-format=absolute --git-common-dir)
    dest_repo=$(dirname "$(dirname "$common")")/$repo
  fi
fi
[ -d "$dest_repo" ] || die 5 "$repo checkout not found at $dest_repo (use --dest-repo)"

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
src="$stage/aa_llm"
mkdir -p "$src"

# --- 1. source files into $stage/aa_llm -------------------------------------------------------------------------------
if [ "$from_local" = 1 ]; then
  ref=local
  label="local checkout $cis"
  [ -d "$cis/$CANON_PATH" ] || die 3 "AA-CIS-App $CANON_PATH/ does not exist in $cis"
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
      git -C "$cis" fetch -q origin main || die 4 "git fetch origin main failed in $cis (use --no-fetch)"
    fi
  fi
  ref=$(git -C "$cis" rev-parse --verify -q "$want^{commit}") || die 4 "CIS ref '$want' not found in $cis"
  label="AA-CIS-App $want ($ref)"
  if [ -n "$ref_arg" ] && git -C "$cis" rev-parse -q --verify origin/main >/dev/null \
     && ! git -C "$cis" merge-base --is-ancestor "$ref" origin/main; then
    echo "llm-core-sync: warning: $ref is not on CIS origin/main; the provenance check will reject this lock" >&2
  fi
  [ -n "$(git -C "$cis" ls-tree -r --name-only "$ref" -- "$CANON_PATH/")" ] \
    || die 3 "AA-CIS-App $CANON_PATH/ does not exist at $label"
  git -C "$cis" archive --format=tar "$ref" -- "$CANON_PATH" | tar -x -C "$stage"
  rm -rf "$src"
  mv "$stage/$CANON_PATH" "$src"
fi
find "$src" -mindepth 1 \( -name __pycache__ -o -name '.*' -o -name '*.pyc' \) -prune -exec rm -rf {} +
[ -f "$src/MANIFEST.json" ] || die 4 "$CANON_PATH/MANIFEST.json is missing at $label"
[ -f "$src/check_copy.py" ] || die 4 "$CANON_PATH/check_copy.py is missing at $label"

# --- 2. integrity of the source against its own manifest -----------------------------------------------------------
if ! out=$(python3 -I -B "$src/check_copy.py" 2>&1); then
  printf '%s\n' "$out" >&2
  die 4 "$CANON_PATH/ does not match its MANIFEST.json at $label (CIS: run python3 $CANON_PATH/check_copy.py --write)"
fi
count=$(find "$src" -type f | wc -l | tr -d ' ')
manifest_sha=$(sha256sum "$src/MANIFEST.json" | cut -d' ' -f1)
lock_json=$(python3 -I - "$CANON_REPO" "$CANON_PATH" "$ref" "$dest_rel" "$manifest_sha" <<'PY'
import json, sys
repo, path, ref, dest, msha = sys.argv[1:6]
print(json.dumps({
    "adr": "root ADR 0009 decision 13",
    "canonical": {"repo": repo, "path": path},
    "ref": ref,
    "dest": dest,
    "manifest_sha256": msha,
}, indent=2))
PY
)

echo "llm-core-sync: $consumer <- $label"
echo "llm-core-sync: $count files -> $dest_repo/$dest_rel"
if [ "$dry" = 1 ]; then
  (cd "$src" && find . -type f | sed 's|^\./|  |' | sort)
  echo "llm-core-sync: dry run; $lock_rel would be:"
  echo "$lock_json"
  exit 0
fi

# --- 3. replace the consumer's aa_llm/ and write the lock -------------------------------------------------------------
case "$dest_rel" in */aa_llm) ;; *) die 5 "refusing to replace $dest_rel (not an aa_llm folder)" ;; esac
dest="$dest_repo/$dest_rel"
[ -L "$dest" ] && die 5 "$dest is a symlink; refusing to replace it"
mkdir -p "$(dirname "$dest")"
rm -rf "$dest"
cp -R "$src" "$dest"
printf '%s\n' "$lock_json" > "$dest_repo/$lock_rel"

echo "llm-core-sync: wrote $dest_rel/ and $lock_rel (ref $ref)"
if git -C "$dest_repo" rev-parse --git-dir >/dev/null 2>&1; then
  changes=$(git -C "$dest_repo" status --porcelain -- "$dest_rel" "$lock_rel" | wc -l | tr -d ' ')
  echo "llm-core-sync: $changes path(s) changed in $repo; review with: git -C $dest_repo status -- $dest_rel $lock_rel"
fi
echo "llm-core-sync: next: python3 $dest_rel/check_copy.py --lock $lock_rel, the app tests, then a PR 'chore: sync AI gateway core aa_llm to AA-CIS-App ${ref:0:7}' with Refs AA-642"
