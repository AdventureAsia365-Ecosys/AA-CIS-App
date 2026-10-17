#!/usr/bin/env bash
# AA-725 / AA-868 — pre-deploy job guard, run by deploy-dev.yml before the containers roll.
#
# A deploy restarts api + worker and would interrupt any queued/running shared.job (S212). CI has no
# RDS access, so this asks the live API's deploy gate. Fails CLOSED (AA-868): a missing credential, an
# unreachable gate or an unreadable answer blocks the deploy, like active jobs do. OVERRIDE (a reason,
# workflow_dispatch only, Nghiệp only) lets a deploy through anyway: it is also the way out when the API
# itself is down and the deploy is the fix.
#
# Env: ADMIN_SECRET, GATE_URL, OVERRIDE (optional). Exit 0 = deploy, 1 = blocked.
set -uo pipefail

allow_override() {
  if [ -n "${OVERRIDE:-}" ]; then
    echo "::warning::Deploy guard OVERRIDDEN ($1). Reason: ${OVERRIDE}"
    exit 0
  fi
  echo "::error::Deploy blocked: $1. Fix it, or re-run via workflow_dispatch with an 'override_running_jobs' reason."
  exit 1
}

[ -n "${ADMIN_SECRET:-}" ] || allow_override "ADMIN_SECRET is not configured, the job guard cannot run"
[ -n "${GATE_URL:-}" ] || allow_override "GATE_URL is not set"

BODY=$(curl -sS --fail --max-time 20 --retry 2 --retry-delay 5 -H "x-admin-secret: ${ADMIN_SECRET}" "${GATE_URL}") \
  || allow_override "the deploy gate did not answer 2xx"
echo "deploy-gate: ${BODY}"

SAFE=$(printf '%s' "${BODY}" | jq -r '.safe_to_deploy // empty' 2>/dev/null) \
  || allow_override "the deploy gate answer is not JSON"
ACTIVE=$(printf '%s' "${BODY}" | jq -r '.active // "?"' 2>/dev/null)

case "${SAFE}" in
  true)
    echo "No queued/running jobs (deploy-gate exempt kinds not counted) — safe to deploy."
    exit 0 ;;
  false)
    allow_override "${ACTIVE} job(s) queued/running" ;;
  *)
    allow_override "the deploy gate answer has no safe_to_deploy" ;;
esac
