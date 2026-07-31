#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"

require_command git
[ -x "$HERMES_HOME/bin/uv" ] || die "missing Hermes uv runtime"
[ -d "$HERMES_AGENT_DIR/.git" ] || die "missing Hermes checkout"
verify_hermes_patch_files

hermes_python="$HERMES_AGENT_DIR/venv/bin/python"
[ -x "$hermes_python" ] || hermes_python="$HERMES_AGENT_DIR/.venv/bin/python"
[ -x "$hermes_python" ] || die "missing Hermes Python runtime"

base="$(mktemp -d "${TMPDIR:-/tmp}/agent-stack-feishu.XXXXXX")"
trap 'rm -rf "$base"' EXIT

run_mode() {
  local mode="$1"
  shift
  local checkout="$base/$mode"

  log "creating disposable Hermes checkout for $mode"
  git clone --quiet --no-hardlinks "$HERMES_AGENT_DIR" "$checkout"
  git -C "$checkout" checkout --quiet --detach "$HERMES_COMMIT"
  git -C "$checkout" apply "$HERMES_FEISHU_PATCH"
  git -C "$checkout" apply "$HERMES_DEPENDENCY_PATCH"
  [ "$(git -C "$checkout" diff --name-only | wc -l | tr -d ' ')" = 7 ] || \
    die "unexpected patched Hermes file set in $mode"

  (
    cd "$checkout"
    "$HERMES_HOME/bin/uv" sync \
      --python "$hermes_python" \
      --extra dev \
      --extra messaging \
      "$@" \
      --locked
    .venv/bin/python -c 'import aiohttp'
    if [ "$mode" = sdk-denied ]; then
      .venv/bin/python -c \
        'import importlib.util; assert importlib.util.find_spec("lark_oapi") is None'
    else
      .venv/bin/python -c 'import lark_oapi'
    fi
    HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh \
      tests/gateway/test_feishu.py \
      tests/gateway/test_setup_feishu.py
  )
}

run_mode sdk-denied
run_mode sdk-present --extra feishu
log "Feishu regression matrix passed with and without lark-oapi"
