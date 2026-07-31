#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"

require_command hermes
require_command git
require_command python3

[ -d "$HERMES_AGENT_DIR/.git" ] || die "missing Hermes checkout: $HERMES_AGENT_DIR"
[ "$(git -C "$HERMES_AGENT_DIR" rev-parse HEAD 2>/dev/null)" = "$HERMES_COMMIT" ] || \
  die "Hermes source is not at the pinned commit"
verify_hermes_patch_files
hermes_stack_patches_applied || die "Hermes security patches are not fully applied"
hermes_source_changes_match || die "Hermes checkout contains unexpected tracked changes"
git -C "$HERMES_AGENT_DIR" merge-base --is-ancestor \
  "$HERMES_COMPRESSION_FIX_COMMIT" HEAD || \
  die "pinned Hermes source does not contain the cited compression fix"

report="$(mktemp)"
trap 'rm -f "$report"' EXIT
set +e
run_with_timeout 1200 hermes security audit --json --fail-on low > "$report"
audit_status=$?
set -e
case "$audit_status" in
  0|1) ;;
  *) die "Hermes security audit could not complete (exit $audit_status)" ;;
esac

python3 "$STACK_ROOT/scripts/audit_security.py" \
  --input "$report" \
  --allow-source-remediated
