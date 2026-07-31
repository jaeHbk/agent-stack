#!/usr/bin/env python3
"""Transactional launchd cutover between Hermes Metal and local Bonsai."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence

SCHEMA_VERSION = 1
METAL_LABELS = (
    "com.hermes.metal.engine",
    "com.hermes.metal.embed",
    "com.hermes.metal.watcher",
)


class CutoverError(RuntimeError):
    """A cutover or rollback could not preserve the service invariant."""


class Runner(Protocol):
    def run(self, arguments: Sequence[str]) -> int: ...


class ProcessRunner:
    def run(self, arguments: Sequence[str]) -> int:
        return subprocess.run(
            list(arguments), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class Cutover:
    domain: str
    bonsai_label: str
    bonsai_plist: Path
    launch_agents: Path
    disabled_dir: Path
    transaction_dir: Path
    health_url: str
    api_key_file: Path
    runner: Runner
    health_timeout: int = 240

    @property
    def manifest_path(self) -> Path:
        return self.transaction_dir / "manifest.json"

    def loaded(self, label: str) -> bool:
        return self.runner.run(["launchctl", "print", f"{self.domain}/{label}"]) == 0

    def stop(self, label: str) -> None:
        self.runner.run(["launchctl", "bootout", f"{self.domain}/{label}"])
        deadline = time.monotonic() + 30
        while self.loaded(label) and time.monotonic() < deadline:
            time.sleep(0.2)
        if self.loaded(label):
            raise CutoverError(f"launchd job did not stop: {label}")

    def load(self, label: str, plist: Path) -> None:
        for attempt in range(6):
            if self.runner.run(["launchctl", "bootstrap", self.domain, str(plist)]) == 0:
                return
            if self.loaded(label):
                return
            if attempt < 5:
                time.sleep(2)
        raise CutoverError(f"launchd job did not load: {label}")

    def healthy(self) -> bool:
        try:
            key = self.api_key_file.read_text(encoding="utf-8").strip()
            if not key:
                return False
            request = urllib.request.Request(
                self.health_url, headers={"Authorization": f"Bearer {key}"}
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError, ValueError):
            return False

    def wait_healthy(self) -> bool:
        deadline = time.monotonic() + self.health_timeout
        while time.monotonic() < deadline:
            if self.healthy():
                return True
            time.sleep(1)
        return False

    def _metal_plists(self) -> list[Path]:
        return sorted(self.launch_agents.glob("com.hermes.metal.*.plist"))

    def _begin(self) -> dict[str, Any]:
        if self.transaction_dir.exists():
            raise CutoverError("an unresolved cutover transaction already exists")
        (self.transaction_dir / "metal").mkdir(parents=True, mode=0o700)
        bonsai_existed = self.bonsai_plist.exists() or self.bonsai_plist.is_symlink()
        if bonsai_existed:
            shutil.copy2(self.bonsai_plist, self.transaction_dir / "bonsai.plist")
        metal_plists: list[str] = []
        for path in self._metal_plists():
            shutil.copy2(path, self.transaction_dir / "metal" / path.name)
            metal_plists.append(path.name)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "active",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "bonsai_loaded": self.loaded(self.bonsai_label),
            "bonsai_plist_existed": bonsai_existed,
            "metal_loaded": [label for label in METAL_LABELS if self.loaded(label)],
            "metal_plists": metal_plists,
        }
        atomic_json(self.manifest_path, manifest)
        return manifest

    def _load_manifest(self) -> dict[str, Any]:
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CutoverError(f"invalid cutover recovery state: {error}") from error
        if value.get("schema_version") != SCHEMA_VERSION or value.get("status") != "active":
            raise CutoverError("unsupported cutover recovery state")
        return value

    def rollback(self) -> None:
        manifest = self._load_manifest()
        errors: list[str] = []
        try:
            self.stop(self.bonsai_label)
        except CutoverError as error:
            errors.append(str(error))
        if manifest.get("bonsai_plist_existed"):
            source = self.transaction_dir / "bonsai.plist"
            if not source.is_file():
                errors.append("saved Bonsai plist is missing")
            else:
                self.bonsai_plist.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, self.bonsai_plist)
        else:
            self.bonsai_plist.unlink(missing_ok=True)
        for name in manifest.get("metal_plists", []):
            source = self.transaction_dir / "metal" / Path(name).name
            target = self.launch_agents / Path(name).name
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            else:
                errors.append(f"saved Metal plist is missing: {name}")
        if manifest.get("bonsai_loaded") and self.bonsai_plist.is_file():
            try:
                self.load(self.bonsai_label, self.bonsai_plist)
            except CutoverError as error:
                errors.append(str(error))
        for label in manifest.get("metal_loaded", []):
            plist = self.launch_agents / f"{label}.plist"
            if not plist.is_file():
                errors.append(f"cannot restore loaded Metal job without {plist.name}")
                continue
            try:
                self.load(label, plist)
            except CutoverError as error:
                errors.append(str(error))
        if errors:
            raise CutoverError("rollback incomplete: " + "; ".join(errors))
        shutil.rmtree(self.transaction_dir)

    def recover(self) -> bool:
        if not self.transaction_dir.exists():
            return False
        self.rollback()
        return True

    def _disable_metal_plists(self) -> None:
        self.disabled_dir.mkdir(parents=True, exist_ok=True)
        for source in self._metal_plists():
            target = self.disabled_dir / source.name
            if target.exists() or target.is_symlink():
                target = self.disabled_dir / f"{source.name}.duplicate-{_stamp()}"
            os.replace(source, target)

    def activate(self, staged_plist: Path) -> None:
        if not staged_plist.is_file():
            raise CutoverError(f"staged Bonsai plist is missing: {staged_plist}")
        self.recover()
        self._begin()
        try:
            for label in METAL_LABELS:
                if self.loaded(label):
                    self.stop(label)
            if self.loaded(self.bonsai_label):
                self.stop(self.bonsai_label)
            self.bonsai_plist.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.bonsai_plist.with_name(f".{self.bonsai_plist.name}.new")
            shutil.copy2(staged_plist, temporary)
            os.replace(temporary, self.bonsai_plist)
            self.load(self.bonsai_label, self.bonsai_plist)
            if not self.wait_healthy():
                raise CutoverError("Bonsai did not become healthy before the timeout")
            self._disable_metal_plists()
            receipt = {
                "schema_version": SCHEMA_VERSION,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "bonsai_label": self.bonsai_label,
                "metal_labels": list(METAL_LABELS),
            }
            atomic_json(self.transaction_dir.parent / "last-cutover.json", receipt)
            shutil.rmtree(self.transaction_dir)
        except BaseException as original:
            try:
                self.rollback()
            except BaseException as rollback_error:
                raise CutoverError(
                    f"cutover failed ({original}); automatic rollback also failed ({rollback_error})"
                ) from original
            raise

    def deactivate(self) -> None:
        self.recover()
        self._begin()
        try:
            if self.loaded(self.bonsai_label):
                self.stop(self.bonsai_label)
            for label in METAL_LABELS:
                if self.loaded(label):
                    self.stop(label)
            self._disable_metal_plists()
            receipt = {
                "schema_version": SCHEMA_VERSION,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "bonsai_label": self.bonsai_label,
                "profile": "cloud",
                "metal_labels": list(METAL_LABELS),
            }
            atomic_json(self.transaction_dir.parent / "last-cutover.json", receipt)
            shutil.rmtree(self.transaction_dir)
        except BaseException as original:
            try:
                self.rollback()
            except BaseException as rollback_error:
                raise CutoverError(
                    f"deactivation failed ({original}); automatic rollback also failed ({rollback_error})"
                ) from original
            raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("activate", "deactivate", "recover"))
    parser.add_argument("--domain", required=True)
    parser.add_argument("--bonsai-label", required=True)
    parser.add_argument("--bonsai-plist", type=Path, required=True)
    parser.add_argument("--launch-agents", type=Path, required=True)
    parser.add_argument("--disabled-dir", type=Path, required=True)
    parser.add_argument("--transaction-dir", type=Path, required=True)
    parser.add_argument("--health-url", required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--staged-plist", type=Path)
    parser.add_argument("--health-timeout", type=int, default=240)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    cutover = Cutover(
        domain=args.domain,
        bonsai_label=args.bonsai_label,
        bonsai_plist=args.bonsai_plist,
        launch_agents=args.launch_agents,
        disabled_dir=args.disabled_dir,
        transaction_dir=args.transaction_dir,
        health_url=args.health_url,
        api_key_file=args.api_key_file,
        runner=ProcessRunner(),
        health_timeout=args.health_timeout,
    )

    def interrupted(_signum: int, _frame: Any) -> None:
        raise InterruptedError("cutover interrupted")

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.command == "activate":
            if args.staged_plist is None:
                raise CutoverError("--staged-plist is required for activation")
            cutover.activate(args.staged_plist)
        elif args.command == "deactivate":
            cutover.deactivate()
        else:
            cutover.recover()
    except (CutoverError, OSError, InterruptedError) as error:
        print(f"service_cutover.py: {error}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
