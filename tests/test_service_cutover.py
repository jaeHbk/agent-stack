from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import service_cutover  # noqa: E402


class LaunchdFake:
    def __init__(self, loaded: set[str]) -> None:
        self.loaded = set(loaded)

    def run(self, arguments) -> int:
        if arguments[1] == "print":
            label = arguments[2].split("/", 2)[-1]
            return 0 if label in self.loaded else 1
        if arguments[1] == "bootout":
            label = arguments[2].split("/", 2)[-1]
            self.loaded.discard(label)
            return 0
        if arguments[1] == "bootstrap":
            label = Path(arguments[3]).stem
            self.loaded.add(label)
            return 0
        return 1


class TestCutover(service_cutover.Cutover):
    health_result = True

    def wait_healthy(self) -> bool:
        return self.health_result


class ServiceCutoverTests(unittest.TestCase):
    def fixture(self, base: Path, health: bool = True):
        launch_agents = base / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True)
        bonsai_label = "com.local.agent-stack.bonsai"
        bonsai = launch_agents / f"{bonsai_label}.plist"
        bonsai.write_text("old-bonsai", encoding="utf-8")
        for label in service_cutover.METAL_LABELS:
            (launch_agents / f"{label}.plist").write_text(label, encoding="utf-8")
        staged = base / "staged.plist"
        staged.write_text("new-bonsai", encoding="utf-8")
        key = base / "key"
        key.write_text("secret", encoding="utf-8")
        runner = LaunchdFake({bonsai_label, *service_cutover.METAL_LABELS})
        cutover = TestCutover(
            domain="gui/501",
            bonsai_label=bonsai_label,
            bonsai_plist=bonsai,
            launch_agents=launch_agents,
            disabled_dir=base / "disabled",
            transaction_dir=base / "transactions" / "cutover",
            health_url="http://127.0.0.1:8080/v1/models",
            api_key_file=key,
            runner=runner,
            health_timeout=1,
        )
        cutover.health_result = health
        return cutover, runner, staged, bonsai, launch_agents

    def test_success_disables_metal_only_after_health(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cutover, runner, staged, bonsai, launch_agents = self.fixture(base)
            cutover.activate(staged)
            self.assertEqual(bonsai.read_text(), "new-bonsai")
            self.assertIn(cutover.bonsai_label, runner.loaded)
            self.assertTrue(set(service_cutover.METAL_LABELS).isdisjoint(runner.loaded))
            for label in service_cutover.METAL_LABELS:
                self.assertFalse((launch_agents / f"{label}.plist").exists())
                self.assertTrue((base / "disabled" / f"{label}.plist").exists())
            self.assertFalse(cutover.transaction_dir.exists())

    def test_health_failure_restores_bonsai_and_metal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cutover, runner, staged, bonsai, launch_agents = self.fixture(base, health=False)
            with self.assertRaisesRegex(service_cutover.CutoverError, "healthy"):
                cutover.activate(staged)
            self.assertEqual(bonsai.read_text(), "old-bonsai")
            self.assertIn(cutover.bonsai_label, runner.loaded)
            for label in service_cutover.METAL_LABELS:
                self.assertIn(label, runner.loaded)
                self.assertTrue((launch_agents / f"{label}.plist").exists())
            self.assertFalse(cutover.transaction_dir.exists())

    def test_cloud_deactivation_stops_both_stacks_and_preserves_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cutover, runner, _staged, bonsai, launch_agents = self.fixture(base)
            cutover.deactivate()
            self.assertTrue(bonsai.exists())
            self.assertNotIn(cutover.bonsai_label, runner.loaded)
            self.assertTrue(set(service_cutover.METAL_LABELS).isdisjoint(runner.loaded))
            for label in service_cutover.METAL_LABELS:
                self.assertFalse((launch_agents / f"{label}.plist").exists())
                self.assertTrue((base / "disabled" / f"{label}.plist").exists())
            self.assertFalse(cutover.transaction_dir.exists())

    def test_interrupted_transaction_is_recovered_on_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cutover, runner, _staged, bonsai, launch_agents = self.fixture(base)
            cutover._begin()
            for label in service_cutover.METAL_LABELS:
                cutover.stop(label)
                (launch_agents / f"{label}.plist").unlink()
            bonsai.write_text("partial-new", encoding="utf-8")
            self.assertTrue(cutover.recover())
            self.assertEqual(bonsai.read_text(), "old-bonsai")
            for label in service_cutover.METAL_LABELS:
                self.assertIn(label, runner.loaded)
                self.assertTrue((launch_agents / f"{label}.plist").exists())


if __name__ == "__main__":
    unittest.main()
