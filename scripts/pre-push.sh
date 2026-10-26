#!/usr/bin/env bash
# AA-993 H1 (design D5): run this repo's CI checks locally before a push.
#
# Every command here is copied from .github/workflows/ci.yml (jobs Lint, Unit Tests, Frontend Lint, Frontend Build,
# Portal Lint, Portal Build). When ci.yml changes, change the matching CI_CMD entry below in the same PR.
#
# Modes
#   (default) fast  checks picked from `git diff --name-only <base>...<head>`; flake8 on the changed files only,
#                   unit tests of the changed modules plus the files that failed last time; never `next build`.
#   --full          the exact CI Unit Tests command (with coverage) + coverage ratchet, flake8 on the whole CI scope.
#                   AA_PREPUSH=full in the environment does the same.
#   --ci            reserved for H2 (CI calls `scripts/pre-push.sh --full --ci`): never skips by diff, and a missing
#                   tool or node_modules is a failure instead of a skip.
#   --self-test     runs the decision logic on fixture diffs; invokes no lint/test tool.
#   --dry-run       prints the changed-file count and the checks it would run, then exits.
#   --only a,b      run only these check ids (see CHECK_IDS).
#   --base REF      diff base (default origin/main, or AA_PREPUSH_BASE).
#   --head REF      commit being pushed (default HEAD; the hook passes the pushed SHA).
#
# Bypass a blocked push once: git push --no-verify
# Install the hook: scripts/install-hooks.sh   Remove it: git config --unset core.hooksPath
set -uo pipefail

SELF=$(realpath "${BASH_SOURCE[0]}")
CHECK_IDS=(resource_ids text_guard quality_lint flake8 unit frontend_tsc frontend_lint portal_tsc portal_lint)

# --- CI command lines, verbatim from .github/workflows/ci.yml (printed when a check fails) -----------------------
# shellcheck disable=SC2054  # the commas belong to flake8's option values
FLAKE8_ARGS=(--max-line-length=120
  --extend-ignore=E203,W503,E302,E303,E305,E221,F401,E128,E124,E401,E127,E126,B008,E701,F821,F841,W291,E272,W293,E231
  --exclude=__pycache__,.venv,migrations)
FLAKE8_SCOPE=(api/ services/ shared/)
declare -A CI_CMD
CI_CMD[flake8]="flake8 api/ services/ shared/ ${FLAKE8_ARGS[*]}   (ci.yml Lint > Run flake8; CI installs: pip install flake8 flake8-bugbear pyyaml==6.0.3)"
CI_CMD[resource_ids]="python3 .github/scripts/check_resource_ids.py   (ci.yml Lint > Resource-ID check)"
CI_CMD[text_guard]="python3 -m services.golden_set.text_guard scan   (ci.yml Lint > Golden-set text guard; locally markers only unless GOLDEN_GUARD_KEY is set)"
CI_CMD[quality_lint]="python -m services.quality.lint --report   (ci.yml Lint > Quality registry lint)"
CI_CMD[unit]="pytest tests/unit/ -v --tb=short --cov=services --cov=shared --cov-report=term-missing --cov-report=xml:coverage.xml && pytest tests/acp/ -v --tb=short && python3 .github/scripts/coverage_ratchet.py coverage.xml --base <base>   (ci.yml Unit Tests)"
CI_CMD[frontend_tsc]="cd frontend && npx tsc --noEmit -p .   (ci.yml Frontend Build > Type-check; next build stays CI-only)"
CI_CMD[frontend_lint]="cd frontend && npm run lint   (ci.yml Frontend Lint; continue-on-error in CI, so report-only here)"
CI_CMD[portal_tsc]="cd portal && npx tsc --noEmit -p .   (ci.yml Portal Build > Type-check; next build stays CI-only)"
CI_CMD[portal_lint]="cd portal && npm run lint   (ci.yml Portal Lint; continue-on-error in CI, so report-only here)"
# Checks that CI runs with continue-on-error: a failure is reported, never blocks.
REPORT_ONLY=" frontend_lint portal_lint "
# Unit Tests job env (ci.yml test-unit > Run unit tests): dummy values only, no real secret.
UNIT_ENV=(ENVIRONMENT=test AWS_REGION=us-west-1 AWS_DEFAULT_REGION=us-west-1
  DATABASE_URL=postgresql://test:test@localhost:5432/test OPENAI_API_KEY=sk-test-dummy
  JWT_SECRET=test-jwt-secret-ci-only-not-real DATAFORSEO_LOGIN=test DATAFORSEO_PASSWORD=test
  LANGFUSE_SECRET_KEY=test LANGFUSE_PUBLIC_KEY=test LANGFUSE_HOST=http://localhost:3000)
# Fast mode: above this many test files importing the changed modules, run only the name-matched ones (+ --full hint).
MAX_TEST_FILES=${AA_PREPUSH_MAX_TEST_FILES:-30}

# --- decision logic (pure: file names in, check ids out; used by --self-test) -------------------------------------

# is_doc FILE: Markdown or anything under docs/ (no code check needed beyond the always-on scans).
is_doc() { [[ $1 == docs/* || $1 == *.md ]]; }

# plan_checks MODE CI FILE...: print the check ids to run, one per line, in CHECK_IDS order.
plan_checks() {
  local mode=$1 ci=$2; shift 2
  local f code=0 py_lint=0 py=0 fe=0 po=0
  if [[ $ci == 1 ]]; then printf '%s\n' "${CHECK_IDS[@]}"; return; fi
  for f in "$@"; do
    is_doc "$f" || code=1
    case $f in
      api/*.py|services/*.py|shared/*.py) py_lint=1; py=1 ;;
      *.py) py=1 ;;
    esac
    [[ $f == pyproject.toml || $f == requirements.txt || $f == .github/coverage-ratchet.json ]] && py=1
    [[ $f == frontend/* ]] && fe=1
    [[ $f == portal/* ]] && po=1
  done
  echo resource_ids
  echo text_guard
  if [[ $mode == full ]]; then echo quality_lint; echo flake8; echo unit
  else
    [[ $code == 1 ]] && echo quality_lint
    [[ $py_lint == 1 ]] && echo flake8
    [[ $py == 1 ]] && echo unit
  fi
  [[ $fe == 1 ]] && { echo frontend_tsc; echo frontend_lint; }
  [[ $po == 1 ]] && { echo portal_tsc; echo portal_lint; }
  return 0
}

# flake8_targets FILE...: changed .py files inside the CI flake8 scope (the --exclude still applies).
flake8_targets() {
  local f
  for f in "$@"; do
    case $f in api/*.py|services/*.py|shared/*.py) echo "$f" ;; esac
  done
}

# module_of FILE: services/quality/lint.py -> services.quality.lint ; shared/x/__init__.py -> shared.x
module_of() {
  local m=${1%.py}
  m=${m%/__init__}
  echo "${m//\//.}"
}

# unit_targets FILE...: test files to run in fast mode. Changed test files themselves, plus the test files under
# tests/unit and tests/acp that import a changed module (`services.x.y` or `from services.x import y`).
# Prints "OVER <n>" first when more than MAX_TEST_FILES import-matches were cut to the name-matched ones.
unit_targets() {
  local f mod pkg leaf hits=() named=() direct=()
  for f in "$@"; do
    case $f in
      tests/unit/*test_*.py|tests/acp/*test_*.py) direct+=("$f") ;;
      tests/*) ;;
      *.py)
        mod=$(module_of "$f"); leaf=${mod##*.}; pkg=${mod%.*}
        local pat="\\b${mod//./\\.}\\b"
        [[ $pkg != "$mod" ]] && pat+="|from ${pkg//./\\.} import .*\\b${leaf}\\b"
        mapfile -t -O "${#hits[@]}" hits < <(grep -rlE --include='test_*.py' "$pat" tests/unit tests/acp 2>/dev/null)
        mapfile -t -O "${#named[@]}" named < <(find tests/unit tests/acp -name "test_*${leaf}*.py" 2>/dev/null)
        ;;
    esac
  done
  mapfile -t hits < <(printf '%s\n' "${hits[@]}" | sed '/^$/d' | sort -u)
  if (( ${#hits[@]} > MAX_TEST_FILES )); then
    echo "OVER ${#hits[@]}"
    hits=("${named[@]}")
  fi
  printf '%s\n' "${direct[@]}" "${hits[@]}" | sed '/^$/d' | sort -u
}

# last_failed_files: test files pytest recorded as failed last run (the `--lf` part of fast mode).
last_failed_files() {
  local cache=.pytest_cache/v/cache/lastfailed
  [[ -s $cache ]] || return 0
  "$PY" - "$cache" <<'PYEOF'
import json, os, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
seen = set()
for node in data:
    path = node.split("::", 1)[0]
    if path.startswith(("tests/unit/", "tests/acp/")) and os.path.isfile(path) and path not in seen:
        seen.add(path)
        print(path)
PYEOF
}

# --- self-test ------------------------------------------------------------------------------------------------------
self_test() {
  local fails=0 n=0 r
  expect() {  # expect DESC MODE CI "ids" FILE...
    local desc=$1 mode=$2 ci=$3 want=$4; shift 4
    local got; got=$(plan_checks "$mode" "$ci" "$@" | tr '\n' ' '); got=${got% }
    n=$((n + 1))
    if [[ $got == "$want" ]]; then echo "ok   $desc"; else echo "FAIL $desc"; echo "     want: $want"; echo "     got:  $got"; fails=$((fails + 1)); fi
  }
  verdict() {  # verdict DESC GOT RC
    n=$((n + 1))
    if (( $3 == 0 )); then echo "ok   $1"; else echo "FAIL $1: $2"; fails=$((fails + 1)); fi
  }
  local all="${CHECK_IDS[*]}"
  expect "empty diff: always-on scans only"        fast 0 "resource_ids text_guard"
  expect "docs only"                               fast 0 "resource_ids text_guard" docs/implementation-notes/AA-1.md README.md
  expect "service module"                          fast 0 "resource_ids text_guard quality_lint flake8 unit" services/quality/lint.py
  expect "api route + doc"                         fast 0 "resource_ids text_guard quality_lint flake8 unit" api/routers/x.py docs/a.md
  expect "test file only: no flake8 (CI scope)"    fast 0 "resource_ids text_guard quality_lint unit" tests/unit/test_x.py
  expect "worker only: no flake8 (CI scope)"       fast 0 "resource_ids text_guard quality_lint unit" worker/main.py
  expect "requirements bump: unit"                 fast 0 "resource_ids text_guard quality_lint unit" requirements.txt
  expect "migration sql: no python tool"           fast 0 "resource_ids text_guard quality_lint" api/migrations/300_x.sql
  expect "yaml registry: quality lint"             fast 0 "resource_ids text_guard quality_lint" services/quality/registry.yaml
  expect "frontend only: tsc + lint, never build"  fast 0 "resource_ids text_guard quality_lint frontend_tsc frontend_lint" frontend/app/page.tsx
  expect "portal only"                             fast 0 "resource_ids text_guard quality_lint portal_tsc portal_lint" portal/app/x.tsx
  expect "workflow only"                           fast 0 "resource_ids text_guard quality_lint" .github/workflows/ci.yml
  expect "mixed backend + frontend"                fast 0 "resource_ids text_guard quality_lint flake8 unit frontend_tsc frontend_lint" shared/db.py frontend/lib/a.ts
  expect "full mode, docs only: python checks"     full 0 "resource_ids text_guard quality_lint flake8 unit" docs/a.md
  expect "full mode + frontend"                    full 0 "resource_ids text_guard quality_lint flake8 unit frontend_tsc frontend_lint" frontend/a.tsx
  expect "--ci, docs only: never skips by diff"    full 1 "$all" docs/a.md
  expect "--ci, empty diff"                        fast 1 "$all"

  # helpers
  local got
  got=$(flake8_targets services/a.py tests/unit/test_a.py worker/w.py api/b.py frontend/x.ts | tr '\n' ' ')
  r=0; [[ $got == "services/a.py api/b.py " ]] || r=1; verdict "flake8 targets stay in CI scope" "$got" "$r"
  got="$(module_of services/quality/lint.py) $(module_of shared/llm_client/__init__.py)"
  r=0; [[ $got == "services.quality.lint shared.llm_client" ]] || r=1; verdict "module names" "$got" "$r"
  # Uses the repo's own tests (grep only, nothing is run): a changed test file maps to itself, and the lint
  # module maps to its test file.
  got=$(MAX_TEST_FILES=1000 unit_targets tests/unit/test_aa926_lint.py services/quality/lint.py docs/x.md | tr '\n' ' ')
  r=0; [[ $got == *"tests/unit/test_aa926_lint.py"* && $got != *docs* ]] || r=1; verdict "unit targets for a changed module" "$got" "$r"
  got=$(MAX_TEST_FILES=0 unit_targets services/quality/lint.py | head -1)
  r=0; [[ $got == OVER* ]] || r=1; verdict "unit targets over the cap fall back to name matches" "$got" "$r"

  echo
  if (( fails )); then echo "self-test: $fails of $n FAILED"; return 1; fi
  echo "self-test: $n passed"
}

# --- runner ---------------------------------------------------------------------------------------------------------
now() { echo "${EPOCHREALTIME:-$(date +%s)}"; }
elapsed() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.1f", b - a }'; }

RESULTS=() BLOCKED=0 TOTAL_START=$(now)
# run_check ID LOCAL_DESC CMD...: run CMD, time it, record the result, print the CI line on failure.
run_check() {
  local id=$1 desc=$2; shift 2
  local t0 rc t log; t0=$(now)
  echo "==> [$id] $desc"
  # Output is kept quiet unless the check fails (--ci and AA_PREPUSH_VERBOSE=1 stream it).
  if [[ $CI_MODE == 1 || ${AA_PREPUSH_VERBOSE:-0} == 1 ]]; then
    "$@"; rc=$?
  else
    log="$LOG_DIR/$id.log"
    "$@" >"$log" 2>&1; rc=$?
  fi
  t=$(elapsed "$t0" "$(now)")
  if (( rc == 0 )); then
    RESULTS+=("PASS  ${t}s  $id")
    return
  fi
  if [[ -n ${log:-} ]]; then
    echo "---- [$id] output (last 150 lines; full log: $log) ----"
    tail -n 150 "$log"
  fi
  if [[ $REPORT_ONLY == *" $id "* ]]; then
    RESULTS+=("WARN  ${t}s  $id (report-only, as in CI)")
    return
  fi
  RESULTS+=("FAIL  ${t}s  $id")
  BLOCKED=1
  echo
  echo "!! [$id] failed (exit $rc)."
  echo "   ran locally:  $*"
  echo "   CI step that goes red the same way:"
  echo "   ${CI_CMD[$id]}"
  echo
}
skip_check() {  # skip_check ID REASON  (a failure in --ci)
  if [[ $CI_MODE == 1 ]]; then RESULTS+=("FAIL  -     $1 ($2)"); BLOCKED=1
  else RESULTS+=("SKIP  -     $1 ($2)"); fi
}

pick_python() {
  if [[ -n ${AA_PREPUSH_PYTHON:-} ]]; then echo "$AA_PREPUSH_PYTHON"; return; fi
  if [[ -n ${VIRTUAL_ENV:-} && -x $VIRTUAL_ENV/bin/python ]]; then echo "$VIRTUAL_ENV/bin/python"; return; fi
  [[ -x .venv/bin/python ]] && { echo "$PWD/.venv/bin/python"; return; }
  # A linked worktree shares the main checkout's .venv.
  local common; common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
  [[ -n $common && -x ${common%/.git}/.venv/bin/python ]] && { echo "${common%/.git}/.venv/bin/python"; return; }
  command -v python3
}

# node_check DIR ID CMD...: run a Node check in DIR when its node_modules exist (the hook never runs npm ci).
node_check() {
  local dir=$1 id=$2; shift 2
  if [[ ! -d $dir/node_modules ]]; then skip_check "$id" "no $dir/node_modules; run npm ci in $dir"; return; fi
  run_check "$id" "(cd $dir && $*)" bash -c 'cd "$1" && shift && "$@"' _ "$dir" "$@"
}

do_unit() {
  if [[ $MODE == full ]]; then
    run_check unit "full CI unit command with coverage + ratchet vs $BASE" bash -c '
      env "${@:3}" "$1" -m pytest tests/unit/ -v --tb=short --cov=services --cov=shared \
        --cov-report=term-missing --cov-report=xml:coverage.xml || exit $?
      env "${@:3}" "$1" -m pytest tests/acp/ -v --tb=short || exit $?
      "$1" .github/scripts/coverage_ratchet.py coverage.xml --base "$2"' _ "$PY" "$BASE" "${UNIT_ENV[@]}"
    return
  fi
  local targets=() lf=() line over=""
  while IFS= read -r line; do
    if [[ $line == OVER* ]]; then over=${line#OVER }; else targets+=("$line"); fi
  done < <(unit_targets "${CHANGED[@]}")
  mapfile -t lf < <(last_failed_files)
  mapfile -t targets < <(printf '%s\n' "${targets[@]}" "${lf[@]}" | sed '/^$/d' | sort -u)
  [[ -n $over ]] && echo "note: $over test files import the changed modules (cap $MAX_TEST_FILES); fast mode runs the name-matched ones. Run scripts/pre-push.sh --full for all."
  if (( ${#targets[@]} == 0 )); then RESULTS+=("SKIP  -     unit (no test file matches the changed modules)"); return; fi
  run_check unit "pytest on ${#targets[@]} test file(s): changed modules + last failed (${#lf[@]})" \
    env "${UNIT_ENV[@]}" "$PY" -m pytest -q --tb=short "${targets[@]}"
}

main() {
  cd "$(git rev-parse --show-toplevel)" || exit 2
  MODE=fast CI_MODE=0 DRY=0 ONLY="" HEAD_REF=HEAD BASE=${AA_PREPUSH_BASE:-origin/main}
  [[ ${AA_PREPUSH:-} == full ]] && MODE=full
  while (( $# )); do
    case $1 in
      --full) MODE=full ;;
      --fast) MODE=fast ;;
      --ci) CI_MODE=1; MODE=full ;;
      --self-test) self_test; exit $? ;;
      --dry-run) DRY=1 ;;
      --only) ONLY=",$2,"; shift ;;
      --base) BASE=$2; shift ;;
      --head) HEAD_REF=$2; shift ;;
      -h|--help) sed -n '2,21p' "$SELF"; exit 0 ;;
      *) echo "pre-push: unknown argument $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
  done
  PY=$(pick_python)

  CHANGED=()
  if [[ $CI_MODE == 0 ]]; then
    if git rev-parse -q --verify "$BASE^{commit}" >/dev/null && git merge-base "$BASE" "$HEAD_REF" >/dev/null 2>&1; then
      mapfile -t CHANGED < <(git diff --name-only "$BASE...$HEAD_REF")
    else
      echo "pre-push: cannot diff $HEAD_REF against $BASE. Run: git fetch origin   (or pass --base <ref>, or --ci for every check)"
      echo "Bypass this hook once with: git push --no-verify"
      exit 2
    fi
  fi
  local plan=() id
  mapfile -t plan < <(plan_checks "$MODE" "$CI_MODE" "${CHANGED[@]}")
  if [[ -n $ONLY ]]; then
    local kept=(); for id in "${plan[@]}"; do [[ $ONLY == *",$id,"* ]] && kept+=("$id"); done; plan=("${kept[@]}")
  fi

  local label=$MODE; [[ $CI_MODE == 1 ]] && label+=", --ci"
  echo "pre-push ($label): ${#CHANGED[@]} changed file(s) vs $BASE; python: $PY"
  echo "checks: ${plan[*]}"
  if [[ $CI_MODE == 0 && -n $(git status --porcelain --untracked-files=no 2>/dev/null) ]]; then
    echo "note: the working tree has uncommitted changes; the checks read the working tree, the push sends commits."
  fi
  [[ $DRY == 1 ]] && exit 0
  LOG_DIR=$(mktemp -d "${TMPDIR:-/tmp}/pre-push.XXXXXX") || exit 2

  local existing=() f
  for f in "${CHANGED[@]}"; do [[ -f $f ]] && existing+=("$f"); done

  for id in "${plan[@]}"; do
    case $id in
      resource_ids) run_check "$id" "$PY .github/scripts/check_resource_ids.py" "$PY" .github/scripts/check_resource_ids.py ;;
      text_guard)
        if [[ $CI_MODE == 1 ]]; then
          run_check "$id" "$PY -m services.golden_set.text_guard scan" "$PY" -m services.golden_set.text_guard scan
        else
          run_check "$id" "$PY -m services.golden_set.text_guard scan (markers only without GOLDEN_GUARD_KEY)" \
            env GOLDEN_GUARD_REQUIRE_KEY=false "$PY" -m services.golden_set.text_guard scan
        fi ;;
      quality_lint) run_check "$id" "$PY -m services.quality.lint --report" "$PY" -m services.quality.lint --report ;;
      flake8)
        local flake=()
        if "$PY" -m flake8 --version >/dev/null 2>&1; then flake=("$PY" -m flake8)
        elif command -v flake8 >/dev/null 2>&1; then flake=(flake8)
        else skip_check "$id" "flake8 not installed: pip install flake8 flake8-bugbear"; continue; fi
        local targets=()
        if [[ $MODE == full ]]; then targets=("${FLAKE8_SCOPE[@]}")
        else mapfile -t targets < <(flake8_targets "${existing[@]}"); fi
        if (( ${#targets[@]} == 0 )); then RESULTS+=("SKIP  -     flake8 (only deleted files in scope)"); continue; fi
        run_check "$id" "flake8 ${targets[*]} (CI flags)" "${flake[@]}" "${targets[@]}" "${FLAKE8_ARGS[@]}" ;;
      unit) do_unit ;;
      frontend_tsc) node_check frontend "$id" npx tsc --noEmit -p . ;;
      frontend_lint) node_check frontend "$id" npm run lint ;;
      portal_tsc) node_check portal "$id" npx tsc --noEmit -p . ;;
      portal_lint) node_check portal "$id" npm run lint ;;
    esac
  done

  echo
  echo "---- pre-push summary ($(elapsed "$TOTAL_START" "$(now)")s) ----"
  printf '  %s\n' "${RESULTS[@]}"
  if (( BLOCKED )); then
    echo
    echo "Push blocked: a check above fails the same way in CI (the CI command is printed under each failure)."
    echo "Fix it, or bypass this hook once with: git push --no-verify"
    exit 1
  fi
  rm -rf "$LOG_DIR"
  echo "All checks passed."
}

main "$@"
