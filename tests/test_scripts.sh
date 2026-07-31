#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP="$(mktemp -d)"
trap 'rm -rf "$TEMP"' EXIT

# Pi bundles ripgrep. Use it when a development shell has not installed the
# Brewfile yet so repository validation does not depend on an undocumented
# global PATH entry.
if ! command -v rg >/dev/null 2>&1 && [ -x "$HOME/.pi/agent/bin/rg" ]; then
  PATH="$HOME/.pi/agent/bin:$PATH"
fi
command -v rg >/dev/null 2>&1 || {
  printf 'ripgrep is required; run ./install --install-deps\n' >&2
  exit 2
}

while IFS= read -r script; do
  if head -n 1 "$script" | rg -q '(/usr/bin/env bash|/bin/bash)'; then
    bash -n "$script"
  fi
done < <(find "$ROOT/bin" "$ROOT/scripts" "$ROOT/tests" -type f | sort; printf '%s\n' "$ROOT/install")

python3 -m py_compile \
  "$ROOT/scripts/configure.py" \
  "$ROOT/scripts/onboard.py" \
  "$ROOT/scripts/runtime_artifact.py" \
  "$ROOT/scripts/service_cutover.py" \
  "$ROOT/scripts/audit_security.py" \
  "$ROOT/tests/test_configure.py" \
  "$ROOT/tests/test_security_audit.py"

"$ROOT/bin/agent-stack" setup --help >/dev/null
"$ROOT/bin/agent-stack" preflight --help >/dev/null

# launchd can transiently return EIO while a recently unloaded job finishes
# tearing down. Verify bounded retry and eventual failure without touching the
# user's real launchd domain.
(
  # shellcheck disable=SC1091
  source "$ROOT/scripts/lib/common.sh"
  attempts=0
  launchctl() {
    case "$1" in
      bootstrap)
        attempts=$((attempts + 1))
        [ "$attempts" -ge 3 ]
        ;;
      print) return 1 ;;
      *) return 1 ;;
    esac
  }
  sleep() { :; }
  bootstrap_bonsai_service 4 0
  [ "$attempts" -eq 3 ]
)
(
  # shellcheck disable=SC1091
  source "$ROOT/scripts/lib/common.sh"
  attempts=0
  launchctl() {
    case "$1" in
      bootstrap) attempts=$((attempts + 1)); return 1 ;;
      print) return 1 ;;
      *) return 1 ;;
    esac
  }
  sleep() { :; }
  if bootstrap_bonsai_service 3 0; then
    printf 'launchd retry unexpectedly succeeded\n' >&2
    exit 1
  fi
  [ "$attempts" -eq 3 ]
)
(
  # shellcheck disable=SC1091
  source "$ROOT/scripts/lib/common.sh"
  checks=0
  service_loaded() {
    checks=$((checks + 1))
    [ "$checks" -lt 3 ]
  }
  sleep() { :; }
  wait_for_service_unloaded 4
  [ "$checks" -eq 3 ]
)
(
  # shellcheck disable=SC1091
  source "$ROOT/scripts/lib/common.sh"
  service_loaded() { return 0; }
  sleep() { :; }
  if wait_for_service_unloaded 2; then
    printf 'launchd unload wait unexpectedly succeeded\n' >&2
    exit 1
  fi
)

# Discover optional Hermes Metal installs without hard-coding one user layout.
(
  LEGACY_HOME="$TEMP/legacy-home"
  LEGACY_CLI="$LEGACY_HOME/Documents/archive/Documents/hermes-metal/bin/hermes"
  mkdir -p "$(dirname "$LEGACY_CLI")" "$LEGACY_HOME/.local/bin"
  printf '#!/bin/sh\nexit 0\n' > "$LEGACY_CLI"
  chmod +x "$LEGACY_CLI"
  HOME="$LEGACY_HOME"
  export HOME
  # shellcheck disable=SC1091
  source "$ROOT/scripts/lib/common.sh"
  [ "$(find_hermes_metal_cli)" = "$LEGACY_CLI" ]
)
(
  EMPTY_HOME="$TEMP/no-legacy-home"
  mkdir -p "$EMPTY_HOME"
  HOME="$EMPTY_HOME"
  export HOME
  # shellcheck disable=SC1091
  source "$ROOT/scripts/lib/common.sh"
  ! find_hermes_metal_cli
)

# Exercise patch detection, idempotent application, and reversal without
# depending on an installed Hermes checkout.
(
  # shellcheck disable=SC1091
  source "$ROOT/scripts/lib/common.sh"
  repository="$TEMP/patch-repository"
  patch="$TEMP/test.patch"
  mkdir -p "$repository"
  git -C "$repository" init -q
  git -C "$repository" config user.name test
  git -C "$repository" config user.email test@example.invalid
  printf 'before\n' > "$repository/example.txt"
  git -C "$repository" add example.txt
  git -C "$repository" commit -qm initial
  printf 'after\n' > "$repository/example.txt"
  git -C "$repository" diff -- example.txt > "$patch"
  git -C "$repository" checkout -q -- example.txt

  git_patch_can_apply "$repository" "$patch"
  apply_git_patch_once "$repository" "$patch"
  apply_git_patch_once "$repository" "$patch"
  git_patch_applied "$repository" "$patch"
  [ "$(cat "$repository/example.txt")" = after ]
  remove_git_patch_once "$repository" "$patch"
  remove_git_patch_once "$repository" "$patch"
  git -C "$repository" diff --quiet HEAD --
  [ "$(cat "$repository/example.txt")" = before ]

  verify_hermes_patch_files
)

python3 "$ROOT/scripts/configure.py" launch-agent \
  --template "$ROOT/config/bonsai.launchagent.plist.in" \
  --output "$TEMP/bonsai.plist" \
  --label com.test.agent-stack \
  --server "$TEMP/llama-server" \
  --model "$TEMP/model.gguf" \
  --model-id bonsai-27b \
  --host 127.0.0.1 \
  --port 8080 \
  --context 65536 \
  --api-key-file "$TEMP/bonsai-api-key" \
  --working-dir "$TEMP" \
  --log-dir "$TEMP/logs"
plutil -lint "$TEMP/bonsai.plist" >/dev/null
plutil -p "$TEMP/bonsai.plist" | rg -q '"--no-ui"'
plutil -p "$TEMP/bonsai.plist" | rg -q '"--api-key-file"'

if rg -n '0\.0\.0\.0' "$ROOT/bin" "$ROOT/config" "$ROOT/scripts"; then
  printf 'network wildcard found in runtime configuration\n' >&2
  exit 1
fi

# A clean machine does not have Pi yet. The installer must reach install_pi
# without treating the binary it owns as a prerequisite.
NO_PI_PATH="$TEMP/no-pi-path"
mkdir -p "$NO_PI_PATH"
for command in \
  awk cat cmake curl df dirname git id jq ninja node npm plutil python3 rg rsync \
  shasum system_profiler tar; do
  ln -s "$(command -v "$command")" "$NO_PI_PATH/$command"
done
cat > "$NO_PI_PATH/sysctl" <<'EOF'
#!/bin/sh
case "$*" in
  *hw.memsize*) echo 38654705664 ;;
  *hw.logicalcpu*) echo 8 ;;
  *) exit 1 ;;
esac
EOF
cat > "$NO_PI_PATH/uname" <<'EOF'
#!/bin/sh
case "${1:-}" in
  -s) echo Darwin ;;
  -m) echo arm64 ;;
  *) echo Darwin ;;
esac
EOF
chmod +x "$NO_PI_PATH/sysctl" "$NO_PI_PATH/uname"
if PATH="$NO_PI_PATH" command -v pi >/dev/null 2>&1; then
  printf 'test PATH unexpectedly contains Pi\n' >&2
  exit 1
fi
PATH="$NO_PI_PATH" /bin/bash "$ROOT/scripts/bootstrap.sh" --dry-run >/dev/null

# The installed command is a symlink. It must still resolve the repository's
# scripts rather than looking under the symlink directory.
mkdir -p "$TEMP/symlink-bin"
ln -s "$ROOT/bin/agent-stack" "$TEMP/symlink-bin/agent-stack"
"$TEMP/symlink-bin/agent-stack" versions | rg -q "Hermes base"

SOURCE_HOME="$TEMP/source-home"
mkdir -p "$SOURCE_HOME/.hermes/skills/demo" "$SOURCE_HOME/.pi/agent"
printf 'model:\n  provider: custom\n' > "$SOURCE_HOME/.hermes/config.yaml"
printf 'TOKEN=secret\n' > "$SOURCE_HOME/.hermes/.env"
printf 'demo\n' > "$SOURCE_HOME/.hermes/skills/demo/SKILL.md"
printf '{"theme":"dark"}\n' > "$SOURCE_HOME/.pi/agent/settings.json"
printf '{"secret":true}\n' > "$SOURCE_HOME/.pi/agent/auth.json"

HOME="$SOURCE_HOME" AGENT_STACK_DATA="$TEMP/source-data" \
  "$ROOT/bin/agent-stack" export "$TEMP/portable.tar.gz" >/dev/null
tar -tzf "$TEMP/portable.tar.gz" | rg -q '^manifest\.json$'
tar -tzf "$TEMP/portable.tar.gz" | rg -q '^home/\.hermes/config\.yaml$'
if tar -tzf "$TEMP/portable.tar.gz" | rg -q '(auth\.json|/\.env)$'; then
  printf 'normal export contains secrets\n' >&2
  exit 1
fi

HOME="$SOURCE_HOME" AGENT_STACK_DATA="$TEMP/source-data" \
  "$ROOT/bin/agent-stack" export "$TEMP/with-secrets.tar.gz" --include-secrets >/dev/null 2>&1
tar -tzf "$TEMP/with-secrets.tar.gz" | rg -q '^home/\.pi/agent/auth\.json$'
tar -tzf "$TEMP/with-secrets.tar.gz" | rg -q '^home/\.hermes/\.env$'

DEST_HOME="$TEMP/destination-home"
mkdir -p "$DEST_HOME"
mkdir -p "$TEMP/destination-data/secrets"
printf 'destination-local-key\n' > "$TEMP/destination-data/secrets/bonsai-api-key"
HOME="$DEST_HOME" AGENT_STACK_DATA="$TEMP/destination-data" \
  "$ROOT/bin/agent-stack" restore "$TEMP/portable.tar.gz" >/dev/null
test -f "$DEST_HOME/.hermes/config.yaml"
test -f "$DEST_HOME/.pi/agent/models.json"
test ! -f "$DEST_HOME/.pi/agent/auth.json"
rg -q '^HERMES_CUSTOM_BONSAI_API_KEY=destination-local-key$' "$DEST_HOME/.hermes/.env"

# Public-release metadata and supply-chain policy must remain explicit.
for required in LICENSE THIRD_PARTY_NOTICES.md CONTRIBUTING.md SECURITY.md CHANGELOG.md; do
  test -s "$ROOT/$required"
done
jq -e '.license == "MIT" and .private == true' "$ROOT/pi-package/package.json" >/dev/null
rg -q 'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1' \
  "$ROOT/.github/workflows/onboarding.yml" "$ROOT/.github/workflows/runtime-release.yml"
rg -q 'actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a' \
  "$ROOT/.github/workflows/runtime-release.yml"
rg -q 'runs-on: macos-15' "$ROOT/.github/workflows/onboarding.yml"
rg -q 'fetch-depth: 0' "$ROOT/.github/workflows/onboarding.yml"
if rg -q 'PI_SETUP_COMMIT|MCP_SDK_VERSION|HONO_NODE_SERVER_VERSION' \
  "$ROOT/versions.env" "$ROOT/scripts/bootstrap.sh"; then
  printf 'removed Pi extension pins remain active\n' >&2
  exit 1
fi

printf 'static and portability tests passed\n'
