#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"
output="${1:-$STACK_ARTIFACTS/llama-runtime-${LLAMA_COMMIT:0:12}-darwin-arm64.tar.gz}"
mkdir -p "$(dirname "$output")"
python3 "$SCRIPT_DIR/runtime_artifact.py" package \
  --source "$LLAMA_DIR" \
  --output "$output" \
  --commit "$LLAMA_COMMIT" \
  --platform darwin-arm64
