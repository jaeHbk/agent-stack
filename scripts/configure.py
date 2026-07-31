#!/usr/bin/env python3
"""Idempotent configuration and launchd rendering for the local agent stack."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import string
import sys
import tempfile
from html import escape
from pathlib import Path
from typing import Any


# Removed from active configuration because the upstream repository has no
# explicit license. Keep the identifier only to migrate existing installations.
LEGACY_PI_SETUP_REPOSITORY = "davis7dotsh/my-pi-setup"


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_text_atomic(path: Path, content: str, default_mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else default_mode
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_json_atomic(
    path: Path, value: dict[str, Any], default_mode: int = 0o600
) -> None:
    write_text_atomic(
        path, json.dumps(value, indent=2) + "\n", default_mode=default_mode
    )


def package_source(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("source"), str):
        return entry["source"]
    return ""


def merge_pi_settings(
    settings: dict[str, Any],
    stack_root: Path,
    include_local_model: bool = True,
) -> dict[str, Any]:
    result = dict(settings)
    packages = result.get("packages", [])
    if not isinstance(packages, list):
        raise ValueError("settings.json packages must be an array")

    package_path = str(stack_root / "pi-package")
    filtered = [
        entry
        for entry in packages
        if LEGACY_PI_SETUP_REPOSITORY not in package_source(entry)
        and package_path != package_source(entry)
    ]
    filtered.append(package_path)
    result["packages"] = filtered

    enabled = result.get("enabledModels", [])
    if enabled is not None:
        if not isinstance(enabled, list) or not all(
            isinstance(item, str) for item in enabled
        ):
            raise ValueError("settings.json enabledModels must be a string array")
        local_model = "bonsai/bonsai-27b"
        enabled = [item for item in enabled if item != local_model]
        if include_local_model:
            enabled.append(local_model)
        result["enabledModels"] = enabled
    return result


def merge_pi_models(
    models: dict[str, Any],
    api_key_file: Path | None = None,
    include_local_model: bool = True,
) -> dict[str, Any]:
    result = dict(models)
    providers = result.get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("models.json providers must be an object")
    providers = dict(providers)
    if not include_local_model:
        providers.pop("bonsai", None)
        result["providers"] = providers
        return result
    if api_key_file is None:
        api_key_file = (
            Path.home()
            / ".local"
            / "share"
            / "agent-stack"
            / "secrets"
            / "bonsai-api-key"
        )
    api_key_command = "!cat " + shlex.quote(str(api_key_file.expanduser().resolve()))
    providers["bonsai"] = {
        "baseUrl": "http://127.0.0.1:8080/v1",
        "api": "openai-completions",
        "apiKey": api_key_command,
        "authHeader": True,
        "compat": {
            "supportsDeveloperRole": False,
            "supportsReasoningEffort": False,
            "thinkingFormat": "qwen-chat-template",
        },
        "models": [
            {
                "id": "bonsai-27b",
                "name": "Ternary Bonsai 27B (Local)",
                "reasoning": True,
                "input": ["text"],
                "contextWindow": 65536,
                "maxTokens": 16384,
                "cost": {
                    "input": 0,
                    "output": 0,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                },
            }
        ],
    }
    result["providers"] = providers
    return result


def remove_pi_settings(
    settings: dict[str, Any], stack_root: Path
) -> dict[str, Any]:
    result = dict(settings)
    packages = result.get("packages", [])
    if isinstance(packages, list):
        result["packages"] = [
            entry
            for entry in packages
            if LEGACY_PI_SETUP_REPOSITORY not in package_source(entry)
            and str(stack_root / "pi-package") != package_source(entry)
        ]
    enabled = result.get("enabledModels")
    if isinstance(enabled, list):
        result["enabledModels"] = [
            item for item in enabled if item != "bonsai/bonsai-27b"
        ]
    return result


def remove_pi_models(models: dict[str, Any]) -> dict[str, Any]:
    result = dict(models)
    providers = result.get("providers")
    if isinstance(providers, dict):
        providers = dict(providers)
        providers.pop("bonsai", None)
        result["providers"] = providers
    return result


def configure_pi(args: argparse.Namespace) -> int:
    home = args.home.expanduser().resolve()
    agent_dir = home / ".pi" / "agent"
    settings_path = agent_dir / "settings.json"
    models_path = agent_dir / "models.json"
    current_settings = read_json(settings_path)
    current_models = read_json(models_path)
    api_key_file = args.api_key_file
    if api_key_file is None:
        api_key_file = (
            home
            / ".local"
            / "share"
            / "agent-stack"
            / "secrets"
            / "bonsai-api-key"
        )
    if args.remove:
        desired_settings = remove_pi_settings(
            current_settings, args.stack_root.resolve()
        )
        desired_models = remove_pi_models(current_models)
    else:
        include_local_model = args.profile in {"local", "hybrid"}
        desired_settings = merge_pi_settings(
            current_settings,
            args.stack_root.resolve(),
            include_local_model=include_local_model,
        )
        desired_models = merge_pi_models(
            current_models,
            api_key_file,
            include_local_model=include_local_model,
        )

    if args.check:
        if current_settings != desired_settings or current_models != desired_models:
            print("Pi configuration drift detected", file=sys.stderr)
            return 1
        return 0

    if current_settings != desired_settings:
        write_json_atomic(settings_path, desired_settings)
    if current_models != desired_models:
        write_json_atomic(models_path, desired_models)
    return 0


def render_launch_agent(args: argparse.Namespace) -> int:
    template_text = args.template.read_text(encoding="utf-8")
    values = {
        "LABEL": args.label,
        "SERVER": str(args.server),
        "MODEL": str(args.model),
        "MODEL_ID": args.model_id,
        "HOST": args.host,
        "PORT": str(args.port),
        "CONTEXT": str(args.context),
        "API_KEY_FILE": str(args.api_key_file),
        "WORKING_DIR": str(args.working_dir),
        "STDOUT": str(args.log_dir / "bonsai.out.log"),
        "STDERR": str(args.log_dir / "bonsai.err.log"),
    }
    escaped = {key: escape(value, quote=True) for key, value in values.items()}
    rendered = string.Template(template_text).substitute(escaped)
    write_text_atomic(args.output, rendered, default_mode=0o644)
    return 0



def merge_dotenv(content: str, key: str, value: str) -> str:
    """Set one dotenv key without changing unrelated lines or duplicating it."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ValueError(f"invalid environment variable name: {key}")
    if not value or "\n" in value or "\r" in value:
        raise ValueError("dotenv value must be one non-empty line")

    matcher = re.compile(
        rf"^\s*(?:export\s+)?{re.escape(key)}\s*=", re.ASCII
    )
    output: list[str] = []
    replaced = False
    for line in content.splitlines():
        if matcher.match(line):
            if not replaced:
                output.append(f"{key}={value}")
                replaced = True
            continue
        output.append(line)
    if not replaced:
        output.append(f"{key}={value}")
    return "\n".join(output) + "\n"


def configure_dotenv(args: argparse.Namespace) -> int:
    value = args.value_file.read_text(encoding="utf-8").strip()
    current = (
        args.path.read_text(encoding="utf-8") if args.path.exists() else ""
    )
    desired = merge_dotenv(current, args.key, value)
    if current != desired:
        write_text_atomic(args.path, desired, default_mode=0o600)
    if args.path.exists():
        os.chmod(args.path, 0o600)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    pi_parser = subparsers.add_parser("pi")
    pi_parser.add_argument("--home", type=Path, default=Path.home())
    pi_parser.add_argument("--stack-root", type=Path, required=True)
    pi_parser.add_argument("--api-key-file", type=Path)
    pi_parser.add_argument(
        "--profile", choices=("local", "hybrid", "cloud"), default="local"
    )
    pi_parser.add_argument("--check", action="store_true")
    pi_parser.add_argument("--remove", action="store_true")
    pi_parser.set_defaults(handler=configure_pi)

    launch_parser = subparsers.add_parser("launch-agent")
    launch_parser.add_argument("--template", type=Path, required=True)
    launch_parser.add_argument("--output", type=Path, required=True)
    launch_parser.add_argument("--label", required=True)
    launch_parser.add_argument("--server", type=Path, required=True)
    launch_parser.add_argument("--model", type=Path, required=True)
    launch_parser.add_argument("--model-id", required=True)
    launch_parser.add_argument("--host", required=True)
    launch_parser.add_argument("--port", type=int, required=True)
    launch_parser.add_argument("--context", type=int, required=True)
    launch_parser.add_argument("--api-key-file", type=Path, required=True)
    launch_parser.add_argument("--working-dir", type=Path, required=True)
    launch_parser.add_argument("--log-dir", type=Path, required=True)
    launch_parser.set_defaults(handler=render_launch_agent)

    dotenv_parser = subparsers.add_parser("dotenv")
    dotenv_parser.add_argument("--path", type=Path, required=True)
    dotenv_parser.add_argument("--key", required=True)
    dotenv_parser.add_argument("--value-file", type=Path, required=True)
    dotenv_parser.set_defaults(handler=configure_dotenv)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.handler(args)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"configure.py: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
