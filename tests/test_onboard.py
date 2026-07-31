from __future__ import annotations

import io
import json
import os
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import onboard  # noqa: E402


class FakeRunner:
    def __init__(self, commands: set[str] | None = None) -> None:
        self.commands = commands or set()
        self.environment = {"PATH": "/fake/bin"}
        self.results: dict[tuple[str, ...], onboard.CommandResult] = {}

    def which(self, command: str) -> str | None:
        return f"/fake/bin/{command}" if command in self.commands else None

    def run(self, arguments, timeout=30, environment=None, cwd=None):
        key = tuple(arguments)
        if key in self.results:
            return self.results[key]
        if key == ("uname", "-s"):
            return onboard.CommandResult(0, "Darwin\n")
        if key == ("uname", "-m"):
            return onboard.CommandResult(0, "arm64\n")
        if key == ("sysctl", "-n", "hw.memsize"):
            return onboard.CommandResult(0, str(36 * 1024**3))
        if key[:2] == ("system_profiler", "SPHardwareDataType"):
            return onboard.CommandResult(0, "Memory: 36 GB\n")
        if key and key[0] == "node":
            return onboard.CommandResult(0, "22\n")
        if key[:3] == ("npm", "config", "get"):
            return onboard.CommandResult(0, str(Path(tempfile.gettempdir())))
        if key[:3] == ("launchctl", "print", "gui/501"):
            return onboard.CommandResult(0)
        if key and key[0] == "launchctl":
            return onboard.CommandResult(1)
        if key and key[0] == "lsof":
            return onboard.CommandResult(1)
        if key[:3] == ("xcrun", "-f", "metal"):
            return onboard.CommandResult(1)
        return onboard.CommandResult(1)


class StateAndPlanTests(unittest.TestCase):
    def test_state_write_is_atomic_private_and_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "state.json"
            store = onboard.StateStore(path, 2)
            value = {"profile": "local", "phases": {"pi": {"status": "complete"}}}
            store.save(value)
            loaded = store.load()
            self.assertEqual(loaded["profile"], "local")
            self.assertEqual(loaded["schema_version"], 2)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(path.parent.glob(".state.json.*")), [])

    def test_old_state_is_migrated_without_being_trusted_as_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_text('{"model_revision":"old"}\n', encoding="utf-8")
            state = onboard.StateStore(path, 2).load()
            self.assertEqual(state["schema_version"], 2)
            self.assertEqual(state["phases"], {})
            self.assertEqual(state["migrated_from"]["model_revision"], "old")

    def test_lock_rejects_concurrent_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "setup.lock"
            with onboard.SetupLock(path):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with onboard.SetupLock(path):
                        pass

    def test_fingerprint_changes_with_profile_and_runtime_source(self) -> None:
        versions = onboard.load_versions()
        local = onboard.phase_fingerprint("runtime", "local", "source", versions)
        prebuilt = onboard.phase_fingerprint("runtime", "local", "prebuilt", versions)
        cloud = onboard.phase_fingerprint("runtime", "cloud", "source", versions)
        self.assertNotEqual(local, prebuilt)
        self.assertNotEqual(local, cloud)

    def test_rerun_adopts_healthy_phases_without_backup(self) -> None:
        class Healthy(onboard.Onboarding):
            def phase_healthy(self, phase, profile, runtime_source):
                return phase not in {"verify", "audit", "backup"}

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = onboard.Paths(ROOT, base / "home", base / "data")
            paths.home.mkdir()
            manager = Healthy(paths, onboard.load_versions(), FakeRunner(), {})
            plan = manager.plan("local", "auto", repair=True, skip_audit=False)
            actions = {item.name: item.action for item in plan}
            self.assertEqual(actions["backup"], "skip")
            self.assertEqual(actions["pi"], "adopt")
            self.assertEqual(actions["cutover"], "adopt")
            self.assertEqual(actions["verify"], "run")
            self.assertEqual(actions["audit"], "run")

    def test_matching_checkpoint_skips_probe(self) -> None:
        class NeverProbe(onboard.Onboarding):
            def phase_healthy(self, phase, profile, runtime_source):
                if phase == "pi":
                    raise AssertionError("matching checkpoint should skip probe")
                return True

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = onboard.Paths(ROOT, base / "home", base / "data")
            paths.home.mkdir()
            manager = NeverProbe(paths, onboard.load_versions(), FakeRunner(), {})
            fingerprint = onboard.phase_fingerprint(
                "pi", "local", "auto", onboard.load_versions()
            )
            manager.store.save(
                {
                    "profile": "local",
                    "phases": {
                        "pi": {"status": "complete", "fingerprint": fingerprint}
                    },
                }
            )
            plan = manager.plan("local", "auto", repair=False, skip_audit=True)
            pi = next(item for item in plan if item.name == "pi")
            self.assertEqual(pi.action, "skip")


class PreflightTests(unittest.TestCase):
    def make_paths(self, base: Path) -> onboard.Paths:
        home = base / "home"
        home.mkdir()
        return onboard.Paths(ROOT, home, base / "data")

    def test_missing_prerequisites_are_reported_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_paths(Path(temporary))
            runner = FakeRunner()
            runner.results[("node", "-p", 'Number(process.versions.node.split(".")[0])')] = onboard.CommandResult(0, "18\n")
            runner.results[("launchctl", "print", f"gui/{os.getuid()}")] = onboard.CommandResult(1)
            runner.results[("npm", "config", "get", "prefix")] = onboard.CommandResult(127)
            report = onboard.Preflight(
                paths, onboard.load_versions(), runner, {"HOME": str(paths.home)}, "source"
            ).run("local")
            failed = {check.name for check in report.checks if check.status == "fail"}
            self.assertTrue({"commands", "node", "runtime", "launchd", "npm-prefix"} <= failed)
            self.assertGreaterEqual(len(failed), 5)

    def test_unmanaged_port_conflict_blocks_before_mutation(self) -> None:
        commands = {
            "bash", "curl", "git", "jq", "lsof", "node", "npm", "plutil",
            "python3", "rg", "rsync", "shasum", "tar", "cmake", "ninja",
        }
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_paths(Path(temporary))
            runtime = paths.llama_server
            runtime.parent.mkdir(parents=True)
            runtime.write_text("server", encoding="utf-8")
            runtime.chmod(0o755)
            runner = FakeRunner(commands)
            runner.results[("launchctl", "print", f"gui/{os.getuid()}")] = onboard.CommandResult(0)
            port_command = ("lsof", "-nP", "-iTCP:8080", "-sTCP:LISTEN")
            runner.results[port_command] = onboard.CommandResult(
                0, "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\nother 1 me 3u IPv4 x 0t0 TCP *:8080 (LISTEN)\n"
            )
            report = onboard.Preflight(
                paths, onboard.load_versions(), runner, {"HOME": str(paths.home)}, "auto"
            ).run("local")
            port = next(check for check in report.checks if check.name == "port")
            self.assertEqual(port.status, "fail")
            self.assertIn("unmanaged", port.summary)

    def test_auto_uses_cloud_when_local_runtime_is_impossible_and_aws_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.make_paths(Path(temporary))
            aws = paths.home / ".aws"
            aws.mkdir()
            (aws / "config").write_text("[profile demo]\nregion=us-east-1\n")
            preflight = onboard.Preflight(
                paths,
                onboard.load_versions(),
                FakeRunner(),
                {"HOME": str(paths.home), "AWS_PROFILE": "demo"},
                "source",
            )
            self.assertEqual(preflight.resolve_profile("auto", "Darwin", "arm64"), "cloud")


class RedactionAndDiagnosticsTests(unittest.TestCase):
    def test_redactor_removes_known_and_structured_secrets(self) -> None:
        redact = onboard.Redactor(["known-value"])
        access_key = "AKIA" + "1234567890ABCDEF"
        value = redact(
            "known-value AWS_SECRET_ACCESS_KEY=redact-me "
            f"Authorization: Bearer bearer-example {access_key}"
        )
        self.assertNotIn("known-value", value)
        self.assertNotIn("redact-me", value)
        self.assertNotIn("bearer-example", value)
        self.assertNotIn(access_key, value)

    def test_diagnostics_with_space_path_excludes_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "home with spaces"
            data = base / "data with spaces"
            home.mkdir()
            key = data / "secrets" / "bonsai-api-key"
            key.parent.mkdir(parents=True)
            key.write_text("redact-me\n", encoding="utf-8")
            state = data / "install-state.json"
            state.write_text('{"profile":"local","token":"redact-me"}\n')
            paths = onboard.Paths(ROOT, home, data)
            output = base / "bundle with spaces.tar.gz"
            runner = FakeRunner()
            runner.results[(str(ROOT / "bin" / "agent-stack"), "status")] = onboard.CommandResult(
                0, "API_KEY=redact-me\n"
            )
            created = onboard.create_diagnostics(
                paths,
                onboard.load_versions(),
                runner,
                {"HOME": str(home)},
                output=output,
            )
            self.assertEqual(created, output.resolve())
            with tarfile.open(created, "r:gz") as archive:
                payload = b"".join(
                    archive.extractfile(member).read()
                    for member in archive.getmembers()
                    if member.isfile()
                )
            self.assertNotIn(b"redact-me", payload)
            self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
