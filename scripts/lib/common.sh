#!/usr/bin/env bash

set -euo pipefail

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_ROOT="${AGENT_STACK_ROOT:-$(cd "$COMMON_DIR/../.." && pwd)}"

# shellcheck disable=SC1091
source "$STACK_ROOT/versions.env"

STACK_DATA="${AGENT_STACK_DATA:-$HOME/.local/share/agent-stack}"
STACK_RUNTIME="$STACK_DATA/runtime"
STACK_MODELS="$STACK_DATA/models"
STACK_LOGS="$STACK_DATA/logs"
STACK_BACKUPS="$STACK_DATA/backups"
STACK_DISABLED="$STACK_DATA/disabled-launch-agents"
STACK_SECRETS="$STACK_DATA/secrets"
STACK_STATE="$STACK_DATA/install-state.json"
STACK_TRANSACTIONS="$STACK_DATA/transactions"
STACK_ARTIFACTS="$STACK_DATA/artifacts"
LLAMA_DIR="$STACK_RUNTIME/llama.cpp"
LLAMA_SERVER="$LLAMA_DIR/build/bin/llama-server"
BONSAI_MODEL="$STACK_MODELS/$BONSAI_FILE"
BONSAI_API_KEY_FILE="$STACK_SECRETS/bonsai-api-key"
BONSAI_HERMES_KEY_ENV="HERMES_CUSTOM_BONSAI_API_KEY"
PI_AGENT_DIR="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_AGENT_DIR="$HERMES_HOME/hermes-agent"
HERMES_FEISHU_PATCH="$STACK_ROOT/patches/hermes-feishu-webhook.patch"
HERMES_DEPENDENCY_PATCH="$STACK_ROOT/patches/hermes-security-dependencies.patch"
LAUNCH_AGENT_LABEL="com.local.agent-stack.bonsai"
LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/$LAUNCH_AGENT_LABEL.plist"
LAUNCHD_DOMAIN="gui/$(id -u)"
BONSAI_BASE_URL="http://$BONSAI_HOST:$BONSAI_PORT"

log() {
  printf '[agent-stack] %s\n' "$*"
}

warn() {
  printf '[agent-stack] warning: %s\n' "$*" >&2
}

die() {
  printf '[agent-stack] error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

timestamp() {
  date -u '+%Y%m%dT%H%M%SZ'
}

sha256_file() {
  shasum -a 256 "$1" | awk '{print $1}'
}

file_size() {
  stat -f '%z' "$1" 2>/dev/null || stat -c '%s' "$1"
}

verify_file_sha256() {
  local path="$1"
  local expected="$2"
  [ -f "$path" ] || die "missing checksum-pinned file: $path"
  [ "$(sha256_file "$path")" = "$expected" ] || \
    die "checksum mismatch: $path"
}

runtime_matches_pin() {
  [ -x "$LLAMA_SERVER" ] || return 1
  if [ -d "$LLAMA_DIR/.git" ]; then
    [ "$(git -C "$LLAMA_DIR" rev-parse HEAD 2>/dev/null || true)" = "$LLAMA_COMMIT" ] && \
      [ -z "$(git -C "$LLAMA_DIR" status --porcelain --untracked-files=no 2>/dev/null)" ]
  elif [ -f "$LLAMA_DIR/runtime-manifest.json" ]; then
    python3 "$STACK_ROOT/scripts/runtime_artifact.py" verify \
      --runtime "$LLAMA_DIR" \
      --commit "$LLAMA_COMMIT" \
      --platform darwin-arm64 \
      --signature-policy "${AGENT_STACK_LLAMA_SIGNATURE_POLICY:-$LLAMA_PREBUILT_SIGNATURE_POLICY}" \
      >/dev/null 2>&1
  else
    return 1
  fi
}

git_patch_applied() {
  local repository="$1"
  local patch="$2"
  git -C "$repository" apply --reverse --check "$patch" >/dev/null 2>&1
}

git_patch_can_apply() {
  local repository="$1"
  local patch="$2"
  git -C "$repository" apply --check "$patch" >/dev/null 2>&1
}

apply_git_patch_once() {
  local repository="$1"
  local patch="$2"
  if git_patch_applied "$repository" "$patch"; then
    return 0
  fi
  git_patch_can_apply "$repository" "$patch" || \
    die "patch does not apply cleanly: $patch"
  git -C "$repository" apply "$patch"
}

remove_git_patch_once() {
  local repository="$1"
  local patch="$2"
  if git_patch_can_apply "$repository" "$patch"; then
    return 0
  fi
  git_patch_applied "$repository" "$patch" || \
    die "installed patch is incomplete or conflicted: $patch"
  git -C "$repository" apply --reverse "$patch"
}

verify_hermes_patch_files() {
  verify_file_sha256 "$HERMES_FEISHU_PATCH" "$HERMES_FEISHU_PATCH_SHA256"
  verify_file_sha256 \
    "$HERMES_DEPENDENCY_PATCH" "$HERMES_DEPENDENCY_PATCH_SHA256"
}

hermes_stack_patches_applied() {
  [ -d "$HERMES_AGENT_DIR/.git" ] && \
    git_patch_applied "$HERMES_AGENT_DIR" "$HERMES_FEISHU_PATCH" && \
    git_patch_applied "$HERMES_AGENT_DIR" "$HERMES_DEPENDENCY_PATCH"
}

hermes_source_changes_match() {
  local actual expected
  actual="$(git -C "$HERMES_AGENT_DIR" diff --name-only HEAD -- | LC_ALL=C sort)"
  expected="$(printf '%s\n' \
    plugins/platforms/feishu/adapter.py \
    pyproject.toml \
    tests/gateway/test_feishu.py \
    tests/gateway/test_setup_feishu.py \
    uv.lock \
    website/docs/user-guide/messaging/feishu.md \
    website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-guide/messaging/feishu.md \
    | LC_ALL=C sort)"
  [ "$actual" = "$expected" ]
}

prepare_hermes_source_for_installer() {
  [ -d "$HERMES_AGENT_DIR/.git" ] || return 0
  [ "$(git -C "$HERMES_AGENT_DIR" rev-parse HEAD 2>/dev/null || true)" = \
    "$HERMES_COMMIT" ] || return 0

  verify_hermes_patch_files
  # Reverse only stack-owned changes. Refuse to let the upstream installer's
  # autostash hide or merge any other tracked customization at our pinned HEAD.
  remove_git_patch_once "$HERMES_AGENT_DIR" "$HERMES_DEPENDENCY_PATCH"
  remove_git_patch_once "$HERMES_AGENT_DIR" "$HERMES_FEISHU_PATCH"
  git -C "$HERMES_AGENT_DIR" diff --quiet HEAD -- || \
    die "$HERMES_AGENT_DIR contains non-stack tracked changes"
}

apply_hermes_security_patches() {
  verify_hermes_patch_files
  [ -d "$HERMES_AGENT_DIR/.git" ] || die "missing Hermes checkout"
  [ "$(git -C "$HERMES_AGENT_DIR" rev-parse HEAD 2>/dev/null)" = \
    "$HERMES_COMMIT" ] || die "Hermes checkout is not at the pinned commit"
  git -C "$HERMES_AGENT_DIR" diff --quiet HEAD -- || \
    die "Hermes checkout is not clean before patching"

  apply_git_patch_once "$HERMES_AGENT_DIR" "$HERMES_FEISHU_PATCH"
  apply_git_patch_once "$HERMES_AGENT_DIR" "$HERMES_DEPENDENCY_PATCH"
  hermes_stack_patches_applied || die "Hermes security patches did not apply"
  hermes_source_changes_match || \
    die "Hermes checkout has unexpected changes after patching"
}

service_loaded() {
  launchctl print "$LAUNCHD_DOMAIN/$LAUNCH_AGENT_LABEL" >/dev/null 2>&1
}

wait_for_service_unloaded() {
  local max_attempts="${1:-30}"
  local attempt=0

  while [ "$attempt" -lt "$max_attempts" ]; do
    if ! service_loaded; then
      return 0
    fi
    sleep 1
    attempt=$((attempt + 1))
  done
  return 1
}

bootstrap_bonsai_service() {
  local max_attempts="${1:-6}"
  local retry_delay="${2:-2}"
  local attempt=1

  while [ "$attempt" -le "$max_attempts" ]; do
    if launchctl bootstrap "$LAUNCHD_DOMAIN" "$LAUNCH_AGENT_PLIST"; then
      return 0
    fi
    # launchctl can return an error after accepting a request. Treat a loaded
    # job as success before retrying, otherwise a second bootstrap would fail.
    if service_loaded; then
      return 0
    fi
    if [ "$attempt" -lt "$max_attempts" ]; then
      warn "launchd bootstrap attempt $attempt/$max_attempts failed; retrying in ${retry_delay}s"
      sleep "$retry_delay"
    fi
    attempt=$((attempt + 1))
  done
  return 1
}

server_ready() {
  [ -s "$BONSAI_API_KEY_FILE" ] || return 1
  curl --fail --silent --max-time 3 \
    -H "Authorization: Bearer $(bonsai_api_key)" \
    "$BONSAI_BASE_URL/v1/models" >/dev/null 2>&1
}

bonsai_api_key() {
  [ -s "$BONSAI_API_KEY_FILE" ] || die "missing Bonsai API key: $BONSAI_API_KEY_FILE"
  tr -d '\r\n' < "$BONSAI_API_KEY_FILE"
}

ensure_bonsai_api_key() {
  mkdir -p "$STACK_SECRETS"
  chmod 700 "$STACK_SECRETS"
  if [ ! -s "$BONSAI_API_KEY_FILE" ]; then
    local temporary
    temporary="$(mktemp "$STACK_SECRETS/.bonsai-api-key.XXXXXX")"
    python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$temporary"
    chmod 600 "$temporary"
    mv "$temporary" "$BONSAI_API_KEY_FILE"
  fi
  chmod 600 "$BONSAI_API_KEY_FILE"
}

wait_for_server() {
  local attempts="${1:-180}"
  local i=0
  while [ "$i" -lt "$attempts" ]; do
    if server_ready; then
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

find_hermes_metal_cli() {
  local link target candidate
  if [ -n "${HERMES_METAL_HOME:-}" ] &&
     [ -x "$HERMES_METAL_HOME/bin/hermes" ]; then
    printf '%s\n' "$HERMES_METAL_HOME/bin/hermes"
    return 0
  fi

  for link in "$HOME/.local/bin/hermes-metal" "$HOME/.local/bin/hermes"; do
    if [ -L "$link" ]; then
      target="$(readlink "$link")"
      case "$target" in
        /*) ;;
        *) target="$(dirname "$link")/$target" ;;
      esac
      case "$target" in
        */hermes-metal/*)
          if [ -x "$target" ]; then
            printf '%s\n' "$target"
            return 0
          fi
          ;;
      esac
    fi
  done

  for candidate in \
    "$HOME/Documents/hermes-metal/bin/hermes" \
    "$HOME"/Documents/*/hermes-metal/bin/hermes \
    "$HOME"/Documents/*/Documents/hermes-metal/bin/hermes; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

backup_item() {
  local source="$1"
  local destination_dir="$2"
  [ -e "$source" ] || [ -L "$source" ] || return 0
  mkdir -p "$destination_dir"
  cp -a "$source" "$destination_dir/"
}

ensure_stack_dirs() {
  mkdir -p \
    "$STACK_RUNTIME" \
    "$STACK_MODELS" \
    "$STACK_LOGS" \
    "$STACK_BACKUPS" \
    "$STACK_DISABLED" \
    "$STACK_SECRETS" \
    "$STACK_TRANSACTIONS" \
    "$STACK_ARTIFACTS" \
    "$HOME/.local/bin" \
    "$HOME/Library/LaunchAgents"
  chmod 700 "$STACK_SECRETS"
}

stack_profile() {
  if [ -n "${AGENT_STACK_PROFILE:-}" ]; then
    printf '%s\n' "$AGENT_STACK_PROFILE"
  elif [ -f "$STACK_STATE" ] && command -v python3 >/dev/null 2>&1; then
    python3 - "$STACK_STATE" <<'PYPROFILE'
import json
import sys
try:
    profile = json.load(open(sys.argv[1], encoding="utf-8")).get("profile", "local")
except (OSError, ValueError):
    profile = "local"
print(profile if profile in {"local", "hybrid", "cloud"} else "local")
PYPROFILE
  else
    printf 'local\n'
  fi
}

run_with_timeout() {
  local seconds="$1"
  shift
  python3 - "$seconds" "$@" <<'PYTIMEOUT'
import subprocess
import sys
try:
    result = subprocess.run(sys.argv[2:], timeout=float(sys.argv[1]))
except subprocess.TimeoutExpired:
    raise SystemExit(124)
raise SystemExit(result.returncode)
PYTIMEOUT
}

logical_cpus() {
  local count
  count="$(sysctl -n hw.logicalcpu 2>/dev/null || true)"
  if [ -z "$count" ]; then
    count="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '4')"
  fi
  printf '%s\n' "$count"
}
