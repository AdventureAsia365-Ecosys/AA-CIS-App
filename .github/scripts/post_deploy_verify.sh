#!/usr/bin/env bash
# AA-993 V1 (ADR 0016 D3) — CIS post-deploy verify, run by .github/workflows/post-deploy-verify.yml after every
# Deploy Dev (and on dispatch). Red/green, no warnings: every check below must pass or the script exits 1.
#
# Checks (each gets VERIFY_TRIES tries, VERIFY_RETRY_DELAY seconds apart):
#   inputs        every required input is set and well formed
#   admin-secret  ADMIN_SECRET is set (red when missing; the admin and worker rows then report "not run")
#   ECS, api and worker service:
#     <svc>-rollout    service ACTIVE, desired >= 1 (0 is red, no scale-up), running = desired, one deployment,
#                      PRIMARY rolloutState COMPLETED
#     <svc>-image      the running task definition's image = EXPECTED_IMAGE (<registry>/<repo>:dev-<sha>)
#     same-image       api and worker run one image
#   HTTP rows     from ROWS_FILE (.github/verify/cis.rows): name|auth|METHOD path|expected HTTP|jq assertion
#   worker-sha    a live worker (/admin/job-runner/workers) runs EXPECTED_SHA
#   worker-kinds  the caps of the live workers on EXPECTED_SHA = the registered kinds (/admin/job-runner/summary)
#
# Env: EXPECTED_SHA, EXPECTED_VERSION, REQUIRED_SCHEMA, EXPECTED_IMAGE, ADMIN_SECRET, ECS_CLUSTER, ECS_SERVICE,
#      ECS_WORKER_SERVICE, AWS_REGION; optional BASE, ROWS_FILE, VERIFY_TRIES (3), VERIFY_RETRY_DELAY (10),
#      VERIFY_INITIAL_WAIT (10), GITHUB_STEP_SUMMARY. Needs bash, curl, jq, aws.
# Public repo: never print the registry (only the image tag) or an admin response body.
# Exit 0 = every check passed, 1 = at least one failed.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${BASE:-https://api-cis.lumiguides.it.com}"
ROWS_FILE="${ROWS_FILE:-${HERE}/../verify/cis.rows}"
TRIES="${VERIFY_TRIES:-3}"
DELAY="${VERIFY_RETRY_DELAY:-10}"
INITIAL_WAIT="${VERIFY_INITIAL_WAIT:-10}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

ROWS=()   # "name<TAB>PASS|FAIL<TAB>detail"
FAILED=0
DETAIL=""

record() {  # name, PASS|FAIL, detail
  ROWS+=("$1"$'\t'"$2"$'\t'"$3")
  if [ "$2" = "PASS" ]; then
    echo "verify [$1] OK: $3"
  else
    echo "::error::verify [$1] $3"
    FAILED=1
  fi
}

retry() {  # name, function, args... — the function sets DETAIL and returns 0 (pass) / 1 (fail)
  local name="$1"; shift
  local try
  for try in $(seq 1 "$TRIES"); do
    DETAIL=""
    if "$@"; then
      record "$name" PASS "$DETAIL"
      return 0
    fi
    [ "$try" -lt "$TRIES" ] && { echo "verify [$name] try $try/$TRIES failed: $DETAIL"; sleep "$DELAY"; }
  done
  record "$name" FAIL "$DETAIL (after $TRIES tries)"
  return 1
}

tag_of() { printf '%s' "${1##*:}"; }  # the image tag only: the registry part is a secret

# ── inputs ────────────────────────────────────────────────────────────────────────────────────────────────────────
check_inputs() {  # sets BLOCKED=1 when the ECS/HTTP checks cannot run at all
  local missing=() name
  for name in EXPECTED_SHA EXPECTED_VERSION REQUIRED_SCHEMA EXPECTED_IMAGE ECS_CLUSTER ECS_SERVICE \
              ECS_WORKER_SERVICE AWS_REGION; do
    [ -n "${!name:-}" ] || missing+=("$name")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    record inputs FAIL "not set: ${missing[*]}"
    BLOCKED=1
  else
    record inputs PASS "commit, version, schema, image and ECS names set"
  fi
  # A missing admin secret is red (it used to skip the admin rows with a warning); the public and ECS checks
  # still run so the table shows everything else.
  if [ -n "${ADMIN_SECRET:-}" ]; then
    record admin-secret PASS "ADMIN_SECRET is set"
  else
    record admin-secret FAIL "ADMIN_SECRET is not set: the admin and worker rows cannot run"
  fi
  if [ -n "${EXPECTED_SHA:-}" ] && ! [[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    record expected-sha FAIL "EXPECTED_SHA must be a full 40-character commit SHA (value not printed)"
    BLOCKED=1
  fi
  if [ -n "${REQUIRED_SCHEMA:-}" ] && ! [[ "$REQUIRED_SCHEMA" =~ ^[0-9]+$ ]]; then
    record required-schema FAIL "REQUIRED_SCHEMA must be a number (value not printed)"
    BLOCKED=1
  fi
  # Only a validated SHA is ever interpolated (an invalid value is never echoed; AA-993 V2 review).
  if [ -n "${EXPECTED_IMAGE:-}" ] && [[ "${EXPECTED_SHA:-}" =~ ^[0-9a-f]{40}$ ]] \
     && [ "$(tag_of "$EXPECTED_IMAGE")" != "dev-${EXPECTED_SHA}" ]; then
    record expected-image FAIL "EXPECTED_IMAGE tag is not dev-<expected sha> (tag not printed)"
    BLOCKED=1
  fi
}

# ── ECS ───────────────────────────────────────────────────────────────────────────────────────────────────────────
describe_service() {  # $1 = service -> $WORK/svc-$1.json ({"services":[...]})
  aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$1" --region "$AWS_REGION" --output json \
    > "$WORK/svc-$1.json" 2> "$WORK/svc-$1.err"
}

check_rollout() {  # $1 = service
  local svc="$1" f="$WORK/svc-$1.json" status desired running ndeps rollout
  if ! describe_service "$svc"; then
    DETAIL="describe-services failed: $(sed -E 's/arn:aws:[^ ]*/<arn>/g; s/[0-9]{12}/<acct>/g' "$WORK/svc-$svc.err" | head -c 200)"; return 1
  fi
  status=$(jq -r '.services[0].status // "MISSING"' "$f")
  if [ "$status" != "ACTIVE" ]; then DETAIL="service $svc is $status"; return 1; fi
  desired=$(jq -r '.services[0].desiredCount // 0' "$f")
  running=$(jq -r '.services[0].runningCount // 0' "$f")
  ndeps=$(jq -r '.services[0].deployments // [] | length' "$f")
  rollout=$(jq -r '[.services[0].deployments // [] | .[] | select(.status == "PRIMARY")][0].rolloutState // "none"' "$f")
  DETAIL="desired=$desired running=$running deployments=$ndeps rolloutState=$rollout"
  if [ "$desired" -lt 1 ]; then
    DETAIL="$DETAIL — desired count 0 (scaling Dev up is a separate approved step)"; return 1
  fi
  [ "$running" = "$desired" ] && [ "$ndeps" = "1" ] && [ "$rollout" = "COMPLETED" ]
}

running_image() {  # $1 = service -> image of the service's task definition on stdout
  local svc="$1" td
  td=$(jq -r '.services[0].taskDefinition // empty' "$WORK/svc-$svc.json" 2>/dev/null)
  [ -n "$td" ] || return 1
  aws ecs describe-task-definition --task-definition "$td" --region "$AWS_REGION" --output json 2>/dev/null \
    | jq -r '.taskDefinition.containerDefinitions[0].image // empty'
}

check_image() {  # $1 = service, $2 = variable name to store the image in
  local svc="$1" img
  describe_service "$svc" || { DETAIL="describe-services failed"; return 1; }
  img=$(running_image "$svc")
  printf -v "$2" '%s' "$img"
  if [ -z "$img" ]; then DETAIL="no running task definition image for $svc"; return 1; fi
  DETAIL="running tag $(tag_of "$img"), built tag $(tag_of "$EXPECTED_IMAGE")"
  [ "$img" = "$EXPECTED_IMAGE" ]
}

check_same_image() {
  DETAIL="api tag $(tag_of "${API_IMAGE:-none}"), worker tag $(tag_of "${WORKER_IMAGE:-none}")"
  [ -n "${API_IMAGE:-}" ] && [ "${API_IMAGE:-}" = "${WORKER_IMAGE:-}" ]
}

# ── HTTP ──────────────────────────────────────────────────────────────────────────────────────────────────────────
http_get() {  # $1 = auth, $2 = path, $3 = out file -> HTTP code on stdout
  local hdr=()
  [ "$1" = "admin" ] && hdr=(-H "x-admin-secret: ${ADMIN_SECRET}")
  curl -s --max-time 25 -o "$3" -w '%{http_code}' "${hdr[@]}" "${BASE}$2" 2>/dev/null || true
}

check_row() {  # auth, method, path, expected http, jq assertion
  local auth="$1" method="$2" path="$3" want="$4" expr="$5" out="$WORK/body.json" code
  if [ "$method" != "GET" ]; then DETAIL="only GET rows are allowed, got $method"; return 1; fi
  case "$auth" in public|admin) ;; *) DETAIL="auth must be public or admin, got '$auth'"; return 1 ;; esac
  rm -f "$out"
  code=$(http_get "$auth" "$path" "$out")
  DETAIL="GET $path -> HTTP ${code:-none}"
  if [ "$code" != "$want" ]; then
    DETAIL="$DETAIL, expected $want"
    [ "$auth" = "public" ] && [ -s "$out" ] && DETAIL="$DETAIL, body $(head -c 200 "$out" | tr -d '\n')"
    return 1
  fi
  [ -n "$expr" ] || return 0
  if ! jq -e --arg sha "$EXPECTED_SHA" --arg version "$EXPECTED_VERSION" --arg required "$REQUIRED_SCHEMA" \
       "$expr" "$out" >/dev/null 2>&1; then
    DETAIL="$DETAIL but the assertion is false: $expr"
    [ "$auth" = "public" ] && [ -s "$out" ] && DETAIL="$DETAIL; body $(head -c 300 "$out" | tr -d '\n')"
    return 1
  fi
  DETAIL="$DETAIL, assertion true"
}

run_rows() {
  local line name auth req want expr method path
  if [ ! -f "$ROWS_FILE" ]; then record rows FAIL "rows file not found: $ROWS_FILE"; return; fi
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    IFS='|' read -r name auth req want expr <<< "$line"
    method="${req%% *}"; path="${req#* }"
    if [ "$auth" = "admin" ] && [ -z "${ADMIN_SECRET:-}" ]; then
      record "$name" FAIL "not run: ADMIN_SECRET is not set"
      continue
    fi
    retry "$name" check_row "$auth" "$method" "$path" "$want" "$expr"
  done < "$ROWS_FILE"
}

# ── worker ────────────────────────────────────────────────────────────────────────────────────────────────────────
check_worker_sha() {
  local code on_sha
  code=$(http_get admin /admin/job-runner/workers "$WORK/workers.json")
  if [ "$code" != "200" ]; then DETAIL="GET /admin/job-runner/workers -> HTTP ${code:-none}"; return 1; fi
  on_sha=$(jq --arg sha "$EXPECTED_SHA" '[.workers[]? | select(.alive and .git_sha == $sha)] | length' \
             "$WORK/workers.json" 2>/dev/null)
  DETAIL="live workers on this SHA: ${on_sha:-0}; live worker SHAs: $(jq -c '[.workers[]? | select(.alive) | .git_sha]' \
    "$WORK/workers.json" 2>/dev/null)"
  [ "${on_sha:-0}" -ge 1 ]
}

check_worker_kinds() {
  local code reg caps
  code=$(http_get admin /admin/job-runner/summary "$WORK/summary.json")
  if [ "$code" != "200" ]; then DETAIL="GET /admin/job-runner/summary -> HTTP ${code:-none}"; return 1; fi
  code=$(http_get admin /admin/job-runner/workers "$WORK/workers.json")
  if [ "$code" != "200" ]; then DETAIL="GET /admin/job-runner/workers -> HTTP ${code:-none}"; return 1; fi
  reg=$(jq -c '[.kinds[]?.kind] | sort' "$WORK/summary.json" 2>/dev/null)
  caps=$(jq -c --arg sha "$EXPECTED_SHA" \
           '[.workers[]? | select(.alive and .git_sha == $sha) | (.caps // {} | keys[])] | unique | sort' \
           "$WORK/workers.json" 2>/dev/null)
  if [ -n "$reg" ] && [ "$reg" = "$caps" ] && [ "$reg" != "[]" ]; then
    DETAIL="$(jq 'length' <<< "$reg") registered kinds, all served by the live worker(s) on this SHA"
    return 0
  fi
  DETAIL="registered kinds $reg != caps of the live worker(s) on this SHA $caps"
  return 1
}

# ── summary ───────────────────────────────────────────────────────────────────────────────────────────────────────
write_summary() {
  local out="${GITHUB_STEP_SUMMARY:-/dev/stdout}" verdict="PASS" row name status detail mark
  [ "$FAILED" = "0" ] || verdict="FAIL"
  {
    echo "### CIS post-deploy verify: ${verdict}"
    echo
    local shown_sha="unset" shown_tag
    [ -n "${EXPECTED_SHA:-}" ] && shown_sha="invalid"
    [[ "${EXPECTED_SHA:-}" =~ ^[0-9a-f]{40}$ ]] && shown_sha="$EXPECTED_SHA"
    shown_tag=$(tag_of "${EXPECTED_IMAGE:-unset}")
    [[ "$shown_tag" =~ ^(dev-[0-9a-f]{40}|unset)$ ]] || shown_tag="invalid"
    echo "Expected commit \`${shown_sha}\`, version \`${EXPECTED_VERSION:-unset}\`," \
         "required schema \`${REQUIRED_SCHEMA:-unset}\`, image tag \`${shown_tag}\`."
    echo
    echo "| Check | Result | Detail |"
    echo "|---|---|---|"
    for row in "${ROWS[@]}"; do
      IFS=$'\t' read -r name status detail <<< "$row"
      mark="PASS"; [ "$status" = "PASS" ] || mark="**FAIL**"
      echo "| ${name} | ${mark} | ${detail//|/\\|} |"
    done
  } >> "$out"
}

# ── main ──────────────────────────────────────────────────────────────────────────────────────────────────────────
BLOCKED=0
for tool in curl jq aws; do
  command -v "$tool" >/dev/null 2>&1 || { record "tool-$tool" FAIL "$tool is not installed"; BLOCKED=1; }
done
check_inputs

if [ "$BLOCKED" = "0" ]; then
  [ "$INITIAL_WAIT" -gt 0 ] && sleep "$INITIAL_WAIT"
  API_IMAGE=""; WORKER_IMAGE=""
  retry api-rollout check_rollout "$ECS_SERVICE"
  retry api-image check_image "$ECS_SERVICE" API_IMAGE
  retry worker-rollout check_rollout "$ECS_WORKER_SERVICE"
  retry worker-image check_image "$ECS_WORKER_SERVICE" WORKER_IMAGE
  retry same-image check_same_image
  run_rows
  if [ -n "${ADMIN_SECRET:-}" ]; then
    retry worker-sha check_worker_sha
    retry worker-kinds check_worker_kinds
  else
    record worker-sha FAIL "not run: ADMIN_SECRET is not set"
    record worker-kinds FAIL "not run: ADMIN_SECRET is not set"
  fi
else
  # The inputs are wrong: nothing below can be checked, and the table says so.
  record checks FAIL "not run: fix the inputs above"
fi

write_summary
if [ "$FAILED" = "0" ]; then
  echo "All post-deploy checks passed."
  exit 0
fi
echo "Post-deploy verify FAILED (see the table in the job summary)."
exit 1
