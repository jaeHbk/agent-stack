#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"

QUICK=false
PROFILE="$(stack_profile)"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --quick) QUICK=true ;;
    --profile)
      [ "$#" -ge 2 ] || die "--profile requires a value"
      PROFILE="$2"
      shift
      ;;
    -h|--help)
      printf 'Usage: doctor.sh [--quick] [--profile local|hybrid|cloud]\n'
      exit 0
      ;;
    *) die "unknown doctor option: $1" ;;
  esac
  shift
done
case "$PROFILE" in
  local|hybrid|cloud) ;;
  *) die "unknown stack profile: $PROFILE" ;;
esac
FAILURES=0
WARNINGS=0

pass() {
  printf 'PASS  %s\n' "$*"
}

fail() {
  printf 'FAIL  %s\n' "$*" >&2
  FAILURES=$((FAILURES + 1))
}

warning() {
  printf 'WARN  %s\n' "$*" >&2
  WARNINGS=$((WARNINGS + 1))
}

check_command() {
  if command -v "$1" >/dev/null 2>&1; then
    pass "$1 is installed"
  else
    fail "$1 is missing"
  fi
}

aws_credentials_ready() {
  if [ -n "${AWS_ACCESS_KEY_ID:-}" ] && [ -n "${AWS_SECRET_ACCESS_KEY:-}" ]; then
    return 0
  fi
  if [ -n "${AWS_WEB_IDENTITY_TOKEN_FILE:-}" ] || \
     [ -n "${AWS_CONTAINER_CREDENTIALS_RELATIVE_URI:-}" ] || \
     [ -n "${AWS_CONTAINER_CREDENTIALS_FULL_URI:-}" ]; then
    return 0
  fi
  if [ -n "${AWS_PROFILE:-}" ] && \
     { [ -f "$HOME/.aws/credentials" ] || [ -f "$HOME/.aws/config" ]; }; then
    return 0
  fi
  [ -f "$HOME/.aws/credentials" ]
}

pi_workflows_reconciled() {
  local output
  output="$(run_with_timeout 30 pi list 2>/dev/null || true)"
  printf '%s\n' "$output" | grep -Fq "$STACK_ROOT/pi-package" &&
    ! printf '%s\n' "$output" | grep -Fq 'davis7dotsh/my-pi-setup'
}

doctor_cloud() {
  local command dependency hermes_python provider model listener label pi_output hermes_output hermes_usage
  for command in curl git jq npm pi hermes plutil python3 shasum; do
    check_command "$command"
  done
  if [ "$(run_with_timeout 15 pi --version 2>/dev/null || true)" = "$PI_VERSION" ]; then
    pass "Pi version is pinned at $PI_VERSION"
  else
    fail "Pi version differs from $PI_VERSION"
  fi
  if python3 "$STACK_ROOT/scripts/configure.py" pi \
    --home "$HOME" \
    --stack-root "$STACK_ROOT" \
    --profile cloud \
    --api-key-file "$BONSAI_API_KEY_FILE" \
    --check; then
    pass "Pi cloud profile is reconciled without a local provider"
  else
    fail "Pi cloud configuration drifted"
  fi
  if pi_workflows_reconciled; then
    pass "Repository-owned Pi workflows are enabled without the legacy package"
  else
    fail "Pi workflow package configuration drifted"
  fi
  dependency="$(npm root -g)/@earendil-works/pi-coding-agent/node_modules/brace-expansion/package.json"
  if [ "$(jq -r '.version // empty' "$dependency" 2>/dev/null || true)" = "$BRACE_EXPANSION_VERSION" ]; then
    pass "Pi global dependency remediation is installed"
  else
    fail "Pi global brace-expansion remediation drifted"
  fi
  if [ -d "$HERMES_AGENT_DIR/.git" ] && \
     [ "$(git -C "$HERMES_AGENT_DIR" rev-parse HEAD 2>/dev/null)" = "$HERMES_COMMIT" ]; then
    pass "Hermes source matches pinned commit"
  else
    fail "Hermes source does not match pinned commit"
  fi
  if (verify_hermes_patch_files) && hermes_stack_patches_applied && hermes_source_changes_match; then
    pass "Hermes security patches are complete and exact"
  else
    fail "Hermes security patch state drifted"
  fi
  hermes_python="$HERMES_AGENT_DIR/venv/bin/python"
  if [ -x "$HERMES_HOME/bin/uv" ] && [ -x "$hermes_python" ] && \
     run_with_timeout 90 "$HERMES_HOME/bin/uv" pip check --python "$hermes_python" >/dev/null 2>&1; then
    pass "Hermes Python dependency graph is compatible"
  else
    fail "Hermes Python dependency graph is inconsistent"
  fi
  provider="$(run_with_timeout 30 hermes config get model.provider 2>/dev/null | tail -n 1 || true)"
  model="$(run_with_timeout 30 hermes config get model.default 2>/dev/null | tail -n 1 || true)"
  if [ "$provider" = bedrock ] && [ "$model" = "$BEDROCK_MODEL" ]; then
    pass "Hermes uses pinned Bedrock model $BEDROCK_MODEL"
  else
    fail "Hermes Bedrock routing drifted (provider=$provider model=$model)"
  fi
  if aws_credentials_ready; then
    pass "AWS credential-chain readiness is present"
  else
    fail "AWS credential-chain readiness is absent"
  fi
  if [ -L "$HERMES_HOME/skills/pi-coder" ] && \
     [ "$(readlink "$HERMES_HOME/skills/pi-coder")" = "$STACK_ROOT/hermes-skills/pi-coder" ]; then
    pass "Hermes Pi delegation skill is installed"
  else
    fail "Hermes Pi delegation skill is missing"
  fi
  if service_loaded; then
    fail "Bonsai LaunchAgent is loaded in the cloud profile"
  else
    pass "Bonsai LaunchAgent is stopped in the cloud profile"
  fi
  listener="$(lsof -nP -iTCP:"$BONSAI_PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -z "$listener" ]; then
    pass "cloud profile has no local inference listener"
  else
    fail "cloud profile still has a listener on port $BONSAI_PORT"
  fi
  for label in com.hermes.metal.engine com.hermes.metal.embed com.hermes.metal.watcher; do
    if launchctl print "$LAUNCHD_DOMAIN/$label" >/dev/null 2>&1; then
      fail "legacy LaunchAgent $label is still loaded"
    else
      pass "legacy LaunchAgent $label is disabled"
    fi
  done
  if [ "$QUICK" = false ] && aws_credentials_ready; then
    pi_output="$(run_with_timeout 300 "$STACK_ROOT/bin/pi-sonnet" --no-session -p 'Reply with only PI_CLOUD_OK.' 2>/dev/null || true)"
    if printf '%s' "$pi_output" | grep -q 'PI_CLOUD_OK'; then
      pass "Pi completes a real Bedrock turn"
    else
      fail "Pi Bedrock inference turn failed"
    fi
    hermes_usage="$(mktemp)"
    hermes_output="$(run_with_timeout 300 hermes -z 'Reply with exactly HERMES_CLOUD_OK and nothing else.' -t todo --usage-file "$hermes_usage" 2>/dev/null || true)"
    if printf '%s\n' "$hermes_output" | grep -q '^HERMES_CLOUD_OK$' && \
       jq -e '.provider == "bedrock" and .api_calls >= 1' "$hermes_usage" >/dev/null 2>&1; then
      pass "Hermes completes a real Bedrock turn"
    else
      fail "Hermes Bedrock inference turn failed"
    fi
    rm -f "$hermes_usage"
  fi
  if run_with_timeout 30 hermes status >/dev/null 2>&1; then
    pass "Hermes status command succeeds"
  else
    fail "Hermes status command fails"
  fi
  printf '\nDoctor summary: %d failure(s), %d warning(s)\n' "$FAILURES" "$WARNINGS"
  [ "$FAILURES" -eq 0 ]
}

if [ "$PROFILE" = cloud ]; then
  doctor_cloud
  exit $?
fi

for command in curl git jq npm pi hermes plutil shasum; do
  check_command "$command"
done

if [ "$(run_with_timeout 15 pi --version 2>/dev/null || true)" = "$PI_VERSION" ]; then
  pass "Pi version is pinned at $PI_VERSION"
else
  fail "Pi version differs from $PI_VERSION"
fi

if python3 "$STACK_ROOT/scripts/configure.py" pi \
  --home "$HOME" \
  --stack-root "$STACK_ROOT" \
  --profile "$PROFILE" \
  --api-key-file "$BONSAI_API_KEY_FILE" \
  --check; then
  pass "Pi settings and local model configuration are reconciled"
else
  fail "Pi configuration drifted"
fi

key_mode="$(stat -f '%Lp' "$BONSAI_API_KEY_FILE" 2>/dev/null || stat -c '%a' "$BONSAI_API_KEY_FILE" 2>/dev/null || true)"
if [ -s "$BONSAI_API_KEY_FILE" ] && [ "$key_mode" = "600" ]; then
  pass "Bonsai API key exists with mode 0600"
else
  fail "Bonsai API key is missing, empty, or not mode 0600"
fi

if run_with_timeout 30 pi --list-models bonsai 2>/dev/null | rg -q 'bonsai-27b'; then
  pass "Pi discovers bonsai/bonsai-27b"
else
  fail "Pi cannot discover bonsai/bonsai-27b"
fi

if pi_workflows_reconciled; then
  pass "Repository-owned Pi workflows are enabled without the legacy package"
else
  fail "Pi workflow package configuration drifted"
fi

global_brace="$(run_with_timeout 15 npm root -g)/@earendil-works/pi-coding-agent/node_modules/brace-expansion/package.json"
if [ -f "$global_brace" ] &&
   [ "$(jq -r '.version // empty' "$global_brace")" = "$BRACE_EXPANSION_VERSION" ]; then
  pass "Pi global dependency remediation is installed"
else
  fail "Pi global brace-expansion remediation drifted"
fi

if [ -x "$HERMES_HOME/hermes-agent/venv/bin/python" ] || \
   [ -x "$HERMES_HOME/hermes-agent/.venv/bin/python" ]; then
  pass "Nous Hermes managed runtime exists"
else
  warning "Hermes venv location was not recognized"
fi

if [ -d "$HERMES_AGENT_DIR/.git" ] && \
   [ "$(git -C "$HERMES_AGENT_DIR" rev-parse HEAD 2>/dev/null)" = "$HERMES_COMMIT" ]; then
  pass "Hermes source matches pinned commit"
else
  fail "Hermes source does not match pinned commit"
fi

if (verify_hermes_patch_files); then
  pass "Hermes security patch files match pinned checksums"
else
  fail "Hermes security patch file checksum verification failed"
fi

if hermes_stack_patches_applied && hermes_source_changes_match; then
  pass "Hermes source has exactly the two stack security patches"
else
  fail "Hermes security patches are missing, partial, or mixed with other changes"
fi

if git -C "$HERMES_AGENT_DIR" merge-base --is-ancestor \
  "$HERMES_COMPRESSION_FIX_COMMIT" HEAD 2>/dev/null; then
  pass "Hermes source contains the cited context-compression hardening"
else
  fail "Hermes source does not prove context-compression remediation"
fi

hermes_python="$HERMES_AGENT_DIR/venv/bin/python"
if [ -x "$hermes_python" ] && "$hermes_python" - \
  "$HERMES_HTTPLIB2_VERSION" \
  "$HERMES_MCP_VERSION" \
  "$HERMES_PILLOW_VERSION" \
  "$HERMES_PYASN1_VERSION" \
  "$HERMES_PYDANTIC_SETTINGS_VERSION" \
  "$HERMES_PYGMENTS_VERSION" <<'PY' >/dev/null 2>&1
from importlib.metadata import version
import sys
expected = dict(zip(
    ("httplib2", "mcp", "Pillow", "pyasn1", "pydantic-settings", "Pygments"),
    sys.argv[1:],
))
raise SystemExit(0 if all(version(name) == wanted for name, wanted in expected.items()) else 1)
PY
then
  pass "Hermes advisory-bearing dependencies match patched lock versions"
else
  fail "Hermes security dependency versions drifted"
fi

if [ -x "$HERMES_HOME/bin/uv" ] && [ -x "$hermes_python" ] && \
   run_with_timeout 90 "$HERMES_HOME/bin/uv" pip check --python "$hermes_python" >/dev/null 2>&1; then
  pass "Hermes Python dependency graph is compatible"
else
  fail "Hermes Python dependency graph is inconsistent"
fi

if [ "$(run_with_timeout 30 hermes config get model.provider 2>/dev/null | tail -n 1)" = "custom:bonsai" ]; then
  pass "Hermes uses the named local Bonsai provider"
else
  fail "Hermes provider is not custom:bonsai"
fi

if run_with_timeout 30 hermes config get model.base_url 2>/dev/null | rg -q "127\.0\.0\.1:$BONSAI_PORT/v1"; then
  pass "Hermes points to the localhost Bonsai endpoint"
else
  fail "Hermes base URL is not the Bonsai endpoint"
fi

if [ "$(run_with_timeout 30 hermes config get providers.bonsai.key_env 2>/dev/null | tail -n 1)" = "$BONSAI_HERMES_KEY_ENV" ]; then
  pass "Hermes resolves Bonsai authentication from its private environment"
else
  fail "Hermes Bonsai key environment mapping is missing"
fi

hermes_toolsets="$(run_with_timeout 30 hermes config get platform_toolsets.cli 2>/dev/null || true)"
required_toolsets="web terminal file skills todo memory session_search clarify delegation cronjob"
disabled_toolsets="browser code_execution vision image_gen tts computer_use"
toolset_drift=false
for toolset in $required_toolsets; do
  printf '%s\n' "$hermes_toolsets" | grep -qx -- "- $toolset" || toolset_drift=true
done
for toolset in $disabled_toolsets; do
  if printf '%s\n' "$hermes_toolsets" | grep -qx -- "- $toolset"; then
    toolset_drift=true
  fi
done
if [ "$toolset_drift" = false ]; then
  pass "Hermes CLI toolsets match the focused coordinator profile"
else
  fail "Hermes CLI toolsets drifted from the coordinator profile"
fi

hermes_local_key="$(awk -F= -v key="$BONSAI_HERMES_KEY_ENV" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$HERMES_HOME/.env" 2>/dev/null || true)"
if [ -s "$BONSAI_API_KEY_FILE" ] && [ "$hermes_local_key" = "$(bonsai_api_key)" ]; then
  pass "Hermes and llama.cpp share the generated local API key"
else
  fail "Hermes local API key is missing or out of sync"
fi

if [ -L "$HERMES_HOME/skills/pi-coder" ] && \
   [ "$(readlink "$HERMES_HOME/skills/pi-coder")" = "$STACK_ROOT/hermes-skills/pi-coder" ]; then
  pass "Hermes Pi delegation skill is installed"
else
  fail "Hermes Pi delegation skill is missing"
fi

if runtime_matches_pin; then
  pass "Prism llama-server matches a verified source or prebuilt runtime"
else
  fail "Prism llama-server is missing or does not match the pinned commit"
fi

if [ -f "$BONSAI_MODEL" ] && [ "$(file_size "$BONSAI_MODEL")" = "$BONSAI_SIZE" ]; then
  pass "Bonsai model size is correct"
else
  fail "Bonsai model is missing or has the wrong size"
fi

if [ "$QUICK" = false ] && [ -f "$BONSAI_MODEL" ]; then
  if [ "$(sha256_file "$BONSAI_MODEL")" = "$BONSAI_SHA256" ]; then
    pass "Bonsai model SHA-256 is correct"
  else
    fail "Bonsai model SHA-256 is incorrect"
  fi
fi

if plutil -lint "$LAUNCH_AGENT_PLIST" >/dev/null 2>&1; then
  pass "Bonsai LaunchAgent plist is valid"
else
  fail "Bonsai LaunchAgent plist is missing or invalid"
fi

if plutil -p "$LAUNCH_AGENT_PLIST" 2>/dev/null | rg -q '"--no-ui"'; then
  pass "llama.cpp web UI is disabled"
else
  fail "llama.cpp web UI is not disabled"
fi

if plutil -p "$LAUNCH_AGENT_PLIST" 2>/dev/null | rg -q '"--api-key-file"'; then
  pass "Bonsai API authentication is configured"
else
  fail "Bonsai API authentication argument is missing"
fi

if service_loaded; then
  pass "Bonsai LaunchAgent is loaded"
else
  fail "Bonsai LaunchAgent is not loaded"
fi

if server_ready; then
  pass "Bonsai OpenAI-compatible API is ready"
else
  fail "Bonsai API is unavailable"
fi

if server_ready; then
  model_id="$(curl --silent --max-time 5 \
    -H "Authorization: Bearer $(bonsai_api_key)" \
    "$BONSAI_BASE_URL/v1/models" | jq -r '.data[0].id // empty')"
  if [ "$model_id" = "$BONSAI_MODEL_ID" ]; then
    pass "API advertises stable model ID $BONSAI_MODEL_ID"
  else
    fail "API model ID is '$model_id', expected '$BONSAI_MODEL_ID'"
  fi

  context="$(curl --silent --max-time 5 \
    -H "Authorization: Bearer $(bonsai_api_key)" \
    "$BONSAI_BASE_URL/props" | jq -r '.default_generation_settings.n_ctx // .n_ctx // empty')"
  if [ "$context" = "$BONSAI_CONTEXT" ]; then
    pass "API context is $BONSAI_CONTEXT tokens"
  else
    fail "API context is '$context', expected '$BONSAI_CONTEXT'"
  fi


  ui_status="$(curl --silent --output /dev/null --max-time 3 --write-out '%{http_code}' \
    -H "Authorization: Bearer $(bonsai_api_key)" \
    "$BONSAI_BASE_URL/" || true)"
  if [ "$ui_status" != "200" ]; then
    pass "Bonsai does not serve the optional web UI"
  else
    fail "Bonsai unexpectedly serves the optional web UI"
  fi

  unauthenticated_payload="$(jq -n \
    --arg model "$BONSAI_MODEL_ID" \
    '{model:$model,messages:[{role:"user",content:"test"}],max_tokens:1}')"
  unauthenticated_status="$(curl --silent --output /dev/null --max-time 3 \
    --write-out '%{http_code}' -H 'Content-Type: application/json' \
    -d "$unauthenticated_payload" \
    "$BONSAI_BASE_URL/v1/chat/completions" || true)"
  if [ "$unauthenticated_status" = "401" ]; then
    pass "Bonsai rejects unauthenticated API requests"
  else
    fail "Bonsai unauthenticated request returned HTTP $unauthenticated_status"
  fi

fi

listener="$(lsof -nP -iTCP:"$BONSAI_PORT" -sTCP:LISTEN 2>/dev/null || true)"
if printf '%s\n' "$listener" | rg -q "127\.0\.0\.1:$BONSAI_PORT"; then
  pass "Inference is bound to loopback only"
else
  fail "no loopback-only listener found on port $BONSAI_PORT"
fi

for label in com.hermes.metal.engine com.hermes.metal.embed com.hermes.metal.watcher; do
  if launchctl print "$LAUNCHD_DOMAIN/$label" >/dev/null 2>&1; then
    fail "legacy LaunchAgent $label is still loaded"
  else
    pass "legacy LaunchAgent $label is disabled"
  fi
done

legacy_cli="$(find_hermes_metal_cli || true)"
if [ -n "$legacy_cli" ] && [ -x "$HOME/.local/bin/hermes-metal" ]; then
  pass "legacy Hermes Metal CLI remains available as hermes-metal"
elif [ -n "$legacy_cli" ]; then
  fail "detected Hermes Metal CLI is not linked as hermes-metal"
elif [ -L "$HOME/.local/bin/hermes-metal" ]; then
  warning "stale hermes-metal compatibility link has no executable target"
else
  pass "no legacy Hermes Metal CLI was detected"
fi

if [ "$PROFILE" = hybrid ]; then
  if aws_credentials_ready; then
    pass "Bedrock escalation credential-chain readiness is present"
  else
    warning "hybrid profile is healthy locally, but Bedrock escalation is not authenticated"
  fi
fi

if [ "$QUICK" = false ] && server_ready; then
  completion_payload="$(jq -n \
    --arg model "$BONSAI_MODEL_ID" \
    '{model:$model,messages:[{role:"user",content:"Reply with exactly STACK_OK and nothing else."}],temperature:0,max_tokens:256}')"
  completion="$(curl --fail --silent --show-error --max-time 300 \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $(bonsai_api_key)" \
    -d "$completion_payload" \
    "$BONSAI_BASE_URL/v1/chat/completions" 2>/dev/null || true)"
  if printf '%s' "$completion" | jq -e '.choices[0].message.content | strings | length > 0' >/dev/null 2>&1; then
    pass "direct local completion succeeds"
  else
    fail "direct local completion failed"
  fi

  tool_payload="$(jq -n \
    --arg model "$BONSAI_MODEL_ID" \
    '{model:$model,messages:[{role:"user",content:"Call the stack_health function now."}],tools:[{type:"function",function:{name:"stack_health",description:"Check stack health",parameters:{type:"object",properties:{},additionalProperties:false}}}],tool_choice:{type:"function",function:{name:"stack_health"}},temperature:0,max_tokens:512}')"
  tool_response="$(curl --fail --silent --show-error --max-time 300 \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $(bonsai_api_key)" \
    -d "$tool_payload" \
    "$BONSAI_BASE_URL/v1/chat/completions" 2>/dev/null || true)"
  if printf '%s' "$tool_response" | jq -e '.choices[0].message.tool_calls[0].function.name == "stack_health"' >/dev/null 2>&1; then
    pass "llama.cpp Jinja tool calling succeeds"
  else
    fail "forced tool call was not returned"
  fi

  pi_output="$(run_with_timeout 300 "$STACK_ROOT/bin/pi-local" --no-session -p 'Reply with only PI_STACK_OK.' 2>/dev/null || true)"
  if printf '%s' "$pi_output" | rg -q 'PI_STACK_OK'; then
    pass "Pi completes a real turn through Bonsai"
  else
    fail "Pi local inference turn failed"
  fi

  hermes_usage="$(mktemp)"
  hermes_output="$(run_with_timeout 300 hermes -z 'Reply with exactly HERMES_STACK_OK and nothing else.' \
    -t todo --usage-file "$hermes_usage" 2>/dev/null || true)"
  if printf '%s\n' "$hermes_output" | rg -q '^HERMES_STACK_OK$' && \
     jq -e --arg model "$BONSAI_MODEL_ID" \
       '.model == $model and (.provider | startswith("custom")) and .api_calls >= 1' \
       "$hermes_usage" >/dev/null 2>&1; then
    pass "Hermes completes a real turn through its custom Bonsai provider"
  else
    fail "Hermes local inference turn failed"
  fi
  rm -f "$hermes_usage"
fi

if run_with_timeout 30 hermes status >/dev/null 2>&1; then
  pass "Hermes status command succeeds"
else
  fail "Hermes status command fails"
fi

printf '\nDoctor summary: %d failure(s), %d warning(s)\n' "$FAILURES" "$WARNINGS"
[ "$FAILURES" -eq 0 ]
