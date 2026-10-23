#!/usr/bin/env bash
# AA-899: OpenAPI breaking-change gate (workflow "API contract", job "OpenAPI breaking changes").
# Byte-identical in AA-CIS-App and AA-Booking; each workflow passes its own public-path rule.
#
# Compares two committed OpenAPI documents with oasdiff (pinned by the workflow):
#   - every change      -> the job summary (oasdiff changelog, markdown), never fails;
#   - PUBLIC paths      -> breaking changes (oasdiff level ERR, after .github/oasdiff/severity-levels.txt) FAIL the
#                          job, unless ALLOW_BREAKING=true (PR label "api-breaking" or a "type!:" PR title);
#   - internal paths    -> breaking changes are listed as warnings, never fail (same-repo clients only).
# Any other oasdiff exit code (bad spec, bad flag) fails the job whatever the label says.
#
# Environment:
#   BASE_SPEC        base document (the PR merge commit's first parent); missing file = first export, pass
#   HEAD_SPEC        document of the PR (kept current by the "Unit Tests" stale check)
#   PUBLIC_PATHS     RE2 regex of public paths, or
#   INTERNAL_PATHS   RE2 regex of internal paths (everything else is public); exactly one of the two
#   SEVERITY_LEVELS  oasdiff --severity-levels file
#   ALLOW_BREAKING   "true" to accept public breaking changes (default false)
#   OASDIFF          oasdiff binary (default: oasdiff on PATH)
#   GITHUB_STEP_SUMMARY  optional; the report is appended there
set -uo pipefail

OASDIFF="${OASDIFF:-oasdiff}"
ALLOW_BREAKING="${ALLOW_BREAKING:-false}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"
PUBLIC_PATHS="${PUBLIC_PATHS:-}"
INTERNAL_PATHS="${INTERNAL_PATHS:-}"

if [[ -n "$PUBLIC_PATHS" && -n "$INTERNAL_PATHS" ]] || [[ -z "$PUBLIC_PATHS" && -z "$INTERNAL_PATHS" ]]; then
  echo "::error::set exactly one of PUBLIC_PATHS and INTERNAL_PATHS"; exit 2
fi
if [[ -n "$PUBLIC_PATHS" ]]; then
  public_filter=(--match-path "$PUBLIC_PATHS"); internal_filter=(--unmatch-path "$PUBLIC_PATHS")
else
  public_filter=(--unmatch-path "$INTERNAL_PATHS"); internal_filter=(--match-path "$INTERNAL_PATHS")
fi
common=(--allow-external-refs=false --severity-levels "$SEVERITY_LEVELS")
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

if [[ ! -f "$HEAD_SPEC" ]]; then
  echo "::error::$HEAD_SPEC is missing. Run the export script (docs/api/README.md)."; exit 2
fi
if [[ ! -s "$BASE_SPEC" ]]; then
  echo "::notice::No OpenAPI document on the base branch yet (first export): nothing to compare."
  echo "No OpenAPI document on the base branch yet: nothing to compare." >> "$SUMMARY"
  exit 0
fi

{
  echo "## OpenAPI changes"
  "$OASDIFF" changelog "$BASE_SPEC" "$HEAD_SPEC" --allow-external-refs=false --format markdown || true
} >> "$SUMMARY"

"$OASDIFF" breaking "$BASE_SPEC" "$HEAD_SPEC" "${common[@]}" "${internal_filter[@]}" --format singleline \
  > "$work/internal.txt" 2>&1
rc_internal=$?
if [[ $rc_internal -ne 0 ]]; then cat "$work/internal.txt"; echo "::error::oasdiff failed (exit $rc_internal)"; exit "$rc_internal"; fi
while IFS= read -r line; do
  [[ "$line" == error* || "$line" == warning* ]] && echo "::warning title=Internal API change::$line"
done < "$work/internal.txt"

"$OASDIFF" breaking "$BASE_SPEC" "$HEAD_SPEC" "${common[@]}" "${public_filter[@]}" --fail-on ERR --format singleline \
  > "$work/public.txt" 2>&1
rc=$?
cat "$work/public.txt"
case $rc in
  0)
    echo "No breaking change on a public path." | tee -a "$SUMMARY"
    exit 0 ;;
  1)
    {
      echo "## Breaking changes on public paths"
      echo '```'; cat "$work/public.txt"; echo '```'
    } >> "$SUMMARY"
    if [[ "$ALLOW_BREAKING" == "true" ]]; then
      while IFS= read -r line; do
        [[ "$line" == error* ]] && echo "::warning title=Accepted breaking API change::$line"
      done < "$work/public.txt"
      echo "Accepted: the PR is marked breaking (label api-breaking or a 'type!:' title)." | tee -a "$SUMMARY"
      exit 0
    fi
    while IFS= read -r line; do
      [[ "$line" == error* ]] && echo "::error title=Breaking API change::$line"
    done < "$work/public.txt"
    echo "::error::Breaking change on a public API path. Make it additive (docs/api/README.md), or mark the PR" \
      "breaking: label 'api-breaking' or a 'type!:' title, with the client plan in the PR body."
    exit 1 ;;
  *)
    echo "::error::oasdiff failed (exit $rc)"; exit "$rc" ;;
esac
