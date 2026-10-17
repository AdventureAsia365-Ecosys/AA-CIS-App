#!/usr/bin/env bash
# AA-868 — blocking pip-audit for CIS (the required "Security Audit" check used to end in `|| true`).
#
# Known findings are ignored only through .github/pip-audit-ignore.txt, one per line:
#   <vuln id> <expires YYYY-MM-DD> <reason>
# An entry past its date fails the check, so every ignore is looked at again. Any finding not on the
# list fails the check.
#
# Usage: pip_audit_gate.sh <requirements file> [ignore file]
set -euo pipefail
REQ="${1:?requirements file}"
LIST="${2:-.github/pip-audit-ignore.txt}"
TODAY="${TODAY_OVERRIDE:-$(date -u +%F)}"

args=()
expired=0
while read -r id expires reason; do
  case "${id}" in ''|'#'*) continue ;; esac
  if ! [[ "${expires}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || [ -z "${reason}" ]; then
    echo "::error::${LIST}: '${id}' needs an expiry date (YYYY-MM-DD) and a reason"; expired=1; continue
  fi
  if [[ "${expires}" < "${TODAY}" ]]; then
    echo "::error::${LIST}: ignore for ${id} expired on ${expires} (${reason}) — fix the package or renew the entry with a reason"
    expired=1; continue
  fi
  echo "ignoring ${id} until ${expires}: ${reason}"
  args+=(--ignore-vuln "${id}")
done < "${LIST}"
[ "${expired}" -eq 0 ] || exit 1

pip-audit -r "${REQ}" "${args[@]}"
