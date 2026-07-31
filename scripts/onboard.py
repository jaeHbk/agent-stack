#!/usr/bin/env python3
"""Plan, reconcile, recover, and diagnose the portable agent stack."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import platform
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
VERSIONS_FILE = ROOT / "versions.env"
PROFILES = ("auto", "local", "hybrid", "cloud")
LOCAL_PROFILES = {"local", "hybrid"}
PHASES = ("backup", "pi", "hermes", "runtime", "model", "cutover", "links", "verify", "audit")
MUTATING_PHASES = {"pi", "hermes", "runtime", "model", "cutover", "links"}
PHASE_TIMEOUTS = {
    "backup": 180,
    "pi": 1800,
    "hermes": 3600,
    "runtime": 7200,
    "model": 14400,
    "cutover": 420,
    "links": 120,
    "verify": 1800,
    "audit": 1800,
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_versions(path: Path = VERSIONS_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclasses.dataclass(frozen=True)
class Paths:
    root: Path
    home: Path
    data: Path

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "Paths":
        home = Path(environment.get("HOME", str(Path.home()))).expanduser().resolve()
        data = Path(
            environment.get(
                "AGENT_STACK_DATA", str(home / ".local" / "share" / "agent-stack")
            )
        ).expanduser().resolve()
        return cls(ROOT, home, data)

    @property
    def state(self) -> Path:
        return self.data / "install-state.json"

    @property
    def logs(self) -> Path:
        return self.data / "logs"

    @property
    def diagnostics(self) -> Path:
        return self.data / "diagnostics"

    @property
    def lock(self) -> Path:
        return self.data / "setup.lock"

    @property
    def key_file(self) -> Path:
        return self.data / "secrets" / "bonsai-api-key"

    @property
    def llama_dir(self) -> Path:
        return self.data / "runtime" / "llama.cpp"

    @property
    def llama_server(self) -> Path:
        return self.llama_dir / "build" / "bin" / "llama-server"

    @property
    def model(self) -> Path:
        return self.data / "models" / load_versions()["BONSAI_FILE"]


@dataclasses.dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class CommandRunner:
    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self.environment = dict(environment or os.environ)

    def which(self, command: str) -> str | None:
        return shutil.which(command, path=self.environment.get("PATH"))

    def run(
        self,
        arguments: Sequence[str],
        timeout: float = 30,
        environment: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandResult:
        merged = dict(self.environment)
        if environment:
            merged.update(environment)
        try:
            result = subprocess.run(
                list(arguments),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=merged,
                cwd=cwd,
            )
            return CommandResult(result.returncode, result.stdout, result.stderr)
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode() if isinstance(error.stdout, bytes) else (error.stdout or "")
            stderr = error.stderr.decode() if isinstance(error.stderr, bytes) else (error.stderr or "")
            return CommandResult(124, stdout, stderr, timed_out=True)
        except OSError as error:
            return CommandResult(127, "", str(error))


@dataclasses.dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    summary: str
    remedy: str = ""

    def as_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class PreflightReport:
    requested_profile: str
    resolved_profile: str
    checks: list[CheckResult]

    @property
    def ok(self) -> bool:
        return not any(check.status == "fail" for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_profile": self.requested_profile,
            "resolved_profile": self.resolved_profile,
            "ok": self.ok,
            "checks": [check.as_dict() for check in self.checks],
        }


class Preflight:
    def __init__(
        self,
        paths: Paths,
        versions: Mapping[str, str],
        runner: CommandRunner,
        environment: Mapping[str, str],
        runtime_source: str,
        configure_shell: bool = False,
    ) -> None:
        self.paths = paths
        self.versions = versions
        self.runner = runner
        self.environment = environment
        self.runtime_source = runtime_source
        self.configure_shell = configure_shell

    def command_path(self, command: str) -> str | None:
        found = self.runner.which(command)
        if found:
            return found
        if command == "rg":
            bundled = self.paths.home / ".pi" / "agent" / "bin" / "rg"
            if bundled.is_file() and os.access(bundled, os.X_OK):
                return str(bundled)
        return None

    def memory_bytes(self) -> int | None:
        result = self.runner.run(["sysctl", "-n", "hw.memsize"], timeout=5)
        with contextlib.suppress(ValueError):
            if result.returncode == 0:
                return int(result.stdout.strip())
        result = self.runner.run(
            ["system_profiler", "SPHardwareDataType"], timeout=20
        )
        match = re.search(r"Memory:\s+(\d+)\s+GB", result.stdout)
        return int(match.group(1)) * 1024**3 if match else None

    def source_runtime_ready(self) -> bool:
        metal = self.runner.run(["xcrun", "-f", "metal"], timeout=10)
        return bool(
            self.command_path("cmake")
            and self.command_path("ninja")
            and metal.returncode == 0
        )

    def prebuilt_ready(self) -> bool:
        url = self.environment.get(
            "AGENT_STACK_LLAMA_PREBUILT_URL", self.versions.get("LLAMA_PREBUILT_URL", "")
        )
        checksum = self.environment.get(
            "AGENT_STACK_LLAMA_PREBUILT_SHA256",
            self.versions.get("LLAMA_PREBUILT_SHA256", ""),
        )
        return bool(url and re.fullmatch(r"[0-9a-fA-F]{64}", checksum))

    def local_runtime_possible(self) -> bool:
        if self.paths.llama_server.is_file() and os.access(self.paths.llama_server, os.X_OK):
            return True
        if self.runtime_source in {"auto", "prebuilt"} and self.prebuilt_ready():
            return True
        return self.runtime_source in {"auto", "source"} and self.source_runtime_ready()

    def aws_ready(self) -> bool:
        env = self.environment
        if env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"):
            return True
        if env.get("AWS_WEB_IDENTITY_TOKEN_FILE") or env.get(
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"
        ) or env.get("AWS_CONTAINER_CREDENTIALS_FULL_URI"):
            return True
        if env.get("AWS_PROFILE") and (
            (self.paths.home / ".aws" / "credentials").is_file()
            or (self.paths.home / ".aws" / "config").is_file()
        ):
            return True
        return (self.paths.home / ".aws" / "credentials").is_file()

    def resolve_profile(self, requested: str, system: str, machine: str) -> str:
        if requested != "auto":
            return requested
        memory = self.memory_bytes()
        local_hardware = (
            system == "Darwin"
            and machine == "arm64"
            and (memory is None or memory >= 24 * 1024**3)
        )
        if local_hardware and self.local_runtime_possible():
            return "local"
        if self.aws_ready():
            return "cloud"
        return "local"

    def _writable(self, path: Path) -> bool:
        candidate = path
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return candidate.exists() and os.access(candidate, os.W_OK)

    def _json_valid(self, path: Path) -> bool:
        if not path.exists():
            return True
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return isinstance(value, dict)
        except (OSError, json.JSONDecodeError):
            return False

    def _server_ready(self) -> bool:
        try:
            key = self.paths.key_file.read_text(encoding="utf-8").strip()
            if not key:
                return False
            port = self.versions.get("BONSAI_PORT", "8080")
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError, ValueError):
            return False

    def run(self, requested_profile: str) -> PreflightReport:
        checks: list[CheckResult] = []
        system_result = self.runner.run(["uname", "-s"], timeout=5)
        machine_result = self.runner.run(["uname", "-m"], timeout=5)
        system = system_result.stdout.strip() or platform.system()
        machine = machine_result.stdout.strip() or platform.machine()
        profile = self.resolve_profile(requested_profile, system, machine)

        if system == "Darwin":
            checks.append(CheckResult("platform", "pass", "macOS detected"))
        else:
            checks.append(
                CheckResult(
                    "platform",
                    "fail",
                    f"unsupported operating system: {system}",
                    "Use macOS; this release only manages user LaunchAgents.",
                )
            )
        if profile in LOCAL_PROFILES and machine != "arm64":
            checks.append(
                CheckResult(
                    "architecture",
                    "fail",
                    f"local profile requires arm64, found {machine}",
                    "Use --profile cloud or an Apple Silicon Mac.",
                )
            )
        else:
            checks.append(CheckResult("architecture", "pass", f"architecture is {machine}"))

        required = [
            "bash",
            "curl",
            "git",
            "jq",
            "lsof",
            "node",
            "npm",
            "plutil",
            "python3",
            "rg",
            "rsync",
            "shasum",
            "tar",
        ]
        missing = [name for name in required if not self.command_path(name)]
        if missing:
            remedy = (
                f"Install the pinned tool set with: {self.paths.root / 'install'} "
                "--install-deps"
            )
            checks.append(
                CheckResult(
                    "commands", "fail", "missing: " + ", ".join(missing), remedy
                )
            )
        else:
            checks.append(CheckResult("commands", "pass", "all base commands are available"))

        python_version = self.runner.run(
            [
                "python3",
                "-c",
                "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
            ],
            timeout=10,
        )
        try:
            python_parts = tuple(int(part) for part in python_version.stdout.strip().split(".")[:2])
        except ValueError:
            python_parts = (0, 0)
        if python_version.returncode == 0 and python_parts >= (3, 10):
            checks.append(
                CheckResult("python", "pass", f"Python is {python_version.stdout.strip()}")
            )
        else:
            checks.append(
                CheckResult(
                    "python",
                    "fail",
                    "Python 3.10 or newer is required",
                    "Run ./install --install-deps to use the pinned Python line.",
                )
            )

        node = self.runner.run(
            ["node", "-p", 'Number(process.versions.node.split(".")[0])'], timeout=10
        )
        try:
            node_major = int(node.stdout.strip()) if node.returncode == 0 else 0
        except ValueError:
            node_major = 0
        if node_major >= 20:
            checks.append(CheckResult("node", "pass", f"Node.js major is {node_major}"))
        else:
            checks.append(
                CheckResult(
                    "node",
                    "fail",
                    "Node.js 20 or newer is required",
                    "Run ./install --install-deps or activate a Node.js 20+ runtime.",
                )
            )

        memory = self.memory_bytes()
        if profile in LOCAL_PROFILES and memory is not None and memory < 24 * 1024**3:
            checks.append(
                CheckResult(
                    "memory",
                    "fail",
                    f"local profile needs 24 GiB RAM; found {memory / 1024**3:.1f} GiB",
                    "Use --profile cloud or a host with at least 24 GiB RAM.",
                )
            )
        elif memory is None:
            checks.append(CheckResult("memory", "warn", "could not determine physical memory"))
        else:
            checks.append(
                CheckResult("memory", "pass", f"physical memory is {memory / 1024**3:.1f} GiB")
            )

        if profile in LOCAL_PROFILES:
            if self.paths.llama_server.is_file():
                checks.append(CheckResult("runtime", "pass", "existing local runtime is available"))
            elif self.runtime_source == "prebuilt" and not self.prebuilt_ready():
                checks.append(
                    CheckResult(
                        "runtime",
                        "fail",
                        "prebuilt runtime was requested without a pinned URL and SHA-256",
                        "Set AGENT_STACK_LLAMA_PREBUILT_URL and AGENT_STACK_LLAMA_PREBUILT_SHA256.",
                    )
                )
            elif self.prebuilt_ready() and self.runtime_source != "source":
                checks.append(CheckResult("runtime", "pass", "verified prebuilt runtime is configured"))
            elif self.source_runtime_ready():
                checks.append(CheckResult("runtime", "pass", "source-build toolchain includes Metal"))
            else:
                checks.append(
                    CheckResult(
                        "runtime",
                        "fail",
                        "no verified prebuilt runtime or complete Metal build toolchain is available",
                        "Provide a pinned prebuilt artifact, or install full Xcode/Metal plus cmake and ninja.",
                    )
                )

            try:
                free = shutil.disk_usage(self.paths.home).free
            except OSError:
                free = 0
            needed = 15 * 1024**3 if not self.paths.model.is_file() else 2 * 1024**3
            if free < needed:
                checks.append(
                    CheckResult(
                        "disk",
                        "fail",
                        f"only {free / 1024**3:.1f} GiB is free",
                        f"Free at least {needed / 1024**3:.0f} GiB before setup.",
                    )
                )
            else:
                checks.append(CheckResult("disk", "pass", f"{free / 1024**3:.1f} GiB is free"))

            domain = f"gui/{os.getuid()}"
            launchd = self.runner.run(["launchctl", "print", domain], timeout=10)
            if launchd.returncode == 0:
                checks.append(CheckResult("launchd", "pass", "GUI LaunchAgent domain is available"))
            else:
                checks.append(
                    CheckResult(
                        "launchd",
                        "fail",
                        "GUI LaunchAgent domain is unavailable",
                        "Run setup from a logged-in macOS desktop session.",
                    )
                )

            port = self.versions.get("BONSAI_PORT", "8080")
            listener = self.runner.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], timeout=10
            )
            managed_job = self.runner.run(
                ["launchctl", "print", f"gui/{os.getuid()}/com.local.agent-stack.bonsai"],
                timeout=10,
            )
            pid_match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", managed_job.stdout, re.M)
            listener_pids = {
                fields[1]
                for line in listener.stdout.splitlines()[1:]
                if len(fields := line.split()) > 1
            }
            managed_listener = bool(
                managed_job.returncode == 0
                and pid_match
                and pid_match.group(1) in listener_pids
                and f"127.0.0.1:{port}" in listener.stdout
            )
            if (
                listener.returncode == 0
                and listener.stdout.strip()
                and not managed_listener
                and not self._server_ready()
            ):
                summary = listener.stdout.splitlines()[-1][:240]
                checks.append(
                    CheckResult(
                        "port",
                        "fail",
                        f"port {port} is occupied by an unmanaged listener: {summary}",
                        f"Stop that process or change BONSAI_PORT before setup.",
                    )
                )
            else:
                checks.append(CheckResult("port", "pass", f"port {port} is free or stack-managed"))

        aws = self.aws_ready()
        if profile == "cloud" and not aws:
            checks.append(
                CheckResult(
                    "bedrock",
                    "fail",
                    "AWS credential-chain readiness was not detected",
                    "Authenticate with AWS_PROFILE/AWS SSO or standard AWS credentials, then rerun.",
                )
            )
        elif profile == "hybrid" and not aws:
            checks.append(
                CheckResult(
                    "bedrock",
                    "warn",
                    "local setup can continue, but Bedrock escalation is not ready",
                    "Authenticate an AWS profile before using pi-sonnet or pi-opus.",
                )
            )
        elif profile in {"cloud", "hybrid"}:
            region = self.environment.get("AWS_REGION") or self.environment.get(
                "AWS_DEFAULT_REGION"
            ) or self.versions.get("BEDROCK_REGION", "")
            checks.append(CheckResult("bedrock", "pass", f"AWS credential shape is present; region {region}"))

        local_bin = self.paths.home / ".local" / "bin"
        path_parts = self.environment.get("PATH", "").split(os.pathsep)
        if str(local_bin) in path_parts or self.configure_shell:
            checks.append(CheckResult("path", "pass", f"{local_bin} will be discoverable"))
        else:
            checks.append(
                CheckResult(
                    "path",
                    "warn",
                    f"{local_bin} is not on PATH",
                    "Rerun with --configure-shell or add it to PATH manually.",
                )
            )

        for name, path in (
            ("stack data", self.paths.data),
            ("local bin", local_bin),
            ("Pi config", self.paths.home / ".pi" / "agent"),
            ("Hermes home", self.paths.home / ".hermes"),
        ):
            if self._writable(path):
                checks.append(CheckResult(f"writable:{name}", "pass", f"{path} is writable"))
            else:
                checks.append(
                    CheckResult(
                        f"writable:{name}",
                        "fail",
                        f"{path} is not writable",
                        f"Correct ownership/permissions for {path}; do not run setup with sudo.",
                    )
                )

        for path in (
            self.paths.home / ".pi" / "agent" / "settings.json",
            self.paths.home / ".pi" / "agent" / "models.json",
        ):
            if self._json_valid(path):
                checks.append(CheckResult(f"json:{path.name}", "pass", f"{path.name} is valid"))
            else:
                checks.append(
                    CheckResult(
                        f"json:{path.name}",
                        "fail",
                        f"{path} is not a JSON object",
                        "Repair the JSON or restore it from backup before setup.",
                    )
                )

        npm = self.runner.run(["npm", "config", "get", "prefix"], timeout=10)
        if npm.returncode == 0 and npm.stdout.strip() and self._writable(Path(npm.stdout.strip())):
            checks.append(CheckResult("npm-prefix", "pass", "global npm prefix is writable"))
        else:
            checks.append(
                CheckResult(
                    "npm-prefix",
                    "fail",
                    "global npm prefix is unavailable or not writable",
                    "Use a user-owned Node installation/prefix; do not run npm setup with sudo.",
                )
            )

        return PreflightReport(requested_profile, profile, checks)


class StateStore:
    def __init__(self, path: Path, schema: int) -> None:
        self.path = path
        self.schema = schema

    def load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": self.schema, "phases": {}}
        if not isinstance(value, dict):
            return {"schema_version": self.schema, "phases": {}}
        if value.get("schema_version") != self.schema:
            return {
                "schema_version": self.schema,
                "migrated_from": value,
                "phases": {},
            }
        if not isinstance(value.get("phases"), dict):
            value["phases"] = {}
        return value

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state["schema_version"] = self.schema
        state["updated_at"] = utc_now()
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


class SetupLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "SetupLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.handle.close()
            raise RuntimeError("another agent-stack setup is already running") from error
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"pid={os.getpid()} started={utc_now()}\n")
        self.handle.flush()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


class Redactor:
    ASSIGNMENT = re.compile(
        r"(?i)(AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|API[_-]?KEY|TOKEN|PASSWORD|SECRET)(\s*[=:]\s*)([^\s]+)"
    )
    BEARER = re.compile(r"(?i)(Authorization:\s*Bearer\s+)[^\s]+")
    AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        self.secrets = [value for value in secrets if value]

    def __call__(self, value: str) -> str:
        result = value
        for secret in self.secrets:
            result = result.replace(secret, "<redacted>")
        result = self.ASSIGNMENT.sub(r"\1\2<redacted>", result)
        result = self.BEARER.sub(r"\1<redacted>", result)
        result = self.AWS_KEY.sub("<redacted-aws-key>", result)
        return result


class SetupLogger:
    def __init__(self, path: Path, redactor: Redactor, quiet: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.redactor = redactor
        self.quiet = quiet
        self.handle = path.open("a", encoding="utf-8")
        os.chmod(path, 0o600)

    def close(self) -> None:
        self.handle.close()

    def event(self, phase: str, text: str, display: bool = True) -> None:
        cleaned = self.redactor(text)
        prefix = f"[{utc_now()}] [{phase}] "
        self.handle.write(prefix + cleaned)
        if not cleaned.endswith("\n"):
            self.handle.write("\n")
        self.handle.flush()
        if display and not self.quiet:
            print(cleaned, end="" if cleaned.endswith(("\n", "\r")) else "\n", flush=True)


@dataclasses.dataclass(frozen=True)
class PhasePlan:
    name: str
    action: str
    reason: str
    fingerprint: str

    def as_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


def phase_fingerprint(
    phase: str, profile: str, runtime_source: str, versions: Mapping[str, str]
) -> str:
    phase_keys = {
        "pi": ("PI_VERSION", "BRACE_EXPANSION_VERSION"),
        "hermes": ("HERMES_COMMIT", "HERMES_FEISHU_PATCH_SHA256", "HERMES_DEPENDENCY_PATCH_SHA256", "BEDROCK_MODEL", "BEDROCK_REGION"),
        "runtime": ("LLAMA_COMMIT", "LLAMA_PREBUILT_SHA256", "LLAMA_PREBUILT_SIGNATURE_POLICY"),
        "model": ("BONSAI_REVISION", "BONSAI_FILE", "BONSAI_SIZE", "BONSAI_SHA256"),
        "cutover": ("BONSAI_HOST", "BONSAI_PORT", "BONSAI_CONTEXT", "BONSAI_MODEL_ID"),
        "links": ("PI_VERSION",),
        "verify": tuple(),
        "audit": ("HERMES_COMMIT",),
        "backup": tuple(),
    }
    payload = {
        "phase": phase,
        "profile": profile,
        "runtime_source": runtime_source if phase == "runtime" else "",
        "versions": {key: versions.get(key, "") for key in phase_keys[phase]},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class Onboarding:
    def __init__(
        self,
        paths: Paths,
        versions: Mapping[str, str],
        runner: CommandRunner,
        environment: Mapping[str, str],
    ) -> None:
        self.paths = paths
        self.versions = versions
        self.runner = runner
        self.environment = dict(environment)
        self.schema = int(versions.get("ONBOARDING_STATE_SCHEMA", "2"))
        self.store = StateStore(paths.state, self.schema)

    def command_environment(self, profile: str) -> dict[str, str]:
        result = dict(self.environment)
        bundled = self.paths.home / ".pi" / "agent" / "bin"
        result["PATH"] = f"{bundled}{os.pathsep}{result.get('PATH', '')}"
        result["AGENT_STACK_PROFILE"] = profile
        result["AGENT_STACK_DATA"] = str(self.paths.data)
        result["HOME"] = str(self.paths.home)
        return result

    def phase_healthy(self, phase: str, profile: str, runtime_source: str) -> bool:
        if phase in {"backup", "verify", "audit"}:
            return False
        command = [
            "/bin/bash",
            str(self.paths.root / "scripts" / "bootstrap.sh"),
            "--check-phase",
            phase,
            "--profile",
            profile,
            "--runtime-source",
            runtime_source,
        ]
        result = self.runner.run(
            command,
            timeout=45,
            environment=self.command_environment(profile),
            cwd=self.paths.root,
        )
        return result.returncode == 0

    def plan(
        self,
        profile: str,
        runtime_source: str,
        repair: bool,
        skip_audit: bool,
    ) -> list[PhasePlan]:
        state = self.store.load()
        checkpoints = state.get("phases", {})
        plans: list[PhasePlan] = []
        for phase in PHASES:
            fingerprint = phase_fingerprint(phase, profile, runtime_source, self.versions)
            if phase in {"runtime", "model"} and profile == "cloud":
                plans.append(PhasePlan(phase, "skip", "cloud profile does not run local inference", fingerprint))
                continue
            if phase == "audit" and skip_audit:
                plans.append(PhasePlan(phase, "skip", "security audit explicitly skipped", fingerprint))
                continue
            if phase == "backup":
                plans.append(PhasePlan(phase, "pending", "depends on mutation plan", fingerprint))
                continue
            checkpoint = checkpoints.get(phase, {}) if isinstance(checkpoints, dict) else {}
            if (
                not repair
                and phase not in {"verify", "audit"}
                and checkpoint.get("status") == "complete"
                and checkpoint.get("fingerprint") == fingerprint
            ):
                plans.append(PhasePlan(phase, "skip", "matching checkpoint", fingerprint))
            elif self.phase_healthy(phase, profile, runtime_source):
                plans.append(PhasePlan(phase, "adopt", "installed state already satisfies the phase", fingerprint))
            else:
                reason = "verification is always refreshed" if phase == "verify" else "reconciliation required"
                plans.append(PhasePlan(phase, "run", reason, fingerprint))
        mutations = any(plan.action == "run" and plan.name in MUTATING_PHASES for plan in plans)
        plans[0] = PhasePlan(
            "backup",
            "run" if mutations else "skip",
            "create one transaction backup" if mutations else "no mutations planned",
            plans[0].fingerprint,
        )
        return plans

    def run_streamed(
        self,
        arguments: Sequence[str],
        phase: str,
        timeout: int,
        profile: str,
        logger: SetupLogger,
    ) -> int:
        logger.event(phase, f"→ {phase}\n")
        process = subprocess.Popen(
            list(arguments),
            cwd=self.paths.root,
            env=self.command_environment(profile),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert process.stdout is not None
        deadline = time.monotonic() + timeout
        fd = process.stdout.fileno()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    logger.event(phase, f"phase timed out after {timeout}s\n")
                    return 124
                readable, _, _ = select.select([fd], [], [], min(0.25, remaining))
                if readable:
                    chunk = os.read(fd, 8192)
                    if chunk:
                        logger.event(phase, chunk.decode("utf-8", errors="replace"))
                    elif process.poll() is not None:
                        break
                elif process.poll() is not None:
                    chunk = os.read(fd, 8192)
                    if chunk:
                        logger.event(phase, chunk.decode("utf-8", errors="replace"))
                        continue
                    break
            return process.wait()
        except BaseException:
            if process.poll() is None:
                process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
            raise

    def phase_command(
        self, phase: str, profile: str, runtime_source: str, verify: str
    ) -> list[str]:
        if phase == "verify":
            command = [str(self.paths.root / "scripts" / "doctor.sh"), "--profile", profile]
            if verify == "quick":
                command.append("--quick")
            return command
        if phase == "audit":
            return [str(self.paths.root / "scripts" / "security-audit.sh")]
        return [
            "/bin/bash",
            str(self.paths.root / "scripts" / "bootstrap.sh"),
            "--phase",
            phase,
            "--profile",
            profile,
            "--runtime-source",
            runtime_source,
        ]

    def configure_shell(self) -> bool:
        path = self.paths.home / ".zprofile"
        marker = "# agent-stack PATH"
        line = 'export PATH="$HOME/.local/bin:$PATH"'
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if line in current:
            return False
        backup = self.paths.data / "backups" / f"shell-{stamp()}"
        backup.mkdir(parents=True, mode=0o700)
        if path.exists():
            shutil.copy2(path, backup / path.name)
        desired = current
        if desired and not desired.endswith("\n"):
            desired += "\n"
        desired += f"\n{marker}\n{line}\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(desired)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(temporary, path)
        return True

    def execute(
        self,
        plans: list[PhasePlan],
        profile: str,
        runtime_source: str,
        verify: str,
        configure_shell: bool,
        json_output: bool,
    ) -> int:
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        secrets: list[str] = []
        with contextlib.suppress(OSError):
            secrets.append(self.paths.key_file.read_text(encoding="utf-8").strip())
        logger = SetupLogger(
            self.paths.logs / f"setup-{stamp()}.log", Redactor(secrets), quiet=json_output
        )
        state = self.store.load()
        state.update(
            {
                "schema_version": self.schema,
                "profile": profile,
                "stack_root": str(self.paths.root),
                "versions": {
                    "pi": self.versions.get("PI_VERSION"),
                    "hermes_commit": self.versions.get("HERMES_COMMIT"),
                    "llama_commit": self.versions.get("LLAMA_COMMIT"),
                    "model_revision": self.versions.get("BONSAI_REVISION"),
                },
            }
        )
        phases = state.setdefault("phases", {})
        try:
            for plan in plans:
                if plan.action == "skip":
                    logger.event(plan.name, f"✓ {plan.name}: {plan.reason}\n")
                    continue
                if plan.action == "adopt":
                    phases[plan.name] = {
                        "status": "complete",
                        "fingerprint": plan.fingerprint,
                        "completed_at": utc_now(),
                        "adopted": True,
                    }
                    self.store.save(state)
                    logger.event(plan.name, f"✓ {plan.name}: adopted healthy installed state\n")
                    continue
                phases[plan.name] = {
                    "status": "running",
                    "fingerprint": plan.fingerprint,
                    "started_at": utc_now(),
                }
                self.store.save(state)
                command = self.phase_command(plan.name, profile, runtime_source, verify)
                status = self.run_streamed(
                    command,
                    plan.name,
                    PHASE_TIMEOUTS[plan.name],
                    profile,
                    logger,
                )
                if status:
                    phases[plan.name].update(
                        {"status": "failed", "failed_at": utc_now(), "exit_code": status}
                    )
                    self.store.save(state)
                    raise RuntimeError(f"phase {plan.name} failed with exit code {status}")
                phases[plan.name].update(
                    {"status": "complete", "completed_at": utc_now(), "adopted": False}
                )
                self.store.save(state)
                logger.event(plan.name, f"✓ {plan.name} complete\n")
                if plan.name == "links" and configure_shell:
                    changed = self.configure_shell()
                    logger.event(
                        "links",
                        "✓ shell PATH configured; open a new shell\n"
                        if changed
                        else "✓ shell PATH was already configured\n",
                    )
            state["completed_at"] = utc_now()
            self.store.save(state)
            if json_output:
                print(json.dumps({"ok": True, "profile": profile, "state": str(self.paths.state)}))
            else:
                self.success(profile)
            return 0
        except (OSError, RuntimeError, KeyboardInterrupt) as error:
            logger.event("error", f"ERROR: {error}\n")
            bundle = create_diagnostics(
                self.paths, self.versions, self.runner, self.environment, logger.path
            )
            print(
                f"Setup stopped safely. Diagnostics: {bundle}\n"
                "Fix the reported phase and rerun the same setup command; completed checkpoints resume.",
                file=sys.stderr,
            )
            return 1
        finally:
            logger.close()

    def success(self, profile: str) -> None:
        print("\nAgent stack is ready.")
        print(f"  Profile:     {profile}")
        print(f"  State:       {self.paths.state}")
        if profile in LOCAL_PROFILES:
            port = self.versions.get("BONSAI_PORT", "8080")
            print(f"  Local model: http://127.0.0.1:{port}/v1 (authenticated)")
        else:
            print(
                f"  Hermes:      Bedrock / {self.versions.get('BEDROCK_MODEL', 'configured model')}"
            )
        print("\nUse:")
        print("  hermes       coordinate multi-step work and retain durable context")
        if profile in LOCAL_PROFILES:
            print("  pi-local     local read-heavy and bounded coding work")
        print("  pi-sonnet    normal cloud implementation")
        print("  pi-opus      hard/high-risk work and independent review")
        print("  agent-stack doctor --quick   recheck health")
        print("  agent-stack diagnostics      create a redacted support bundle")


def print_preflight(report: PreflightReport) -> None:
    print(f"Profile: {report.requested_profile} → {report.resolved_profile}")
    for check in report.checks:
        label = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[check.status]
        print(f"{label:<5} {check.name:<20} {check.summary}")
    remedies = list(dict.fromkeys(check.remedy for check in report.checks if check.remedy))
    if remedies:
        print("\nRemedies:")
        for remedy in remedies:
            print(f"  - {remedy}")


def print_plan(plans: Sequence[PhasePlan]) -> None:
    print("\nExecution plan:")
    for plan in plans:
        print(f"  {plan.action.upper():<5} {plan.name:<10} {plan.reason}")


def install_dependencies(paths: Paths, runner: CommandRunner) -> int:
    brew = runner.which("brew")
    if not brew:
        print(
            "Homebrew is not installed and will not be installed implicitly.\n"
            "Install it from https://brew.sh, then rerun with --install-deps.",
            file=sys.stderr,
        )
        return 2
    try:
        result = subprocess.run(
            [brew, "bundle", "--file", str(paths.root / "Brewfile")],
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        print("brew bundle timed out after 30 minutes", file=sys.stderr)
        return 124
    if result.returncode == 0:
        prefixes: list[str] = []
        for formula in (None, "node@22", "python@3.13"):
            command = [brew, "--prefix"] + ([formula] if formula else [])
            prefix = runner.run(command, timeout=30)
            if prefix.returncode == 0 and prefix.stdout.strip():
                prefixes.append(str(Path(prefix.stdout.strip()) / "bin"))
                if formula is None:
                    prefixes.append(str(Path(prefix.stdout.strip()) / "sbin"))
        current = runner.environment.get("PATH", "")
        runner.environment["PATH"] = os.pathsep.join([*prefixes, current])
        os.environ["PATH"] = runner.environment["PATH"]
    return result.returncode


def _capture_diagnostic(
    runner: CommandRunner,
    arguments: Sequence[str],
    environment: Mapping[str, str],
    timeout: int = 30,
) -> str:
    result = runner.run(arguments, timeout=timeout, environment=environment, cwd=ROOT)
    return f"exit={result.returncode} timeout={result.timed_out}\n{result.stdout}{result.stderr}"


def create_diagnostics(
    paths: Paths,
    versions: Mapping[str, str],
    runner: CommandRunner,
    environment: Mapping[str, str],
    setup_log: Path | None = None,
    output: Path | None = None,
) -> Path:
    paths.diagnostics.mkdir(parents=True, exist_ok=True)
    output = (output or paths.diagnostics / f"agent-stack-diagnostics-{stamp()}.tar.gz").expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    secrets: list[str] = []
    with contextlib.suppress(OSError):
        secrets.append(paths.key_file.read_text(encoding="utf-8").strip())
    redact = Redactor(secrets)
    stage = Path(tempfile.mkdtemp(prefix="agent-stack-diagnostics-"))
    try:
        metadata = {
            "created_at": utc_now(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "stack_root": str(paths.root),
            "stack_data": str(paths.data),
            "pins": {
                key: value
                for key, value in versions.items()
                if not re.search(r"KEY|TOKEN|SECRET|PASSWORD", key, re.I)
            },
        }
        (stage / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if paths.state.is_file():
            (stage / "install-state.json").write_text(
                redact(paths.state.read_text(encoding="utf-8")), encoding="utf-8"
            )
        command_env = dict(environment)
        bundled = paths.home / ".pi" / "agent" / "bin"
        command_env["PATH"] = f"{bundled}{os.pathsep}{command_env.get('PATH', '')}"
        command_env["HOME"] = str(paths.home)
        command_env["AGENT_STACK_DATA"] = str(paths.data)
        (stage / "status.txt").write_text(
            redact(
                _capture_diagnostic(
                    runner, [str(paths.root / "bin" / "agent-stack"), "status"], command_env
                )
            ),
            encoding="utf-8",
        )
        if setup_log and setup_log.is_file():
            (stage / "setup.log").write_text(
                redact(setup_log.read_text(encoding="utf-8", errors="replace")),
                encoding="utf-8",
            )
        with tarfile.open(output, "w:gz") as archive:
            for path in sorted(stage.iterdir()):
                archive.add(path, arcname=path.name, recursive=False)
        os.chmod(output, 0o600)
        return output
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def add_setup_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan", action="store_true", help="print the exact no-mutation plan")
    parser.add_argument("--repair", action="store_true", help="re-probe every phase and reconcile drift")
    parser.add_argument("--profile", choices=PROFILES, default="auto")
    parser.add_argument("--runtime-source", choices=("auto", "prebuilt", "source"), default="auto")
    parser.add_argument("--install-deps", action="store_true")
    parser.add_argument("--configure-shell", action="store_true")
    parser.add_argument("--verify", choices=("quick", "full"), default="quick")
    parser.add_argument("--skip-security-audit", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-stack")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="plan or reconcile the complete stack")
    add_setup_arguments(setup)
    preflight = commands.add_parser("preflight", help="report every prerequisite together")
    preflight.add_argument("--profile", choices=PROFILES, default="auto")
    preflight.add_argument("--runtime-source", choices=("auto", "prebuilt", "source"), default="auto")
    preflight.add_argument("--configure-shell", action="store_true")
    preflight.add_argument("--json", action="store_true")
    diagnostics = commands.add_parser("diagnostics", help="create a redacted diagnostics archive")
    diagnostics.add_argument("output", nargs="?", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    environment = dict(os.environ)
    paths = Paths.from_environment(environment)
    versions = load_versions()
    runner = CommandRunner(environment)
    if args.command == "diagnostics":
        output = create_diagnostics(paths, versions, runner, environment, output=args.output)
        print(output)
        return 0
    preflight = Preflight(
        paths,
        versions,
        runner,
        environment,
        args.runtime_source,
        getattr(args, "configure_shell", False),
    )
    if args.command == "preflight":
        report = preflight.run(args.profile)
        if args.json:
            print(json.dumps(report.as_dict(), indent=2))
        else:
            print_preflight(report)
        return 0 if report.ok else 2
    onboarding = Onboarding(paths, versions, runner, environment)
    if args.plan:
        report = preflight.run(args.profile)
        if args.json:
            print(json.dumps({"preflight": report.as_dict()}))
        else:
            print_preflight(report)
        if not report.ok:
            return 2
        plans = onboarding.plan(
            report.resolved_profile,
            args.runtime_source,
            args.repair,
            args.skip_security_audit,
        )
        if args.json:
            print(json.dumps({"plan": [plan.as_dict() for plan in plans]}))
        else:
            print_plan(plans)
        return 0
    try:
        with SetupLock(paths.lock):
            if args.install_deps and install_dependencies(paths, runner):
                return 2
            # Dependency installation can change PATH, so probe only after it
            # completes and while holding the same lock as reconciliation.
            environment.update(runner.environment)
            preflight = Preflight(
                paths,
                versions,
                runner,
                environment,
                args.runtime_source,
                args.configure_shell,
            )
            report = preflight.run(args.profile)
            if args.json:
                print(json.dumps({"preflight": report.as_dict()}))
            else:
                print_preflight(report)
            if not report.ok:
                return 2
            onboarding = Onboarding(paths, versions, runner, environment)
            plans = onboarding.plan(
                report.resolved_profile,
                args.runtime_source,
                args.repair,
                args.skip_security_audit,
            )
            if args.json:
                print(json.dumps({"plan": [plan.as_dict() for plan in plans]}))
            else:
                print_plan(plans)
            if not args.yes and not args.non_interactive and sys.stdin.isatty():
                answer = input("\nApply this plan? [y/N] ").strip().lower()
                if answer not in {"y", "yes"}:
                    print("No changes made.")
                    return 0
            return onboarding.execute(
                plans,
                report.resolved_profile,
                args.runtime_source,
                args.verify,
                args.configure_shell,
                args.json,
            )
    except RuntimeError as error:
        print(f"agent-stack setup: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
