#!/usr/bin/env bash
# AA-993 H1 (design D5): run this repo's CI checks locally before a push.
#
# Every command here is copied from .github/workflows/ci.yml (jobs Lint, Unit Tests, Frontend Lint, Frontend Build).
# When ci.yml changes, change the matching CI_CMD entry below in the same PR.
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
#   --integration   AA-1024 l22 (G6): run the integration test files the diff touches (changed tests/integration/test_*.py,
#                   the tests that name a changed migration or import a changed integration helper, and a fixed smoke
#                   file when conftest.py or a helper it imports changed) on a throwaway
#                   Postgres: the port in CIS_TEST_DB_PORT when set (never 5432), else a cluster made with initdb under
#                   $TMPDIR and removed after. A pass on a clean tree writes a stamp for this code; the hook (every mode
#                   but --ci) then blocks a push whose changed integration tests have no stamp. Skip once, visibly:
#                   AA_PREPUSH_SKIP_INTEGRATION=1 git push (say so in the PR body).
#   --self-test     runs the decision logic on fixture diffs; invokes no lint/test tool.
#   --dry-run       prints the changed-file count and the checks it would run, then exits.
#   --only a,b      run only these check ids (see CHECK_IDS, plus gitleaks).
#   gitleaks        AA-1024 G14: every mode but --ci scans the pushed commits (<base>..<head>) with gitleaks v8.30.1 (the
#                   secret-scan.yml pin) and the BASE's .gitleaks.toml / .gitleaksignore, as CI reads them. Binary:
#                   $AA_GITLEAKS, `gitleaks` on PATH, or <workspace>/.tmp-session/gitleaks-8301/gitleaks. A missing or
#                   other-version binary blocks; skip once, visibly: AA_SKIP_GITLEAKS=1 git push.
#   pins            AA-1024 (S274, S276): before a run that starts Python tests (unit, integration, --integration) the
#                   Python it uses must have the fastapi / pydantic versions requirements.txt pins (CI installs them);
#                   another version makes test_aa899_openapi_export red on main too. A mismatch stops at once with the
#                   install line; skip once, visibly: AA_PREPUSH_SKIP_PINS=1 git push.
#   --base REF      diff base (default origin/main, or AA_PREPUSH_BASE).
#   --head REF      commit being pushed (default HEAD; the hook passes the pushed SHA).
#
# Bypass a blocked push once: git push --no-verify
# Install the hook: scripts/install-hooks.sh   Remove it: git config --unset core.hooksPath
set -uo pipefail

SELF=$(realpath "${BASH_SOURCE[0]}")
CHECK_IDS=(resource_ids text_guard quality_lint flake8 unit integration frontend_kit frontend_tsc frontend_lint)

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
CI_CMD[unit]="COVERAGE_CORE=sysmon pytest tests/unit/ -v --tb=short --cov=services --cov=shared --cov-report=term-missing --cov-report=xml:coverage.xml && pytest tests/acp/ -v --tb=short && python3 .github/scripts/coverage_ratchet.py coverage.xml --base <base>   (ci.yml Unit Tests; CI installs: .github/actions/url-wheels, then uv pip install --system -r <its rewritten requirements.txt> -r requirements-dev.txt)"
UNIT_INSTALL="pip install -r requirements.txt -r requirements-dev.txt"
CI_CMD[integration]="scripts/pre-push.sh --integration   (runs the changed integration test files on a throwaway Postgres and stamps this code; CI: Integration shard i/4, postgres:15)"
INTEGRATION_INSTALL="pip install -r requirements.txt -r tests/integration/requirements-integration.txt"
CI_CMD[gitleaks]="gitleaks git . --log-opts=\"--no-renames <base>..<head>\" -c .gitleaks.toml -i .gitleaksignore --redact   (secret-scan.yml gitleaks, rules from the PR base; a finding: rewrite the branch, never fix it in a later commit)"
GITLEAKS_VERSION=8.30.1 # .github/workflows/secret-scan.yml (gitleaks v8.30.1 in every repo)
GITLEAKS_LOCAL=.tmp-session/gitleaks-8301/gitleaks # where the PR opener and the lanes keep the checked download
CI_CMD[frontend_tsc]="cd frontend && npx tsc --noEmit -p .   (ci.yml Frontend Build > Type-check; next build stays CI-only)"
CI_CMD[frontend_kit]="cd frontend && node app/_kit/core/check-kit-copy.mjs --self && node app/_kit/core/check-kit-copy.mjs --css . && node app/_kit/core/check-kit-copy.mjs --suppressions <base eslint-suppressions.json> eslint-suppressions.json --base-files <git ls-tree -r --name-only <base> -- frontend/> && node app/_kit/core/check-kit-copy.mjs --no-kit-disable app --ignore _kit   (ci.yml Frontend Build > Kit core self-check; Frontend Lint > Suppressions do not grow, No inline disable of a kit rule)"
CI_CMD[frontend_lint]="cd frontend && npx eslint --prune-suppressions && git diff --exit-code eslint-suppressions.json   (ci.yml Frontend Lint; locally only the changed files, or all of frontend/ when the lint config, the kit rules or the suppressions file changed; fix, or after fixing old violations: npx eslint --prune-suppressions <files>)"
# Checks that CI runs with continue-on-error: a failure is reported, never blocks.
REPORT_ONLY=" "
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
# --integration: above this many matched integration test files only the changed ones run (each pytest session replays
# every migration once; the cap keeps a run short on a low-RAM machine).
MAX_INTEGRATION_FILES=${AA_PREPUSH_MAX_INTEGRATION_FILES:-12}
# A changed tests/integration/conftest.py, or an integration helper conftest imports (_replay_guard.py, _constants.py),
# changes every integration session (the schema replay and its guards) but no test file names it: the stamp then
# covers this fixed smoke set (one cheap DB test; its session runs the whole replay). CI's shards run the rest.
SMOKE_INTEGRATION=(tests/integration/test_aa1024_replay_smoke.py)

# --- decision logic (pure: file names in, check ids out; used by --self-test) -------------------------------------

# is_doc FILE: Markdown or anything under docs/ (no code check needed beyond the always-on scans).
is_doc() { [[ $1 == docs/* || $1 == *.md ]]; }

# plan_checks MODE CI FILE...: print the check ids to run, one per line, in CHECK_IDS order.
plan_checks() {
  local mode=$1 ci=$2; shift 2
  local f code=0 py_lint=0 py=0 fe=0 it=0
  # --ci: everything but the integration stamp (CI runs its own Integration shards).
  if [[ $ci == 1 ]]; then printf '%s\n' "${CHECK_IDS[@]}" | grep -vx integration; return; fi
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
    case $f in tests/integration/*.py|api/migrations/*.sql) it=1 ;; esac
  done
  echo resource_ids
  echo text_guard
  if [[ $mode == full ]]; then echo quality_lint; echo flake8; echo unit
  else
    [[ $code == 1 ]] && echo quality_lint
    [[ $py_lint == 1 ]] && echo flake8
    [[ $py == 1 ]] && echo unit
  fi
  [[ $it == 1 ]] && echo integration
  [[ $fe == 1 ]] && { echo frontend_kit; echo frontend_tsc; echo frontend_lint; }
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

# integration_targets FILE...: the integration test files a diff touches (--integration runs them, the hook asks for
# their stamp):
#   - a changed tests/integration/test_*.py (still present): itself;
#   - a changed integration helper (tests/integration/_*.py): the test files that import it, plus SMOKE_INTEGRATION
#     when conftest.py imports it;
#   - a changed api/migrations/NNN_name.sql: the test files that name it (NNN_name) and test_*migration_NNN*.py;
#   - a changed tests/integration/conftest.py: SMOKE_INTEGRATION (every file depends on it; CI's shards run them all).
# Prints "OVER <n>" first when more than MAX_INTEGRATION_FILES matched; then only the changed test files and the smoke
# set are listed (the cap never drops them).
integration_targets() {
  local f name stem leaf num direct=() hits=()
  for f in "$@"; do
    name=${f##*/}
    case $f in
      tests/integration/conftest.py) direct+=("${SMOKE_INTEGRATION[@]}") ;;
      tests/integration/test_*.py) [[ -f $f ]] && direct+=("$f") ;;
      tests/integration/_*.py)
        leaf=${name%.py}
        grep -qE "^[[:space:]]*(import|from) ${leaf}\b" tests/integration/conftest.py 2>/dev/null && direct+=("${SMOKE_INTEGRATION[@]}")
        mapfile -t -O "${#hits[@]}" hits < <(grep -rlE --include='test_*.py' "(import|from) ${leaf}\b" tests/integration 2>/dev/null) ;;
      api/migrations/[0-9][0-9][0-9]_*.sql)
        stem=${name%.sql}; num=${name:0:3}
        mapfile -t -O "${#hits[@]}" hits < <(grep -rlF --include='test_*.py' -- "$stem" tests/integration 2>/dev/null)
        mapfile -t -O "${#hits[@]}" hits < <(find tests/integration -maxdepth 1 -name "test_*migration_${num}*.py" 2>/dev/null) ;;
    esac
  done
  mapfile -t hits < <(printf '%s\n' "${hits[@]}" | sed '/^$/d' | sort -u)
  if (( ${#hits[@]} + ${#direct[@]} > MAX_INTEGRATION_FILES )); then
    echo "OVER $(( ${#hits[@]} + ${#direct[@]} ))"
    hits=()
  fi
  printf '%s\n' "${direct[@]}" "${hits[@]}" | sed '/^$/d' | sort -u
}

# integration_key HEAD TARGET...: one hash for "these integration tests on this code": the target list plus the blob
# ids at HEAD of every changed file that is not a doc. A later docs-only commit keeps the stamp; any code change drops it.
integration_key() {
  local head=$1; shift
  local code=() f
  for f in "${CHANGED[@]}"; do is_doc "$f" || code+=("$f"); done
  {
    printf 'target %s\n' "$@"
    (( ${#code[@]} )) && git ls-tree -r "$head" -- "${code[@]}"
  } | sha256sum | cut -c1-40
}

# stamp_dir: per worktree (git-path), so a lane's stamp is read only by pushes from that worktree.
stamp_dir() { git rev-parse --path-format=absolute --git-path aa-prepush/integration; }

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
  local all tmp; all=$(printf '%s\n' "${CHECK_IDS[@]}" | grep -vx integration | tr '\n' ' '); all=${all% }
  expect "empty diff: always-on scans only"        fast 0 "resource_ids text_guard"
  expect "docs only"                               fast 0 "resource_ids text_guard" docs/implementation-notes/AA-1.md README.md
  expect "service module"                          fast 0 "resource_ids text_guard quality_lint flake8 unit" services/quality/lint.py
  expect "api route + doc"                         fast 0 "resource_ids text_guard quality_lint flake8 unit" api/routers/x.py docs/a.md
  expect "test file only: no flake8 (CI scope)"    fast 0 "resource_ids text_guard quality_lint unit" tests/unit/test_x.py
  expect "worker only: no flake8 (CI scope)"       fast 0 "resource_ids text_guard quality_lint unit" worker/main.py
  expect "requirements bump: unit"                 fast 0 "resource_ids text_guard quality_lint unit" requirements.txt
  expect "requirements-dev bump: unit"             fast 0 "resource_ids text_guard quality_lint unit" requirements-dev.txt
  expect "migration sql: unit + integration stamp" fast 0 "resource_ids text_guard quality_lint unit integration" api/migrations/300_x.sql
  expect "integration test: unit + stamp"          fast 0 "resource_ids text_guard quality_lint unit integration" tests/integration/test_x.py
  expect "full mode, integration test: stamp too"  full 0 "resource_ids text_guard quality_lint flake8 unit integration" tests/integration/test_x.py
  expect "test helper module: unit, no flake8"     fast 0 "resource_ids text_guard quality_lint unit" tests/unit/quality/chain.py
  expect "Q0 case json: unit"                      fast 0 "resource_ids text_guard quality_lint unit" tests/unit/quality/cases/a1_contract.json
  expect ".trivyignore: unit"                      fast 0 "resource_ids text_guard quality_lint unit" .trivyignore
  expect "yaml registry: quality lint"             fast 0 "resource_ids text_guard quality_lint" services/quality/registry.yaml
  expect "frontend only: tsc + lint, never build"  fast 0 "resource_ids text_guard quality_lint frontend_kit frontend_tsc frontend_lint" frontend/app/page.tsx
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
  # AA-1024 l22 (G6): integration targets, from the repo's own integration tests (grep only, nothing is run).
  got=$(integration_targets tests/integration/test_aa1002_migration_304.py services/x.py docs/a.md | tr '\n' ' ')
  r=0; [[ $got == "tests/integration/test_aa1002_migration_304.py " ]] || r=1
  verdict "integration targets: a changed integration test is itself, a service module is unit-only" "$got" "$r"
  got=$(MAX_INTEGRATION_FILES=1000 integration_targets api/migrations/304_llm_connection_deployment.sql | tr '\n' ' ')
  r=0; [[ $got == *tests/integration/test_aa1002_migration_304.py* ]] || r=1
  verdict "integration targets: a migration maps to the tests that replay it" "$got" "$r"
  got=$(MAX_INTEGRATION_FILES=1000 integration_targets tests/integration/_rls_fixture_helpers.py | tr '\n' ' ')
  r=0; [[ $got == *tests/integration/test_aa919_migration_284.py* && $got != *test_aa1002_* ]] || r=1
  verdict "integration targets: a helper maps to its importers only" "$got" "$r"
  got=$(integration_targets tests/integration/conftest.py | tr '\n' ' ')
  r=0; [[ $got == "${SMOKE_INTEGRATION[*]} " && -f ${SMOKE_INTEGRATION[0]} ]] || r=1
  verdict "integration conftest: the smoke set needs a stamp, not a WARN" "$got" "$r"
  got=$(integration_targets tests/integration/_replay_guard.py | tr '\n' ' ')
  r=0; [[ $got == *"${SMOKE_INTEGRATION[0]}"* ]] || r=1; verdict "a helper conftest imports: the smoke set too" "$got" "$r"
  got=$(MAX_INTEGRATION_FILES=1000 integration_targets tests/integration/_rls_fixture_helpers.py | tr '\n' ' ')
  r=0; [[ $got != *"${SMOKE_INTEGRATION[0]}"* ]] || r=1; verdict "a helper conftest does not import: no smoke set" "$got" "$r"
  got=$(MAX_INTEGRATION_FILES=0 integration_targets tests/integration/test_aa1002_migration_304.py | head -1)
  r=0; [[ $got == OVER* ]] || r=1; verdict "integration targets over the cap: OVER first" "$got" "$r"
  # The stamp key: same code = same key, a code change = another key, a docs-only change = the same key.
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/pre-push-key.XXXXXX")
  got=$(
    cd "$tmp" && git init -q . && mkdir -p tests/integration docs && echo a >tests/integration/test_k.py &&
      git add -A && git -c user.name=t -c user.email=t@t commit -q -m a &&
      CHANGED=(tests/integration/test_k.py docs/n.md) && k1=$(integration_key HEAD tests/integration/test_k.py) &&
      echo n >docs/n.md && git add -A && git -c user.name=t -c user.email=t@t commit -q -m d &&
      k2=$(integration_key HEAD tests/integration/test_k.py) &&
      echo b >tests/integration/test_k.py && git add -A && git -c user.name=t -c user.email=t@t commit -q -m b &&
      k3=$(integration_key HEAD tests/integration/test_k.py) &&
      echo "$([[ $k1 == "$k2" ]] && echo same)-$([[ $k2 != "$k3" ]] && echo new)"
  )
  rm -rf "$tmp"
  r=0; [[ $got == same-new ]] || r=1; verdict "integration stamp key: docs keep it, code changes it" "$got" "$r"
  # A throwaway repo (no network, no tool): a non-ASCII name with a space comes back raw, not C-quoted.
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/pre-push-st.XXXXXX")
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

# check_pins: the Python that runs the tests has the versions requirements.txt pins for PINNED_PKGS (CI installs those
# pins; S274 + S276: the main checkout's .venv kept fastapi 0.110 / pydantic 2.13 against pins 0.161.1 / 2.14.1, so
# test_aa899_openapi_export failed locally on main and blocked every push). Returns 1 with the install line on a
# mismatch; AA_PREPUSH_SKIP_PINS=1 turns that into a warning in the summary.
PINNED_PKGS=(fastapi pydantic)
check_pins() {
  [[ -f requirements.txt ]] || return 0
  local probe pkg pin have bad=() pattern msg
  probe=$("$PY" -c 'import sys
from importlib import metadata  # AA_PINS probe: one interpreter start for every package
for n in sys.argv[1:]:
    try:
        print(n, metadata.version(n))
    except metadata.PackageNotFoundError:
        print(n, "not-installed")' "${PINNED_PKGS[@]}" 2>/dev/null)
  for pkg in "${PINNED_PKGS[@]}"; do
    pin=$(sed -n "s/^$pkg==\([^ #;]*\).*/\1/p" requirements.txt | head -n 1)
    [[ -n $pin ]] || continue
    have=$(awk -v n="$pkg" '$1 == n { print $2 }' <<<"$probe")
    [[ $have == "$pin" ]] || bad+=("$pkg ${have:-unknown} (requirements.txt pins $pin)")
  done
  (( ${#bad[@]} == 0 )) && return 0
  pattern=$(IFS='|'; echo "${PINNED_PKGS[*]}")
  msg="$PY has $(printf '%s, ' "${bad[@]}" | sed 's/, $//')"
  if [[ ${AA_PREPUSH_SKIP_PINS:-0} == 1 ]]; then
    warn_result pins "$msg; skipped by AA_PREPUSH_SKIP_PINS=1 (say so in the PR body)"
    return 0
  fi
  echo "!! [pins] $msg."
  echo "   CI installs the requirements.txt pins: with other versions tests such as test_aa899_openapi_export (the"
  echo "   openapi.json snapshot) fail here and pass in CI, or the other way round. Install the pins, then retry:"
  echo "   \"$PY\" -m pip install \$(grep -E '^($pattern)==' requirements.txt)"
  echo "   (a uv venv without pip: uv pip install --python \"$PY\" \$(grep -E '^($pattern)==' requirements.txt))"
  echo "   Skip once, visibly: AA_PREPUSH_SKIP_PINS=1 git push"
  return 1
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

# read_integration_targets: INTEGRATION_TARGETS from the diff; notes (OVER / WARN) are printed.
read_integration_targets() {
  local line
  INTEGRATION_TARGETS=()
  while IFS= read -r line; do
    case $line in
      OVER*) echo "note: ${line#OVER } integration test files match the changed files (cap $MAX_INTEGRATION_FILES); only the changed test files count." ;;
      WARN*) warn_result integration "${line#WARN }" ;;
      *) INTEGRATION_TARGETS+=("$line") ;;
    esac
  done < <(integration_targets "${CHANGED[@]}")
}

# do_integration (hook, fast/full): the changed integration tests must have passed locally on this code
# (scripts/pre-push.sh --integration wrote the stamp). The hook never starts Postgres: a push stays fast.
do_integration() {
  read_integration_targets
  if (( ${#INTEGRATION_TARGETS[@]} == 0 )); then RESULTS+=("SKIP  -     integration (no integration test file matches the diff)"); return; fi
  local key stamp; key=$(integration_key "$HEAD_REF" "${INTEGRATION_TARGETS[@]}"); stamp="$(stamp_dir)/$key"
  if [[ -f $stamp ]]; then
    RESULTS+=("PASS  -     integration (${#INTEGRATION_TARGETS[@]} file(s) passed locally: $(head -n 1 "$stamp"))")
    return
  fi
  if [[ ${AA_PREPUSH_SKIP_INTEGRATION:-0} == 1 ]]; then
    warn_result integration "AA_PREPUSH_SKIP_INTEGRATION=1: ${#INTEGRATION_TARGETS[@]} changed integration test file(s) not run locally; say so in the PR body"
    return
  fi
  fail_check integration "${#INTEGRATION_TARGETS[@]} integration test file(s) the diff touches have not passed locally on this code: ${INTEGRATION_TARGETS[*]}" \
    "run: scripts/pre-push.sh --integration   (throwaway Postgres; or skip once: AA_PREPUSH_SKIP_INTEGRATION=1, and say so in the PR body)"
}

# free_port: a TCP port nothing listens on (never 5432).
free_port() {
  "$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

# find_initdb: AA_PREPUSH_INITDB when set (tests), else the newest local initdb (Debian layout first), or nothing.
find_initdb() {
  local c
  [[ -n ${AA_PREPUSH_INITDB:-} ]] && { echo "$AA_PREPUSH_INITDB"; return; }
  for c in $(ls -d /usr/lib/postgresql/*/bin/initdb 2>/dev/null | sort -V -r); do [[ -x $c ]] && { echo "$c"; return; }; done
  command -v initdb 2>/dev/null || true
}

PG_TMP=""
PG_BIN=""
stop_scratch_pg() {
  [[ -n $PG_TMP ]] || return 0
  "$PG_BIN/pg_ctl" -D "$PG_TMP/data" -m fast -w stop >/dev/null 2>&1
  rm -rf "$PG_TMP"
  PG_TMP=""
}

# start_scratch_pg: a throwaway cluster (superuser cistest, database cis_integration_test, role app_user as CI's setup
# step) on a free port; sets PG_PORT. Lesson S246: no "local Postgres has no superuser" excuse. Call it directly, never
# in $(...): PG_TMP / PG_BIN must reach this shell, or stop_scratch_pg cannot stop the cluster (it leaked, S271 l22).
PG_PORT=""
start_scratch_pg() {
  local initdb port; initdb=$(find_initdb)
  [[ -n $initdb ]] || return 1
  PG_BIN=${initdb%/initdb}
  PG_TMP=$(mktemp -d "${TMPDIR:-/tmp}/pre-push-pg.XXXXXX") || return 1
  port=$(free_port) || return 1
  "$initdb" -D "$PG_TMP/data" -U postgres -A trust >"$PG_TMP/initdb.log" 2>&1 || { cat "$PG_TMP/initdb.log" >&2; return 1; }
  "$PG_BIN/pg_ctl" -D "$PG_TMP/data" -l "$PG_TMP/pg.log" -w \
    -o "-p $port -c listen_addresses=127.0.0.1 -c unix_socket_directories='' -c fsync=off" start >/dev/null || {
      cat "$PG_TMP/pg.log" >&2; return 1; }
  "$PG_BIN/psql" -q -h 127.0.0.1 -p "$port" -U postgres -d postgres -v ON_ERROR_STOP=1 \
    -c "CREATE USER cistest WITH PASSWORD 'cistest' SUPERUSER" \
    -c "CREATE DATABASE cis_integration_test OWNER cistest" \
    -c "CREATE ROLE app_user LOGIN PASSWORD 'appuser123'" >/dev/null || return 1
  PG_PORT=$port
}

# run_integration (--integration): run INTEGRATION_TARGETS on CIS_TEST_DB_PORT or a throwaway cluster; on a pass with
# a clean tracked tree, write the stamp the hook reads.
run_integration() {
  read_integration_targets
  if (( ${#INTEGRATION_TARGETS[@]} == 0 )); then echo "pre-push --integration: no integration test file matches the diff vs $BASE; nothing to run."; return 0; fi
  if ! "$PY" -c 'import psycopg2, fakeredis, pytest_asyncio' >/dev/null 2>&1; then
    echo "!! [integration] psycopg2 / fakeredis / pytest-asyncio are not installed for $PY"
    echo "   install: \"$PY\" -m $INTEGRATION_INSTALL"
    return 1
  fi
  local port=${CIS_TEST_DB_PORT:-} rc dirty
  if [[ $port == 5432 ]]; then
    echo "!! [integration] CIS_TEST_DB_PORT=5432: the conftest drops and re-creates schemas; use a throwaway cluster (lesson S248)."
    return 1
  fi
  if [[ -z $port ]]; then
    trap stop_scratch_pg EXIT
    start_scratch_pg || {
      echo "!! [integration] no Postgres: set CIS_TEST_DB_PORT to a throwaway cluster, or install PostgreSQL (initdb) locally"
      return 1
    }
    port=$PG_PORT
    echo "throwaway Postgres on 127.0.0.1:$port ($PG_TMP, removed after the run)"
  fi
  echo "==> [integration] pytest on ${#INTEGRATION_TARGETS[@]} file(s): ${INTEGRATION_TARGETS[*]}"
  env -u AWS_PROFILE "${UNIT_ENV[@]}" CIS_TEST_DB_HOST=127.0.0.1 CIS_TEST_DB_PORT="$port" \
    DATABASE_URL="postgresql://cistest:${CIS_TEST_DB_PASS:-cistest}@127.0.0.1:$port/cis_integration_test" REDIS_URL=redis://localhost:6379 \
    AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true \
    "$PY" -m pytest -q --tb=short -p no:cacheprovider "${INTEGRATION_TARGETS[@]}"
  rc=$?
  stop_scratch_pg
  if (( rc != 0 )); then echo "!! [integration] failed (exit $rc): no stamp written"; return "$rc"; fi
  dirty=$(git status --porcelain --untracked-files=no 2>/dev/null)
  if [[ -n $dirty ]]; then
    echo "note: passed, but tracked files are modified: commit them and run --integration again (the stamp names committed code)."
    return 0
  fi
  local key dir; key=$(integration_key "$HEAD_REF" "${INTEGRATION_TARGETS[@]}"); dir=$(stamp_dir)
  mkdir -p "$dir" && printf '%s %s on %s\n%s\n' "$(date -u +%FT%TZ)" "$(git rev-parse --short "$HEAD_REF")" \
    "$("$PY" -c 'import sys; print(sys.version.split()[0])')" "${INTEGRATION_TARGETS[*]}" >"$dir/$key"
  echo "integration: passed; stamp $key written (the pre-push hook accepts this code's changed integration tests)."
}

# do_frontend_kit: the dependency-free kit checks (no node_modules needed): core self-check, no --aa-* definition
# outside core/tokens.css, no inline disable of a kit rule in page code, and the ESLint suppressions file does not
# grow against the diff base (new entries only in files the base did not have) (CI: Frontend Build "Kit core
# self-check" + Frontend Lint "Suppressions do not grow" and "No inline disable of a kit rule").
do_frontend_kit() {
  if ! command -v node >/dev/null 2>&1; then fail_check frontend_kit "node is not installed" "install Node 22 (CI: setup-node 22)"; return; fi
  local base_file=$LOG_DIR/base-eslint-suppressions.json base_list=$LOG_DIR/base-frontend-files.txt supp=(true)
  if git show "$BASE:frontend/eslint-suppressions.json" >"$base_file" 2>/dev/null \
    && git ls-tree -r --name-only "$BASE" -- frontend/ | sed 's#^frontend/##' >"$base_list"; then
    supp=(node app/_kit/core/check-kit-copy.mjs --suppressions "$base_file" eslint-suppressions.json --base-files "$base_list")
  fi
  run_check frontend_kit "kit core self-check + --css + no kit disable + suppressions growth vs $BASE (frontend/)" bash -c '
    cd frontend || exit 2
    node app/_kit/core/check-kit-copy.mjs --self || exit $?
    node app/_kit/core/check-kit-copy.mjs --css . || exit $?
    node app/_kit/core/check-kit-copy.mjs --no-kit-disable app --ignore _kit || exit $?
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

# gitleaks_bin: $AA_GITLEAKS, gitleaks on PATH, or the workspace copy in a parent directory of this checkout (the repo
# sits at <workspace>/apps/AA-CIS-App, a lane worktree under <workspace>/.tmp-session/wt/).
gitleaks_bin() {
  local d
  if [[ -n ${AA_GITLEAKS:-} ]]; then echo "$AA_GITLEAKS"; return 0; fi
  command -v gitleaks 2>/dev/null && return 0
  d=$(pwd -P)
  while [[ -n $d && $d != / ]]; do
    if [[ -x $d/$GITLEAKS_LOCAL ]]; then echo "$d/$GITLEAKS_LOCAL"; return 0; fi
    d=$(dirname "$d")
  done
  return 0
}

# do_gitleaks: scan the pushed commits the way secret-scan.yml does (lessons S250-S266, CIS #1066 / #1109 / #1181: a
# fix-up push to a PR branch was not scanned before it reached GitHub, and each leak cost a squash rewrite).
do_gitleaks() {
  if [[ ${AA_SKIP_GITLEAKS:-} == 1 ]]; then
    warn_result gitleaks "skipped (AA_SKIP_GITLEAKS=1): CI's Secret Scan is the only scan of these commits"; return
  fi
  local exe have rules name hint
  hint="install gitleaks v$GITLEAKS_VERSION (github.com/gitleaks/gitleaks/releases, linux_x64 tar.gz checked against the release checksums) on PATH or at <workspace>/$GITLEAKS_LOCAL, or set AA_GITLEAKS; skip once: AA_SKIP_GITLEAKS=1 git push"
  exe=$(gitleaks_bin)
  if [[ -z $exe ]]; then fail_check gitleaks "no gitleaks binary" "$hint"; return; fi
  have=$("$exe" version 2>/dev/null); have=${have#v}
  if [[ $have != "$GITLEAKS_VERSION" ]]; then
    fail_check gitleaks "gitleaks ${have:-unreadable} at $exe; CI uses $GITLEAKS_VERSION" "$hint"; return
  fi
  # The rules come from the BASE (as CI reads them), so a branch cannot allowlist its own leak.
  rules="$LOG_DIR/gitleaks-rules"; mkdir -p "$rules"
  for name in .gitleaks.toml .gitleaksignore; do
    git show "$BASE:$name" >"$rules/$name" 2>/dev/null || : >"$rules/$name"
  done
  local cfg=(); [[ -s $rules/.gitleaks.toml ]] && cfg=(-c "$rules/.gitleaks.toml")
  run_check gitleaks "gitleaks $GITLEAKS_VERSION on $BASE..$HEAD_REF (rules from $BASE)" \
    "$exe" git . --log-opts="--no-renames $BASE..$HEAD_REF" "${cfg[@]}" -i "$rules/.gitleaksignore" --redact --no-banner
}

main() {
  cd "$(git rev-parse --show-toplevel)" || exit 2
  # S272 incident: git exports GIT_DIR (and friends) to a hook. A test that runs `git init` / `git config` / `git commit`
  # in a temp dir with the inherited env then writes into THIS clone (core.bare=true, user=ci, fake commits, a rewritten
  # origin/main shared by every worktree). Drop them once cwd is the top level, so git finds the repo by itself.
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES \
    GIT_NAMESPACE GIT_PREFIX GIT_CEILING_DIRECTORIES GIT_DISCOVERY_ACROSS_FILESYSTEM
  MODE=fast CI_MODE=0 DRY=0 ONLY="" HEAD_REF=HEAD BASE=${AA_PREPUSH_BASE:-origin/main} RUN_INTEGRATION=0
  [[ ${AA_PREPUSH:-} == full ]] && MODE=full
  while (( $# )); do
    case $1 in
      --full) MODE=full ;;
      --fast) MODE=fast ;;
      --ci) CI_MODE=1; MODE=full ;;
      --self-test) self_test; exit $? ;;
      --integration) RUN_INTEGRATION=1 ;;
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
  if [[ $RUN_INTEGRATION == 1 ]]; then
    if [[ $CI_MODE == 1 ]]; then echo "pre-push: --integration reads the diff; it does not combine with --ci" >&2; exit 2; fi
    RESULTS=()
    check_pins || exit 1
    run_integration; exit $?
  fi
  local plan=() id
  mapfile -t plan < <(plan_checks "$MODE" "$CI_MODE" "${CHANGED[@]}")
  # AA-1024 G14: every local push scans its commits (CI runs its own Secret Scan job, so not in --ci).
  [[ $CI_MODE == 0 ]] && plan+=(gitleaks)
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
  # Stop before minutes of tests that fail (or pass) only because of a local version drift; CI installs the pins.
  if [[ $CI_MODE == 0 && " ${plan[*]} " =~ \ (unit|integration)\  ]]; then
    check_pins || { echo; echo "Blocked before the checks ran (pins). Bypass the hook once: git push --no-verify"; exit 1; }
  fi
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
      integration) do_integration ;;
      frontend_kit) do_frontend_kit ;;
      frontend_tsc) node_check frontend "$id" npx tsc --noEmit -p . ;;
      frontend_lint) do_frontend_lint ;;
      gitleaks) do_gitleaks ;;
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
