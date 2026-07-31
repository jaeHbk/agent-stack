#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"

RESTORE_METAL=false

usage() {
  cat <<'EOF'
Usage: uninstall.sh [--restore-metal]

Removes stack-owned service wiring and Pi configuration. Runtime sources,
model weights, Nous Hermes data, Pi auth, and all Hermes Metal data are kept.

  --restore-metal  Restore and load the preserved Hermes Metal LaunchAgents,
                   and make `hermes` point to Hermes Metal again.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --restore-metal) RESTORE_METAL=true ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
  shift
done

if service_loaded; then
  launchctl bootout "$LAUNCHD_DOMAIN/$LAUNCH_AGENT_LABEL"
  wait_for_service_unloaded 30 || die "Bonsai LaunchAgent did not finish unloading"
fi
[ -f "$LAUNCH_AGENT_PLIST" ] && mv "$LAUNCH_AGENT_PLIST" "$STACK_DISABLED/$(basename "$LAUNCH_AGENT_PLIST").$(timestamp)"

for name in agent-stack pi-local pi-sonnet pi-opus; do
  path="$HOME/.local/bin/$name"
  if [ -L "$path" ] && [ "$(readlink "$path")" = "$STACK_ROOT/bin/$name" ]; then
    rm "$path"
  fi
done

skill="$HERMES_HOME/skills/pi-coder"
if [ -L "$skill" ] && [ "$(readlink "$skill")" = "$STACK_ROOT/hermes-skills/pi-coder" ]; then
  rm "$skill"
fi

if [ -f "$PI_AGENT_DIR/settings.json" ] || [ -f "$PI_AGENT_DIR/models.json" ]; then
  python3 "$STACK_ROOT/scripts/configure.py" pi \
    --home "$HOME" \
    --stack-root "$STACK_ROOT" \
    --remove
fi

if [ "$RESTORE_METAL" = true ]; then
  metal_dir="$STACK_DISABLED/hermes-metal"
  for plist in "$metal_dir"/com.hermes.metal.*.plist; do
    [ -f "$plist" ] || continue
    destination="$HOME/Library/LaunchAgents/$(basename "$plist")"
    mv "$plist" "$destination"
    launchctl bootstrap "$LAUNCHD_DOMAIN" "$destination" >/dev/null 2>&1 || true
  done
  if [ -x "$HOME/Documents/hermes-metal/bin/hermes" ]; then
    ln -sfn "$HOME/Documents/hermes-metal/bin/hermes" "$HOME/.local/bin/hermes"
    ln -sfn "$HOME/Documents/hermes-metal/bin/hermes" "$HOME/.local/bin/hermes-metal"
  fi
  log "Hermes Metal LaunchAgents and CLI restored"
fi

log "stack wiring removed; runtime and user data preserved in $STACK_DATA"
