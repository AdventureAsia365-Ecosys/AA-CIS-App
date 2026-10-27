#!/usr/bin/env bash
# AA-993 H1 (design D5): run this repo's CI checks locally before a push.
#
# Every command here is copied from .github/workflows/ci.yml (jobs Lint, Unit Tests, Frontend Lint, Frontend Build,
# Portal Lint, Portal Build). When ci.yml changes, change the matching CI_CMD entry below in the same PR.
#
# Modes
#   (default) fast  checks picked from `git diff --name-only <base>...<head>`; flake8 on the changed files only,
#                   unit tests of the changed files (importers, tests that name the file, a changed conftest's tree)
#                   plus the files that failed last time (both capped); never `next build`.
#   --full          the exact CI Unit Tests command (with coverage) + coverage ratchet, flake8 on the whole CI scope.
#                   AA_PREPUSH=full in the environment does the same.
#   --ci            reserved for H2 (CI calls `scripts/pre-push.sh --full --ci`): never skips by diff, and a missing
#                   node_modules is a failure instead of a skip. A missing Python tool (flake8 + flake8-bugbear,
#                   pytest) is a failure in every mode.
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
CHECK_IDS=(resource_ids text_guard quality_lint flake8 unit frontend_kit frontend_tsc frontend_lint portal_tsc portal_lint)

# --- CI command lines, verbatim from .github/workflows/ci.yml (printed when a check fails) -----------------------
# shellcheck disable=SC2054  # the commas belong to flake8's option values
FLAKE8_ARGS=(--max-line-length=120
  --extend-ignore=E203,W503,E302,E303,E305,E221,F401,E128,E124,E401,E127,E126,B008,E701,F821,F841,W291,E272,W293,E231
  --exclude=__pycache__,.venv,migrations)
FLAKE8_SCOPE=(api/ services/ shared/)
declare -A CI_CMD
CI_CMD[flake8]="flake8 api/ services/ shared/ ${FLAKE8_ARGS[*]}   (ci.yml Lint > Run flake8; CI installs: pip install flake8 flake8-bugbear pyyaml==6.0.3)"
FLAKE8_INSTALL="pip install flake8 flake8-bugbear pyyaml==6.0.3"
CI_CMD[resource_ids]="python3 .github/scripts/check_resource_ids.py   (ci.yml Lint > Resource-ID check)"
CI_CMD[text_guard]="python3 -m services.golden_set.text_guard scan   (ci.yml Lint > Golden-set text guard; locally markers only unless GOLDEN_GUARD_KEY is set)"
CI_CMD[quality_lint]="python -m services.quality.lint --report   (ci.yml Lint > Quality registry lint)"
CI_CMD[unit]="COVERAGE_CORE=sysmon pytest tests/unit/ -v --tb=short --cov=services --cov=shared --cov-report=term-missing --cov-report=xml:coverage.xml && pytest tests/acp/ -v --tb=short && python3 .github/scripts/coverage_ratchet.py coverage.xml --base <base>   (ci.yml Unit Tests; CI installs: uv pip install --system -r requirements.txt -r requirements-dev.txt)"
UNIT_INSTALL="pip install -r requirements.txt -r requirements-dev.txt"
CI_CMD[frontend_tsc]="cd frontend && npx tsc --noEmit -p .   (ci.yml Frontend Build > Type-check; next build stays CI-only)"
CI_CMD[frontend_kit]="cd frontend && node app/_kit/core/check-kit-copy.mjs --self && node app/_kit/core/check-kit-copy.mjs --css . && node app/_kit/core/check-kit-copy.mjs --suppressions <base eslint-suppressions.json> eslint-suppressions.json   (ci.yml Frontend Build > Kit core self-check; Frontend Lint > Suppressions do not grow)"
CI_CMD[frontend_lint]="cd frontend && npx eslint --prune-suppressions && git diff --exit-code eslint-suppressions.json   (ci.yml Frontend Lint; locally only the changed files, or all of frontend/ when the lint config, the kit rules or the suppressions file changed; fix, or after fixing old violations: npx eslint --prune-suppressions <files>)"
CI_CMD[portal_tsc]="cd portal && npx tsc --noEmit -p .   (ci.yml Portal Build > Type-check; next build stays CI-only)"
CI_CMD[portal_lint]="cd portal && npm run lint   (ci.yml Portal Lint; continue-on-error in CI, so report-only here)"
# Checks that CI runs with continue-on-error: a failure is reported, never blocks.
REPORT_ONLY=" portal_lint "
# Unit Tests job env (ci.yml test-unit > Run unit tests): dummy values only, no real secret. COVERAGE_CORE only matters
# with --cov (full mode); CI pins the coverage version it was verified on in requirements-dev.txt.
UNIT_ENV=(COVERAGE_CORE=sysmon ENVIRONMENT=test AWS_REGION=us-west-1 AWS_DEFAULT_REGION=us-west-1
  DATABASE_URL=postgresql://test:test@localhost:5432/test OPENAI_API_KEY=sk-test-dummy
  JWT_SECRET=test-jwt-secret-ci-only-not-real DATAFORSEO_LOGIN=test DATAFORSEO_PASSWORD=test
  LANGFUSE_SECRET_KEY=test LANGFUSE_PUBLIC_KEY=test LANGFUSE_HOST=http://localhost:3000)
# Fast mode: above this many test files matched by grep, run only the name-matched ones (+ --full hint); above this
# many last-failed files, drop them (+ --full hint). Keeps a fast run small on a low-RAM machine.
MAX_TEST_FILES=${AA_PREPUSH_MAX_TEST_FILES:-30}
TEST_DIRS=(tests/unit tests/acp)

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
      # Data the unit tests read: test fixtures and Q0 cases, workflows / CI scripts / ratchet config, migrations.
      tests/unit/*|tests/acp/*|.github/*|api/migrations/*) py=1 ;;
    esac
    case $f in pyproject.toml|requirements.txt|requirements-dev.txt|.trivyignore) py=1 ;; esac
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
  [[ $fe == 1 ]] && { echo frontend_kit; echo frontend_tsc; echo frontend_lint; }
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

# frontend_lint_targets FILE...: what ESLint lints in fast mode, relative to frontend/: the changed files ESLint reads
# (still present), or "." (all of frontend/) when a change can move results in files that did not change: the lint
# config, the kit rules, the suppressions file, the package set. Prints nothing when no frontend code changed.
frontend_lint_targets() {
  local f all=0 out=()
  for f in "$@"; do
    case $f in
      frontend/eslint.config.mjs|frontend/eslint-suppressions.json|frontend/app/_kit/core/eslint-kit.mjs|\
      frontend/package.json|frontend/package-lock.json|frontend/tsconfig.json) all=1 ;;
      frontend/*.ts|frontend/*.tsx|frontend/*.js|frontend/*.jsx|frontend/*.mjs|frontend/*.cjs)
        [[ -f $f ]] && out+=("${f#frontend/}") ;;
    esac
  done
  if (( all )); then echo .; return; fi
  printf '%s\n' "${out[@]}" | sed '/^$/d'
}

# module_of FILE: services/quality/lint.py -> services.quality.lint ; shared/x/__init__.py -> shared.x
module_of() {
  local m=${1%.py}
  m=${m%/__init__}
  echo "${m//\//.}"
}

# import_pattern FILE: ERE for an import of FILE's module (`a.b.c`, or `from a.b import ... c`).
import_pattern() {
  local mod pkg leaf pat
  mod=$(module_of "$1"); leaf=${mod##*.}; pkg=${mod%.*}
  pat="\\b${mod//./\\.}\\b"
  [[ $pkg != "$mod" ]] && pat+="|from ${pkg//./\\.} import .*\\b${leaf}\\b"
  echo "$pat"
}

# unit_targets FILE...: test files to run in fast mode, from the test dirs CI runs (tests/unit, tests/acp):
#   - a changed test_*.py: itself;
#   - a changed conftest.py: every test file in its directory tree; the root conftest of tests/unit or tests/acp
#     prints "WARN <reason>" instead (everything depends on it: --full);
#   - any other changed file: the test files that name it (its file name), plus
#       .py module (also helpers under tests/): the test files that import it, and name-matched test_*<leaf>*.py;
#       a helper under a tests/ sub-folder: also the test files beside it that use the word <leaf>;
#       a data file under tests/unit or tests/acp: the test files in its folder and the folder above
#         (quality/cases/*.json -> quality/test_*.py), never the whole root;
#       .github/workflows/*: the test files that read the workflows folder (paths are often built in segments);
#       api/migrations/*.sql: the test files that glob the migrations.
# Prints "OVER <n>" first when more than MAX_TEST_FILES grep matches were cut to the name-matched ones.
unit_targets() {
  local f name dir parent leaf hits=() named=() direct=()
  for f in "$@"; do
    name=${f##*/}; dir=.; [[ $f == */* ]] && dir=${f%/*}
    if [[ $f == tests/unit/* || $f == tests/acp/* ]]; then
      if [[ $name == test_*.py ]]; then direct+=("$f"); continue; fi
      if [[ $name == conftest.py ]]; then
        if [[ $dir == tests/unit || $dir == tests/acp ]]; then
          echo "WARN $f changed: every test under $dir depends on it"
        else
          mapfile -t -O "${#direct[@]}" direct < <(find "$dir" -name 'test_*.py' 2>/dev/null)
        fi
        continue
      fi
    fi
    [[ $name != __init__.py ]] &&
      mapfile -t -O "${#hits[@]}" hits < <(grep -rlF --include='test_*.py' -- "$name" "${TEST_DIRS[@]}" 2>/dev/null)
    case $f in
      *.py)
        mapfile -t -O "${#hits[@]}" hits < <(grep -rlE --include='test_*.py' "$(import_pattern "$f")" "${TEST_DIRS[@]}" 2>/dev/null)
        leaf=$(module_of "$f"); leaf=${leaf##*.}
        mapfile -t -O "${#named[@]}" named < <(find "${TEST_DIRS[@]}" -name "test_*${leaf}*.py" 2>/dev/null)
        if [[ $f == tests/*/*/* && $dir != tests/unit && $dir != tests/acp ]]; then
          mapfile -t -O "${#hits[@]}" hits < <(grep -rlE --include='test_*.py' "\\b${leaf}\\b" "$dir" 2>/dev/null)
        fi
        ;;
      tests/unit/*|tests/acp/*)
        parent=${dir%/*}
        [[ $dir != tests/unit && $dir != tests/acp ]] &&
          mapfile -t -O "${#hits[@]}" hits < <(find "$dir" -maxdepth 1 -name 'test_*.py' 2>/dev/null)
        [[ $parent != tests && $parent != tests/unit && $parent != tests/acp ]] &&
          mapfile -t -O "${#hits[@]}" hits < <(find "$parent" -maxdepth 1 -name 'test_*.py' 2>/dev/null)
        ;;
      .github/workflows/*)
        mapfile -t -O "${#hits[@]}" hits < <(grep -rlF --include='test_*.py' -- workflows "${TEST_DIRS[@]}" 2>/dev/null) ;;
      api/migrations/*.sql)
        mapfile -t -O "${#hits[@]}" hits < <(grep -rlE --include='test_*.py' 'glob\(.*\.sql' "${TEST_DIRS[@]}" 2>/dev/null) ;;
    esac
  done
  mapfile -t hits < <(printf '%s\n' "${hits[@]}" | sed '/^$/d' | sort -u)
  if (( ${#hits[@]} > MAX_TEST_FILES )); then
    echo "OVER ${#hits[@]}"
    hits=("${named[@]}")
  fi
  printf '%s\n' "${direct[@]}" "${hits[@]}" | sed '/^$/d' | sort -u
}

# changed_files BASE HEAD: the changed paths, NUL-separated and never C-quoted (git quotes non-ASCII names by default,
# which no `case` pattern or -f test would then match). Read with: mapfile -d '' -t ARRAY < <(changed_files ...)
changed_files() { git -c core.quotePath=false diff --name-only -z "$1...$2"; }

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
  expect "requirements-dev bump: unit"             fast 0 "resource_ids text_guard quality_lint unit" requirements-dev.txt
  expect "migration sql: unit (tests read them)"   fast 0 "resource_ids text_guard quality_lint unit" api/migrations/300_x.sql
  expect "test helper module: unit, no flake8"     fast 0 "resource_ids text_guard quality_lint unit" tests/unit/quality/chain.py
  expect "Q0 case json: unit"                      fast 0 "resource_ids text_guard quality_lint unit" tests/unit/quality/cases/a1_contract.json
  expect ".trivyignore: unit"                      fast 0 "resource_ids text_guard quality_lint unit" .trivyignore
  expect "yaml registry: quality lint"             fast 0 "resource_ids text_guard quality_lint" services/quality/registry.yaml
  expect "frontend only: tsc + lint, never build"  fast 0 "resource_ids text_guard quality_lint frontend_kit frontend_tsc frontend_lint" frontend/app/page.tsx
  expect "portal only"                             fast 0 "resource_ids text_guard quality_lint portal_tsc portal_lint" portal/app/x.tsx
  expect "workflow only: unit (CI gate tests)"     fast 0 "resource_ids text_guard quality_lint unit" .github/workflows/ci.yml
  expect "ratchet config: unit"                    fast 0 "resource_ids text_guard quality_lint unit" .github/coverage-ratchet.json
  expect "mixed backend + frontend"                fast 0 "resource_ids text_guard quality_lint flake8 unit frontend_kit frontend_tsc frontend_lint" shared/db.py frontend/lib/a.ts
  expect "full mode, docs only: python checks"     full 0 "resource_ids text_guard quality_lint flake8 unit" docs/a.md
  expect "full mode + frontend"                    full 0 "resource_ids text_guard quality_lint flake8 unit frontend_kit frontend_tsc frontend_lint" frontend/a.tsx
  expect "--ci, docs only: never skips by diff"    full 1 "$all" docs/a.md
  expect "--ci, empty diff"                        fast 1 "$all"

  # helpers
  local got
  got=$(flake8_targets services/a.py tests/unit/test_a.py worker/w.py api/b.py frontend/x.ts | tr '\n' ' ')
  r=0; [[ $got == "services/a.py api/b.py " ]] || r=1; verdict "flake8 targets stay in CI scope" "$got" "$r"
  got="$(module_of services/quality/lint.py) $(module_of shared/llm_client/__init__.py)"
  r=0; [[ $got == "services.quality.lint shared.llm_client" ]] || r=1; verdict "module names" "$got" "$r"
  # Frontend lint targets: changed lintable files only (relative to frontend/); a config / rule / suppressions change
  # lints everything; non-code and deleted files are dropped.
  got=$(frontend_lint_targets frontend/app/layout.tsx frontend/app/globals.css frontend/app/gone.tsx docs/a.md | tr '\n' ' ')
  r=0; [[ $got == "app/layout.tsx " ]] || r=1; verdict "frontend lint targets: changed code files only" "$got" "$r"
  got=$(frontend_lint_targets frontend/app/layout.tsx frontend/eslint-suppressions.json | tr '\n' ' ')
  r=0; [[ $got == ". " ]] || r=1; verdict "frontend lint targets: suppressions change lints all" "$got" "$r"
  got=$(frontend_lint_targets frontend/app/_kit/core/eslint-kit.mjs | tr '\n' ' ')
  r=0; [[ $got == ". " ]] || r=1; verdict "frontend lint targets: kit rule change lints all" "$got" "$r"
  got=$(frontend_lint_targets portal/app/x.tsx | tr '\n' ' ')
  r=0; [[ -z $got ]] || r=1; verdict "frontend lint targets: portal files are not frontend" "$got" "$r"
  # Uses the repo's own tests (grep only, nothing is run): a changed test file maps to itself, and the lint
  # module maps to its test file.
  got=$(MAX_TEST_FILES=1000 unit_targets tests/unit/test_aa926_lint.py services/quality/lint.py docs/x.md | tr '\n' ' ')
  r=0; [[ $got == *"tests/unit/test_aa926_lint.py"* && $got != *docs* ]] || r=1; verdict "unit targets for a changed module" "$got" "$r"
  got=$(MAX_TEST_FILES=0 unit_targets services/quality/lint.py | head -1)
  r=0; [[ $got == OVER* ]] || r=1; verdict "unit targets over the cap fall back to name matches" "$got" "$r"
  got=$(unit_targets tests/unit/conftest.py | tr '\n' ' ')
  r=0; [[ $got == WARN* ]] || r=1; verdict "root conftest: WARN (run --full), not a quiet skip" "$got" "$r"
  got=$(MAX_TEST_FILES=1000 unit_targets tests/unit/quality/conftest.py | tr '\n' ' ')
  r=0; [[ $got == *tests/unit/quality/test_q0_corpus.py* && $got != *tests/unit/test_* ]] || r=1
  verdict "sub-folder conftest: the tests in its tree only" "$got" "$r"
  got=$(MAX_TEST_FILES=1000 unit_targets tests/unit/quality/chain.py | tr '\n' ' ')
  r=0; [[ $got == *test_q0_corpus.py* && $got == *test_red_on_base_script.py* ]] || r=1
  verdict "test helper module: its importers and the tests that name it" "$got" "$r"
  got=$(MAX_TEST_FILES=1000 unit_targets tests/unit/quality/cases/a1_contract.json | tr '\n' ' ')
  r=0; [[ $got == *tests/unit/quality/test_q0_corpus.py* ]] || r=1; verdict "Q0 case json: the Q0 corpus test" "$got" "$r"
  got=$(MAX_TEST_FILES=1000 unit_targets .github/workflows/ci.yml | tr '\n' ' ')
  r=0; [[ $got == *tests/unit/test_aa879_ci_gates.py* ]] || r=1; verdict "ci.yml: the CI gate tests" "$got" "$r"
  got=$(MAX_TEST_FILES=1000 unit_targets api/migrations/261_place_locate_sonnet.sql | tr '\n' ' ')
  r=0; [[ $got == *tests/unit/test_aa847_backfill_ids_prices.py* ]] || r=1; verdict "migration sql: the tests that read it" "$got" "$r"
  # A throwaway repo (no network, no tool): a non-ASCII name with a space comes back raw, not C-quoted.
  local tmp; tmp=$(mktemp -d "${TMPDIR:-/tmp}/pre-push-st.XXXXXX")
  got=$(
    cd "$tmp" && git init -q . && git -c user.name=t -c user.email=t@t commit -q --allow-empty -m a &&
      mkdir services && : >$'services/\xc3\xa9 a.py' && git add -A &&
      git -c user.name=t -c user.email=t@t commit -q -m b && changed_files HEAD~1 HEAD | tr '\0' '\n'
  )
  rm -rf "$tmp"
  r=0; [[ $got == $'services/\xc3\xa9 a.py' ]] || r=1; verdict "non-ASCII file name is listed raw (core.quotePath=false)" "$got" "$r"

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
fail_check() {  # fail_check ID REASON HINT: a check that cannot run the way CI runs it (missing tool) blocks, any mode.
  RESULTS+=("FAIL  -     $1 ($2)")
  BLOCKED=1
  echo "!! [$1] $2"
  echo "   $3"
}
warn_result() {  # warn_result ID TEXT: printed now and listed in the summary; never blocks.
  echo "warning: [$1] $2"
  RESULTS+=("WARN  -     $1 ($2)")
}

# need_pytest: pytest importable by $PY, else a blocking FAIL with the install line.
need_pytest() {
  "$PY" -m pytest --version >/dev/null 2>&1 && return 0
  fail_check unit "pytest is not installed for $PY" "install: \"$PY\" -m $UNIT_INSTALL"
  return 1
}

# check_versions: warn when the local tools differ from what CI pins (Python 3.12; in full mode the coverage pin in
# requirements-dev.txt, which the sysmon tracer was verified on).
check_versions() {
  local v pin cov
  v=$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)
  [[ $v == 3.12.* ]] || warn_result python "$PY is Python ${v:-unknown}; CI runs 3.12"
  [[ $MODE == full ]] || return 0
  pin=$(sed -n 's/^coverage==\([^ #]*\).*/\1/p' requirements-dev.txt 2>/dev/null | head -n 1)
  cov=$("$PY" -c 'import coverage; print(coverage.__version__)' 2>/dev/null)
  if [[ -n $pin && $cov != "$pin" ]]; then
    warn_result coverage "coverage ${cov:-not installed}, CI pins $pin; reinstall: \"$PY\" -m $UNIT_INSTALL"
  fi
  return 0
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
    need_pytest || return
    run_check unit "full CI unit command with coverage + ratchet vs $BASE" bash -c '
      env "${@:3}" "$1" -m pytest tests/unit/ -v --tb=short --cov=services --cov=shared \
        --cov-report=term-missing --cov-report=xml:coverage.xml || exit $?
      env "${@:3}" "$1" -m pytest tests/acp/ -v --tb=short || exit $?
      "$1" .github/scripts/coverage_ratchet.py coverage.xml --base "$2"' _ "$PY" "$BASE" "${UNIT_ENV[@]}"
    return
  fi
  local targets=() lf=() warns=() line over="" full_hint="scripts/pre-push.sh --full runs the whole suite"
  while IFS= read -r line; do
    case $line in
      OVER*) over=${line#OVER } ;;
      WARN*) warns+=("${line#WARN }") ;;
      *) targets+=("$line") ;;
    esac
  done < <(unit_targets "${CHANGED[@]}")
  for line in "${warns[@]}"; do warn_result unit "$line; run --full"; done
  [[ -n $over ]] && echo "note: $over test files match the changed files (cap $MAX_TEST_FILES); fast mode runs the name-matched ones. $full_hint."
  if (( ${#targets[@]} == 0 && ${#warns[@]} == 0 )); then
    warn_result unit "no test file matches the changed files; run --full"
  fi
  mapfile -t lf < <(last_failed_files)
  if (( ${#lf[@]} > MAX_TEST_FILES )); then
    echo "note: ${#lf[@]} test files failed last run (cap $MAX_TEST_FILES); fast mode drops them. $full_hint."
    lf=()
  fi
  mapfile -t targets < <(printf '%s\n' "${targets[@]}" "${lf[@]}" | sed '/^$/d' | sort -u)
  (( ${#targets[@]} == 0 )) && return
  need_pytest || return
  run_check unit "pytest on ${#targets[@]} test file(s): changed files + last failed (${#lf[@]})" \
    env "${UNIT_ENV[@]}" "$PY" -m pytest -q --tb=short "${targets[@]}"
}

# do_frontend_kit: the dependency-free kit checks (no node_modules needed): core self-check, no --aa-* definition
# outside core/tokens.css, and the ESLint suppressions file does not grow against the diff base (CI: Frontend Build
# "Kit core self-check" + Frontend Lint "Suppressions do not grow").
do_frontend_kit() {
  if ! command -v node >/dev/null 2>&1; then fail_check frontend_kit "node is not installed" "install Node 22 (CI: setup-node 22)"; return; fi
  local base_file=$LOG_DIR/base-eslint-suppressions.json supp=(true)
  if git show "$BASE:frontend/eslint-suppressions.json" >"$base_file" 2>/dev/null; then
    supp=(node app/_kit/core/check-kit-copy.mjs --suppressions "$base_file" eslint-suppressions.json)
  fi
  run_check frontend_kit "kit core self-check + --css + suppressions growth vs $BASE (frontend/)" bash -c '
    cd frontend || exit 2
    node app/_kit/core/check-kit-copy.mjs --self || exit $?
    node app/_kit/core/check-kit-copy.mjs --css . || exit $?
    "$@"' _ "${supp[@]}"
}

# do_frontend_lint: ESLint (kit preset at error + bulk suppressions) on the changed frontend files, or on all of
# frontend/ when the config, the rules or the suppressions changed (frontend_lint_targets). In --full / --ci mode the
# CI command itself: prune, then the committed file must equal the pruned one.
do_frontend_lint() {
  local targets=()
  if [[ $MODE == full ]]; then
    node_check frontend frontend_lint bash -c 'npx eslint --prune-suppressions && git diff --exit-code -- eslint-suppressions.json'
    return
  fi
  mapfile -t targets < <(frontend_lint_targets "${CHANGED[@]}")
  if (( ${#targets[@]} == 0 )); then RESULTS+=("SKIP  -     frontend_lint (no changed file ESLint reads)"); return; fi
  node_check frontend frontend_lint npx eslint --no-warn-ignored -- "${targets[@]}"
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
      --only|--base|--head)
        if (( $# < 2 )); then echo "pre-push: $1 needs a value (usage: $1 <value>; see --help)" >&2; exit 2; fi
        case $1 in --only) ONLY=",$2,";; --base) BASE=$2;; --head) HEAD_REF=$2;; esac
        shift ;;
      -h|--help) sed -n '2,/^set /{/^#/p}' "$SELF"; exit 0 ;;
      *) echo "pre-push: unknown argument $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
  done
  PY=$(pick_python)

  CHANGED=()
  if [[ $CI_MODE == 0 ]]; then
    if git rev-parse -q --verify "$BASE^{commit}" >/dev/null && git merge-base "$BASE" "$HEAD_REF" >/dev/null 2>&1; then
      mapfile -d '' -t CHANGED < <(changed_files "$BASE" "$HEAD_REF")
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
  check_versions
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
        local targets=()
        if [[ $MODE == full ]]; then targets=("${FLAKE8_SCOPE[@]}")
        else mapfile -t targets < <(flake8_targets "${existing[@]}"); fi
        if (( ${#targets[@]} == 0 )); then RESULTS+=("SKIP  -     flake8 (only deleted files in scope)"); continue; fi
        # CI installs flake8 + flake8-bugbear: without the plugin the B checks silently do not run, so it is required.
        local flake=() fver=""
        if fver=$("$PY" -m flake8 --version 2>/dev/null); then flake=("$PY" -m flake8)
        elif command -v flake8 >/dev/null 2>&1 && fver=$(flake8 --version 2>/dev/null); then flake=(flake8)
        fi
        if (( ${#flake[@]} == 0 )); then
          fail_check "$id" "flake8 is not installed" "install: \"$PY\" -m $FLAKE8_INSTALL"; continue
        fi
        if [[ $fver != *flake8-bugbear* ]]; then
          fail_check "$id" "flake8 runs without flake8-bugbear (CI runs the B checks)" "install: \"$PY\" -m $FLAKE8_INSTALL"
          continue
        fi
        run_check "$id" "flake8 ${targets[*]} (CI flags)" "${flake[@]}" "${targets[@]}" "${FLAKE8_ARGS[@]}" ;;
      unit) do_unit ;;
      frontend_kit) do_frontend_kit ;;
      frontend_tsc) node_check frontend "$id" npx tsc --noEmit -p . ;;
      frontend_lint) do_frontend_lint ;;
      portal_tsc) node_check portal "$id" npx tsc --noEmit -p . ;;
      portal_lint) node_check portal "$id" npm run lint ;;
    esac
  done

  echo
  echo "---- pre-push summary ($(elapsed "$TOTAL_START" "$(now)")s) ----"
  printf '  %s\n' "${RESULTS[@]}"
  if (( BLOCKED )); then
    echo
    echo "Push blocked: a check above fails the same way in CI, or a tool CI uses is missing (see the lines under each failure)."
    echo "Fix it, or bypass this hook once with: git push --no-verify"
    exit 1
  fi
  rm -rf "$LOG_DIR"
  echo "All checks passed."
}

main "$@"
