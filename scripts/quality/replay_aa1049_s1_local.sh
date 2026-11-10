#!/usr/bin/env bash
# AA-1049 W3 (S1 train) — the free S1 real-data replay, step 2 (local; no model call, no DB, no network, $0).
#
# Usage (from the root repo, after the S1 train is on CIS main):
#   tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/quality/export_aa1049_s1.py DAYS=60 LIMIT=120 > <export file>
#   apps/AA-CIS-App/scripts/quality/replay_aa1049_s1_local.sh <export file>
#   apps/AA-CIS-App/scripts/quality/replay_aa1049_s1_local.sh --smoke      # the same runs on the synthetic set
#
# What it does:
#   1. decodes the gzip+base64 block of the export into $WORK (gitignored .tmp-session; tour text never leaves it);
#   2. exports CIS origin/main with `git archive` into $WORK/main (the replay drives that checkout's production path);
#   3. runs the replay three times:
#        base    S1_CONTROL unset (= main's A1 path)                                    -> s1_base.json
#        repeat  defaults again, diffed against base (determinism: must be 0 declared / 0 undeclared, exit 0)
#        on      S1_CONTROL=contract with --opt-in, diffed against base                 -> s1_on.json + s1_on.md
#   4. prints one summary line per run with its exit code (0 ok / 1 undeclared or missing: stop / 3 only MISS rows).
set -uo pipefail

ROOT=/home/nghiep/projects/AA-Ecosys
CIS=$ROOT/apps/AA-CIS-App
WORK=${WORK:-$ROOT/.tmp-session/aa1049-s1-replay-$(date +%Y%m%d-%H%M%S)}
PYBIN=${PYBIN:-$CIS/.venv/bin/python}
mkdir -p "$WORK"

run_py() {  # never reach AWS from a replay; dummy app env so module imports do not look for secrets
  env -u AWS_PROFILE AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true \
    ENVIRONMENT=test DATABASE_URL=postgresql://127.0.0.1:1/replay JWT_SECRET=replay-only \
    PYTHONPATH="$WORK/main" "$@"
}

git -C "$CIS" fetch -q origin || { echo "git fetch failed"; exit 2; }
MAIN_SHA=$(git -C "$CIS" rev-parse --short origin/main)
rm -rf "$WORK/main" && mkdir -p "$WORK/main"
git -C "$CIS" archive origin/main | tar -x -C "$WORK/main"
echo "main export: CIS origin/main $MAIN_SHA -> $WORK/main"
[ -f "$WORK/main/scripts/quality/replay_aa1049_s1.py" ] || { echo "the S1 replay is not on CIS main yet"; exit 2; }

if [ "${1:-}" = "--smoke" ]; then
  (cd "$WORK/main" && run_py "$PYBIN" -c "
import json, sys
sys.path.insert(0, 'tests/unit/fixtures')
import aa1049_s1_harness as h
json.dump(h.replay_masters(), open('$WORK/s1_masters.json', 'w'))
") || { echo "smoke: could not build the synthetic S1 masters"; exit 2; }
else
  IN=${1:?usage: replay_aa1049_s1_local.sh <ecsx output file> | --smoke}
  "$PYBIN" - "$IN" "$WORK" <<'EOF' || exit 2
import base64, gzip, re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
name = "s1_masters.json"
m = re.search(rf"=== BEGIN {re.escape(name)}\n(.*?)\n=== END {re.escape(name)}", text, re.S)
if not m:
    sys.exit(f"block {name} missing")
data = gzip.decompress(base64.b64decode("".join(m.group(1).split())))
open(f"{sys.argv[2]}/{name}", "wb").write(data)
print(f"decoded {name}: {len(data)} bytes")
EOF
fi

R=$WORK/main/scripts/quality/replay_aa1049_s1.py
M=$WORK/s1_masters.json
cd "$WORK/main" || exit 2
run_py "$PYBIN" "$R" --masters "$M" --out "$WORK/s1_base.json" >"$WORK/s1_base.log" 2>&1
echo "S1 base   exit=$?"
run_py "$PYBIN" "$R" --masters "$M" --base "$WORK/s1_base.json" --out "$WORK/s1_repeat.json" \
  --report "$WORK/s1_repeat.md" >"$WORK/s1_repeat.log" 2>&1
echo "S1 repeat exit=$? (expect 0: 0 declared, 0 undeclared)"
S1_CONTROL=contract run_py "$PYBIN" "$R" --masters "$M" --base "$WORK/s1_base.json" --out "$WORK/s1_on.json" \
  --report "$WORK/s1_on.md" --opt-in >"$WORK/s1_on.log" 2>&1
echo "S1 on     exit=$? (expect 0, or 3 = only cassette-MISS rows; 1 = undeclared: stop)"
grep -h '^S1_REPLAY' "$WORK/s1_base.log" "$WORK/s1_repeat.log" "$WORK/s1_on.log"
echo "report: $WORK/s1_on.md (declared / undeclared / MISS counts at the top)"
echo "work dir: $WORK"
