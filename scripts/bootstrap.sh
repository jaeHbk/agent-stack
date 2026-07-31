#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"

DRY_RUN=false
SKIP_MODEL=false
PHASE=""
CHECK_PHASE=""
PROFILE="local"
RUNTIME_SOURCE="auto"

usage() {
  cat <<'EOF'
Usage: bootstrap.sh [--dry-run] [--skip-model]
       bootstrap.sh --phase PHASE [--profile PROFILE] [--runtime-source SOURCE]
       bootstrap.sh --check-phase PHASE [--profile PROFILE]

  --dry-run     Validate prerequisites and print the rollout without changes
  --skip-model  Configure clients and runtimes without downloading the model
  --phase       Run one resumable setup phase (internal adapter)
  --check-phase Check whether one phase is already satisfied
  --profile     local, hybrid, or cloud
  --runtime-source  auto, prebuilt, or source
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=true ;;
    --skip-model) SKIP_MODEL=true ;;
    --phase)
      [ "$#" -ge 2 ] || die "--phase requires a value"
      PHASE="$2"
      shift
      ;;
    --check-phase)
      [ "$#" -ge 2 ] || die "--check-phase requires a value"
      CHECK_PHASE="$2"
      shift
      ;;
    --profile)
      [ "$#" -ge 2 ] || die "--profile requires a value"
      PROFILE="$2"
      shift
      ;;
    --runtime-source)
      [ "$#" -ge 2 ] || die "--runtime-source requires a value"
      RUNTIME_SOURCE="$2"
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
  shift
done

case "$PROFILE" in
  local|hybrid|cloud) ;;
  *) die "unknown profile: $PROFILE" ;;
esac
case "$RUNTIME_SOURCE" in
  auto|prebuilt|source) ;;
  *) die "unknown runtime source: $RUNTIME_SOURCE" ;;
esac

preflight() {
  [ "$(uname -s)" = "Darwin" ] || die "this profile currently supports macOS"
  [ "$(uname -m)" = "arm64" ] || die "this profile requires Apple Silicon"
  # Pi is intentionally not a prerequisite: install_pi installs or upgrades it.
  # Everything else is used before or during that installation.
  for command in curl git jq npm node python3 cmake ninja plutil rg shasum rsync tar; do
    require_command "$command"
  done

  local node_major
  node_major="$(node -p 'Number(process.versions.node.split(".")[0])')"
  [ "$node_major" -ge 20 ] || die "Node.js 20 or newer is required"

  local memory_bytes memory_gb available_kb
  memory_bytes="$(sysctl -n hw.memsize 2>/dev/null || true)"
  if [ -n "$memory_bytes" ]; then
    [ "$memory_bytes" -ge 25769803776 ] || die "at least 24 GiB RAM is required"
  else
    memory_gb="$(system_profiler SPHardwareDataType 2>/dev/null | awk '/Memory:/ {print $2; exit}')"
    if [ -n "$memory_gb" ]; then
      [ "$memory_gb" -ge 24 ] || die "at least 24 GiB RAM is required"
    else
      warn "could not determine memory size"
    fi
  fi
  available_kb="$(df -Pk "$HOME" | awk 'NR == 2 {print $4}')"
  if [ ! -f "$BONSAI_MODEL" ] && [ "$available_kb" -lt 15728640 ]; then
    die "at least 15 GiB free disk is required for the first install"
  fi
}

print_plan() {
  cat <<EOF
Pinned rollout:
  Pi                 $PI_VERSION
  Pi dependency fix  brace-expansion $BRACE_EXPANSION_VERSION
  Hermes             $HERMES_COMMIT + checksum-pinned security overlay
  Prism llama.cpp    $LLAMA_COMMIT
  Pi workflows       repository-owned package
  Bonsai             $BONSAI_REVISION
  Endpoint            $BONSAI_BASE_URL/v1
  API authentication  generated token in $BONSAI_API_KEY_FILE
  Context             $BONSAI_CONTEXT tokens, one slot, q4_0 KV
  Runtime data        $STACK_DATA
  Hermes data         $HERMES_HOME
  Pi data             $PI_AGENT_DIR
EOF
}

create_backup() {
  local backup_dir="$STACK_BACKUPS/$(timestamp)"
  mkdir -p "$backup_dir/pi" "$backup_dir/hermes" "$backup_dir/bin" "$backup_dir/launchagents"
  chmod 700 "$backup_dir"

  backup_item "$PI_AGENT_DIR/settings.json" "$backup_dir/pi"
  backup_item "$PI_AGENT_DIR/models.json" "$backup_dir/pi"
  backup_item "$HOME/.local/bin/hermes" "$backup_dir/bin"
  backup_item "$HOME/.local/bin/hermes-metal" "$backup_dir/bin"
  for plist in "$HOME"/Library/LaunchAgents/com.hermes.metal.*.plist "$LAUNCH_AGENT_PLIST"; do
    [ -e "$plist" ] && backup_item "$plist" "$backup_dir/launchagents"
  done

  if [ -d "$HERMES_HOME" ]; then
    rsync -a \
      --exclude 'hermes-agent/' \
      --exclude 'venv/' \
      --exclude 'node/' \
      --exclude 'bin/' \
      --exclude 'logs/' \
      "$HERMES_HOME/" "$backup_dir/hermes/"
  fi

  printf '%s\n' "$backup_dir" > "$STACK_DATA/latest-backup"
  chmod 600 "$STACK_DATA/latest-backup"
  log "backed up current configuration to $backup_dir"
}


install_pi() {
  local current_version
  current_version="$(pi --version 2>/dev/null || true)"
  if [ "$current_version" != "$PI_VERSION" ]; then
    log "installing Pi $PI_VERSION"
    npm install -g "@earendil-works/pi-coding-agent@$PI_VERSION"
  else
    log "Pi $PI_VERSION already installed"
  fi

  log "configuring repository-owned Pi workflows"
  python3 "$STACK_ROOT/scripts/configure.py" pi \
    --home "$HOME" \
    --stack-root "$STACK_ROOT" \
    --profile "$PROFILE" \
    --api-key-file "$BONSAI_API_KEY_FILE"

  remediate_pi_dependencies
}

replace_brace_expansion() {
  local target="$1"
  local extracted="$2"
  local current parent replacement previous

  [ -f "$target/package.json" ] || die "missing Pi dependency: $target"
  current="$(jq -r '.version // empty' "$target/package.json")"
  if [ "$current" = "$BRACE_EXPANSION_VERSION" ]; then
    return 0
  fi
  [ "$current" = "5.0.7" ] || \
    die "unexpected brace-expansion version at $target: $current"

  parent="$(dirname "$target")"
  replacement="$(mktemp -d "$parent/.brace-expansion.XXXXXX")"
  previous="$parent/.brace-expansion-vulnerable-$$"
  cp -a "$extracted/package/." "$replacement/"
  [ "$(jq -r '.version // empty' "$replacement/package.json")" = "$BRACE_EXPANSION_VERSION" ] || \
    die "fixed brace-expansion archive has the wrong version"
  mv "$target" "$previous"
  mv "$replacement" "$target"
  rm -rf "$previous"
}

remediate_pi_dependencies() {
  local archive extracted global_pi url
  archive="$(mktemp)"
  extracted="$(mktemp -d)"
  trap 'rm -f "$archive"; rm -rf "$extracted"' RETURN
  url="https://registry.npmjs.org/brace-expansion/-/brace-expansion-$BRACE_EXPANSION_VERSION.tgz"

  curl --fail --location --silent --show-error "$url" --output "$archive"
  [ "$(sha256_file "$archive")" = "$BRACE_EXPANSION_SHA256" ] || \
    die "brace-expansion checksum mismatch"
  tar -xzf "$archive" -C "$extracted"

  global_pi="$(npm root -g)/@earendil-works/pi-coding-agent"
  replace_brace_expansion \
    "$global_pi/node_modules/brace-expansion" "$extracted"

  rm -f "$archive"
  rm -rf "$extracted"
  trap - RETURN
  log "Pi brace-expansion dependency pinned at $BRACE_EXPANSION_VERSION"
}

preserve_hermes_metal_cli() {
  local target
  target="$(find_hermes_metal_cli || true)"
  if [ -n "$target" ]; then
    ln -sfn "$target" "$HOME/.local/bin/hermes-metal"
    log "preserved legacy CLI as hermes-metal"
  fi
}

install_hermes() {
  local installer
  prepare_hermes_source_for_installer
  installer="$(mktemp)"
  trap 'rm -f "$installer"' RETURN
  curl --fail --location --silent --show-error \
    "https://raw.githubusercontent.com/NousResearch/hermes-agent/$HERMES_COMMIT/scripts/install.sh" \
    --output "$installer"
  [ "$(sha256_file "$installer")" = "$HERMES_INSTALLER_SHA256" ] || \
    die "Hermes installer checksum mismatch"
  chmod +x "$installer"
  log "installing pinned Nous Hermes"
  HERMES_HOME="$HERMES_HOME" bash "$installer" \
    --commit "$HERMES_COMMIT" \
    --skip-setup \
    --skip-browser
  rm -f "$installer"
  trap - RETURN

  apply_hermes_security_patches
  [ -x "$HERMES_HOME/bin/uv" ] || die "Hermes uv runtime is missing"
  [ -x "$HERMES_AGENT_DIR/venv/bin/python" ] || die "Hermes Python runtime is missing"
  log "synchronizing checksum-pinned Hermes security dependencies"
  (
    cd "$HERMES_AGENT_DIR"
    UV_PYTHON="$HERMES_AGENT_DIR/venv/bin/python" \
      UV_PROJECT_ENVIRONMENT="$HERMES_AGENT_DIR/venv" \
      "$HERMES_HOME/bin/uv" sync --extra all --locked
    "$HERMES_HOME/bin/uv" pip check \
      --python "$HERMES_AGENT_DIR/venv/bin/python"
  )

  if [ "$PROFILE" = cloud ]; then
    hermes config set model.default "$BEDROCK_MODEL"
    hermes config set model.provider bedrock
    hermes config unset model.base_url >/dev/null 2>&1 || true
    hermes config unset model.api_key >/dev/null 2>&1 || true
    hermes config set bedrock.region "${AWS_REGION:-${AWS_DEFAULT_REGION:-$BEDROCK_REGION}}"
    if [ -n "${AWS_PROFILE:-}" ]; then
      hermes config set bedrock.profile "$AWS_PROFILE"
    fi
  else
    hermes config set model.default "$BONSAI_MODEL_ID"
    hermes config set model.provider custom:bonsai
    hermes config set model.base_url "$BONSAI_BASE_URL/v1"
    hermes config set model.context_length "$BONSAI_CONTEXT"
    hermes config set model.max_tokens "$BONSAI_MAX_TOKENS"
    hermes config unset model.api_key >/dev/null 2>&1 || true
    hermes config set providers.bonsai.name "Bonsai Local"
    hermes config set providers.bonsai.base_url "$BONSAI_BASE_URL/v1"
    hermes config set providers.bonsai.key_env "$BONSAI_HERMES_KEY_ENV"
    hermes config set providers.bonsai.transport chat_completions
    hermes config set providers.bonsai.default_model "$BONSAI_MODEL_ID"
    hermes config set providers.bonsai.max_output_tokens "$BONSAI_MAX_TOKENS"
  fi

  # Keep the local coordinator focused. Pi owns coding, while Hermes retains
  # research, planning, memory, delegation, scheduling, and Pi invocation.
  hermes tools disable --platform cli \
    browser code_execution vision image_gen tts computer_use >/dev/null
  hermes tools enable --platform cli \
    web terminal file skills todo memory session_search clarify delegation cronjob \
    >/dev/null

  if [ "$PROFILE" != cloud ]; then
    python3 "$STACK_ROOT/scripts/configure.py" dotenv \
      --path "$HERMES_HOME/.env" \
      --key "$BONSAI_HERMES_KEY_ENV" \
      --value-file "$BONSAI_API_KEY_FILE"
  fi

  local skill_target="$HERMES_HOME/skills/pi-coder"
  if [ -e "$skill_target" ] && [ ! -L "$skill_target" ]; then
    mv "$skill_target" "$skill_target.pre-agent-stack-$(timestamp)"
  fi
  ln -sfn "$STACK_ROOT/hermes-skills/pi-coder" "$skill_target"
}

install_llama_prebuilt() {
  local url checksum policy archive
  url="${AGENT_STACK_LLAMA_PREBUILT_URL:-$LLAMA_PREBUILT_URL}"
  checksum="${AGENT_STACK_LLAMA_PREBUILT_SHA256:-$LLAMA_PREBUILT_SHA256}"
  policy="${AGENT_STACK_LLAMA_SIGNATURE_POLICY:-$LLAMA_PREBUILT_SIGNATURE_POLICY}"
  [ -n "$url" ] && [ -n "$checksum" ] || \
    die "verified prebuilt runtime requires URL and SHA-256"
  archive="$STACK_ARTIFACTS/llama-runtime-$LLAMA_COMMIT.tar.gz"
  if [ ! -f "$archive" ] || [ "$(sha256_file "$archive")" != "$checksum" ]; then
    log "downloading checksum-pinned llama.cpp runtime"
    curl --fail --location --show-error --retry 5 --retry-delay 3 \
      --output "$archive.partial" "$url"
    [ "$(sha256_file "$archive.partial")" = "$checksum" ] || \
      die "prebuilt runtime archive checksum mismatch"
    mv "$archive.partial" "$archive"
  fi
  python3 "$STACK_ROOT/scripts/runtime_artifact.py" extract \
    --archive "$archive" \
    --destination "$LLAMA_DIR" \
    --sha256 "$checksum" \
    --commit "$LLAMA_COMMIT" \
    --platform darwin-arm64 \
    --signature-policy "$policy"
}

install_llama_source() {
  command -v cmake >/dev/null 2>&1 || die "source runtime build requires cmake"
  command -v ninja >/dev/null 2>&1 || die "source runtime build requires ninja"
  xcrun -f metal >/dev/null 2>&1 || \
    die "source runtime build requires the full Xcode Metal compiler"
  if [ ! -d "$LLAMA_DIR/.git" ]; then
    log "cloning pinned Prism llama.cpp"
    git clone --filter=blob:none https://github.com/PrismML-Eng/llama.cpp.git "$LLAMA_DIR"
  fi

  [ -z "$(git -C "$LLAMA_DIR" status --porcelain --untracked-files=no)" ] || \
    die "$LLAMA_DIR has tracked local changes"
  git -C "$LLAMA_DIR" fetch --depth 1 origin "$LLAMA_COMMIT"
  git -C "$LLAMA_DIR" checkout --detach "$LLAMA_COMMIT"

  log "building llama-server with Metal"
  # This stack exposes only the OpenAI-compatible API. Removing the optional
  # UI avoids shipping its browser bundle and its development dependency tree.
  rm -rf "$LLAMA_DIR/tools/ui/node_modules" "$LLAMA_DIR/build/tools/ui/dist"
  cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DGGML_METAL=ON \
    -DLLAMA_BUILD_UI=OFF \
    -DLLAMA_USE_PREBUILT_UI=OFF
  cmake --build "$LLAMA_DIR/build" \
    --target llama-server llama-cli \
    -j "$(logical_cpus)"
  [ -x "$LLAMA_SERVER" ] || die "llama-server build did not produce $LLAMA_SERVER"
}

install_llama_runtime() {
  if runtime_matches_pin; then
    log "pinned llama.cpp runtime is already installed"
    return 0
  fi
  case "$RUNTIME_SOURCE" in
    prebuilt) install_llama_prebuilt ;;
    source) install_llama_source ;;
    auto)
      if [ -n "${AGENT_STACK_LLAMA_PREBUILT_URL:-$LLAMA_PREBUILT_URL}" ] && \
         [ -n "${AGENT_STACK_LLAMA_PREBUILT_SHA256:-$LLAMA_PREBUILT_SHA256}" ]; then
        install_llama_prebuilt
      else
        install_llama_source
      fi
      ;;
  esac
}

download_model() {
  if [ -f "$BONSAI_MODEL" ]; then
    if [ "$(file_size "$BONSAI_MODEL")" = "$BONSAI_SIZE" ] && \
       [ "$(sha256_file "$BONSAI_MODEL")" = "$BONSAI_SHA256" ]; then
      log "Bonsai model already present and verified"
      return 0
    fi
    mv "$BONSAI_MODEL" "$BONSAI_MODEL.invalid-$(timestamp)"
    warn "preserved an invalid existing model file for inspection"
  fi

  local partial="$BONSAI_MODEL.partial"
  local url="https://huggingface.co/$BONSAI_REPOSITORY/resolve/$BONSAI_REVISION/$BONSAI_FILE?download=true"
  log "downloading $BONSAI_FILE (7.16 GB, resumable)"
  curl --fail --location --show-error \
    --retry 5 --retry-delay 3 --continue-at - \
    --output "$partial" "$url"
  [ "$(file_size "$partial")" = "$BONSAI_SIZE" ] || die "model size mismatch"
  [ "$(sha256_file "$partial")" = "$BONSAI_SHA256" ] || die "model checksum mismatch"
  mv "$partial" "$BONSAI_MODEL"
}

install_links() {
  local name
  for name in agent-stack pi-local pi-sonnet pi-opus; do
    ln -sfn "$STACK_ROOT/bin/$name" "$HOME/.local/bin/$name"
  done
  preserve_hermes_metal_cli
}

stage_service() {
  local staged="$STACK_DATA/bonsai.plist.staged"
  python3 "$STACK_ROOT/scripts/configure.py" launch-agent \
    --template "$STACK_ROOT/config/bonsai.launchagent.plist.in" \
    --output "$staged" \
    --label "$LAUNCH_AGENT_LABEL" \
    --server "$LLAMA_SERVER" \
    --model "$BONSAI_MODEL" \
    --model-id "$BONSAI_MODEL_ID" \
    --host "$BONSAI_HOST" \
    --port "$BONSAI_PORT" \
    --context "$BONSAI_CONTEXT" \
    --api-key-file "$BONSAI_API_KEY_FILE" \
    --working-dir "$STACK_DATA" \
    --log-dir "$STACK_LOGS"
  plutil -lint "$staged" >/dev/null
  printf '%s\n' "$staged"
}

transactional_cutover() {
  [ -x "$LLAMA_SERVER" ] || die "cannot activate Bonsai without $LLAMA_SERVER"
  [ -f "$BONSAI_MODEL" ] || die "cannot activate Bonsai without $BONSAI_MODEL"
  local staged
  staged="$(stage_service)"
  python3 "$STACK_ROOT/scripts/service_cutover.py" activate \
    --domain "$LAUNCHD_DOMAIN" \
    --bonsai-label "$LAUNCH_AGENT_LABEL" \
    --bonsai-plist "$LAUNCH_AGENT_PLIST" \
    --launch-agents "$HOME/Library/LaunchAgents" \
    --disabled-dir "$STACK_DISABLED/hermes-metal" \
    --transaction-dir "$STACK_TRANSACTIONS/cutover" \
    --health-url "$BONSAI_BASE_URL/v1/models" \
    --api-key-file "$BONSAI_API_KEY_FILE" \
    --staged-plist "$staged" \
    --health-timeout 240
  log "Bonsai activated; Hermes Metal services remain preserved and disabled"
}

deactivate_local_inference() {
  python3 "$STACK_ROOT/scripts/service_cutover.py" deactivate \
    --domain "$LAUNCHD_DOMAIN" \
    --bonsai-label "$LAUNCH_AGENT_LABEL" \
    --bonsai-plist "$LAUNCH_AGENT_PLIST" \
    --launch-agents "$HOME/Library/LaunchAgents" \
    --disabled-dir "$STACK_DISABLED/hermes-metal" \
    --transaction-dir "$STACK_TRANSACTIONS/cutover" \
    --health-url "$BONSAI_BASE_URL/v1/models" \
    --api-key-file "$BONSAI_API_KEY_FILE"
  log "local inference is stopped for the cloud profile; runtime data is preserved"
}

check_pi_phase() {
  [ "$(pi --version 2>/dev/null || true)" = "$PI_VERSION" ] || return 1
  python3 "$STACK_ROOT/scripts/configure.py" pi \
    --home "$HOME" \
    --stack-root "$STACK_ROOT" \
    --profile "$PROFILE" \
    --api-key-file "$BONSAI_API_KEY_FILE" \
    --check >/dev/null 2>&1 || return 1
  local global_brace pi_packages
  pi_packages="$(pi list 2>/dev/null || true)"
  printf '%s\n' "$pi_packages" | grep -Fq "$STACK_ROOT/pi-package" || return 1
  ! printf '%s\n' "$pi_packages" | grep -Fq 'davis7dotsh/my-pi-setup' || return 1
  global_brace="$(npm root -g)/@earendil-works/pi-coding-agent/node_modules/brace-expansion/package.json"
  [ "$(jq -r '.version // empty' "$global_brace" 2>/dev/null || true)" = "$BRACE_EXPANSION_VERSION" ] || return 1
}

check_hermes_phase() {
  command -v hermes >/dev/null 2>&1 || return 1
  [ -d "$HERMES_AGENT_DIR/.git" ] || return 1
  [ "$(git -C "$HERMES_AGENT_DIR" rev-parse HEAD 2>/dev/null || true)" = "$HERMES_COMMIT" ] || return 1
  (verify_hermes_patch_files) >/dev/null 2>&1 || return 1
  hermes_stack_patches_applied || return 1
  hermes_source_changes_match || return 1
  [ -L "$HERMES_HOME/skills/pi-coder" ] || return 1
  [ "$(readlink "$HERMES_HOME/skills/pi-coder")" = "$STACK_ROOT/hermes-skills/pi-coder" ] || return 1
  if [ "$PROFILE" = cloud ]; then
    [ "$(hermes config get model.provider 2>/dev/null | tail -n 1)" = bedrock ] || return 1
    [ "$(hermes config get model.default 2>/dev/null | tail -n 1)" = "$BEDROCK_MODEL" ] || return 1
  else
    [ "$(hermes config get model.provider 2>/dev/null | tail -n 1)" = custom:bonsai ] || return 1
    [ "$(hermes config get providers.bonsai.key_env 2>/dev/null | tail -n 1)" = "$BONSAI_HERMES_KEY_ENV" ] || return 1
    [ -s "$HERMES_HOME/.env" ] || return 1
  fi
}

check_model_phase() {
  [ -f "$BONSAI_MODEL" ] || return 1
  [ "$(file_size "$BONSAI_MODEL")" = "$BONSAI_SIZE" ] || return 1
  if [ -f "$STACK_STATE" ] && python3 - "$STACK_STATE" "$BONSAI_REVISION" <<'PYMODEL'
import json
import sys
try:
    state = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
revision = state.get("model_revision") or state.get("versions", {}).get("model_revision")
raise SystemExit(0 if revision == sys.argv[2] else 1)
PYMODEL
  then
    return 0
  fi
  [ "$(sha256_file "$BONSAI_MODEL")" = "$BONSAI_SHA256" ]
}

check_cutover_phase() {
  if [ "$PROFILE" = cloud ]; then
    ! service_loaded || return 1
    local cloud_label cloud_plist
    for cloud_label in com.hermes.metal.engine com.hermes.metal.embed com.hermes.metal.watcher; do
      ! launchctl print "$LAUNCHD_DOMAIN/$cloud_label" >/dev/null 2>&1 || return 1
    done
    for cloud_plist in "$HOME"/Library/LaunchAgents/com.hermes.metal.*.plist; do
      [ ! -e "$cloud_plist" ] || return 1
    done
    return 0
  fi
  service_loaded || return 1
  if ! server_ready; then
    lsof -nP -iTCP:"$BONSAI_PORT" -sTCP:LISTEN 2>/dev/null | \
      grep -Fq "127.0.0.1:$BONSAI_PORT" || return 1
  fi
  plutil -lint "$LAUNCH_AGENT_PLIST" >/dev/null 2>&1 || return 1
  local label plist
  for label in com.hermes.metal.engine com.hermes.metal.embed com.hermes.metal.watcher; do
    ! launchctl print "$LAUNCHD_DOMAIN/$label" >/dev/null 2>&1 || return 1
  done
  for plist in "$HOME"/Library/LaunchAgents/com.hermes.metal.*.plist; do
    [ ! -e "$plist" ] || return 1
  done
}

check_links_phase() {
  local name legacy_target
  for name in agent-stack pi-local pi-sonnet pi-opus; do
    [ -L "$HOME/.local/bin/$name" ] || return 1
    [ "$(readlink "$HOME/.local/bin/$name")" = "$STACK_ROOT/bin/$name" ] || return 1
  done
  legacy_target="$(find_hermes_metal_cli || true)"
  if [ -n "$legacy_target" ]; then
    [ -x "$HOME/.local/bin/hermes-metal" ] || return 1
    [ "$(readlink "$HOME/.local/bin/hermes-metal")" = "$legacy_target" ] || return 1
  fi
}

check_phase() {
  case "$1" in
    pi) check_pi_phase ;;
    hermes) check_hermes_phase ;;
    runtime)
      [ "$PROFILE" = cloud ] || runtime_matches_pin
      ;;
    model)
      [ "$PROFILE" = cloud ] || check_model_phase
      ;;
    cutover) check_cutover_phase ;;
    links) check_links_phase ;;
    *) return 1 ;;
  esac
}

run_phase() {
  ensure_stack_dirs
  case "$1" in
    backup) create_backup ;;
    pi)
      [ "$PROFILE" = cloud ] || ensure_bonsai_api_key
      install_pi
      ;;
    hermes)
      [ "$PROFILE" = cloud ] || ensure_bonsai_api_key
      preserve_hermes_metal_cli
      install_hermes
      ;;
    runtime)
      [ "$PROFILE" = cloud ] || install_llama_runtime
      ;;
    model)
      [ "$PROFILE" = cloud ] || download_model
      ;;
    cutover)
      if [ "$PROFILE" = cloud ]; then
        deactivate_local_inference
      else
        ensure_bonsai_api_key
        transactional_cutover
      fi
      ;;
    links) install_links ;;
    verify|audit) : ;;
    *) die "unknown setup phase: $1" ;;
  esac
}

write_state() {
  local state="$STACK_DATA/install-state.json"
  python3 - "$state" "$PROFILE" "$STACK_ROOT" "$PI_VERSION" \
    "$HERMES_COMMIT" "$LLAMA_COMMIT" "$BONSAI_REVISION" \
    "$ONBOARDING_STATE_SCHEMA" <<'PYSTATE'
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
try:
    state = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    state = {}
state.update({
    "schema_version": int(sys.argv[8]),
    "installed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "profile": sys.argv[2],
    "stack_root": sys.argv[3],
    "versions": {
        "pi": sys.argv[4],
        "hermes_commit": sys.argv[5],
        "llama_commit": sys.argv[6],
        "model_revision": sys.argv[7],
    },
})
state.setdefault("phases", {})
path.parent.mkdir(parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(state, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PYSTATE
}

if [ -n "$CHECK_PHASE" ]; then
  check_phase "$CHECK_PHASE"
  exit $?
fi

if [ -n "$PHASE" ]; then
  run_phase "$PHASE"
  exit 0
fi

preflight
print_plan
if [ "$DRY_RUN" = true ]; then
  log "dry run complete; no changes made"
  exit 0
fi

ensure_stack_dirs
ensure_bonsai_api_key
create_backup
install_pi
preserve_hermes_metal_cli
install_hermes
install_llama_runtime

if [ "$SKIP_MODEL" = false ]; then
  download_model
  transactional_cutover
else
  warn "model download and service activation skipped"
fi

install_links
write_state
log "bootstrap complete"
"$STACK_ROOT/bin/agent-stack" status
