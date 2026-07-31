#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP="$(mktemp -d)"
trap 'rm -rf "$TEMP"' EXIT

# A read-only clean-machine plan reports all failures and does not create state.
set +e
HOME="$TEMP/clean home" \
AGENT_STACK_DATA="$TEMP/clean data" \
PATH="/usr/bin:/bin" \
  /usr/bin/python3 "$ROOT/scripts/onboard.py" setup \
    --plan --profile cloud --runtime-source source --json \
    > "$TEMP/preflight.jsonl" 2> "$TEMP/preflight.err"
status=$?
set -e
[ "$status" -eq 2 ]
[ ! -e "$TEMP/clean data" ]
/usr/bin/python3 - "$TEMP/preflight.jsonl" <<'PY'
import json
import sys
lines = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
report = lines[0]["preflight"]
failures = [item for item in report["checks"] if item["status"] == "fail"]
assert len(failures) >= 2, failures
PY

# Diagnostics accepts paths with spaces and produces a private archive.
mkdir -p "$TEMP/space home" "$TEMP/space data"
archive="$TEMP/diagnostics with spaces.tar.gz"
HOME="$TEMP/space home" AGENT_STACK_DATA="$TEMP/space data" \
  /usr/bin/python3 "$ROOT/scripts/onboard.py" diagnostics "$archive" >/dev/null
[ -f "$archive" ]
[ "$(stat -f '%Lp' "$archive")" = 600 ]
tar -tzf "$archive" | grep -q '^metadata.json$'

printf 'isolated onboarding scenarios passed\n'
