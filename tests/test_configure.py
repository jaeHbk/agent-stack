from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "configure.py"
SPEC = importlib.util.spec_from_file_location("stack_configure", SCRIPT)
assert SPEC and SPEC.loader
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


class PiMergeTests(unittest.TestCase):
    def test_merge_preserves_settings_and_removes_legacy_package(self) -> None:
        stack_root = Path("/tmp/portable-stack")
        source = {
            "defaultProvider": "amazon-bedrock",
            "defaultModel": "opus",
            "enabledModels": ["amazon-bedrock/opus"],
            "packages": [
                "npm:other-package@1.0.0",
                "git:github.com/davis7dotsh/my-pi-setup@old",
            ],
        }
        merged = configure.merge_pi_settings(source, stack_root)

        self.assertEqual(merged["defaultProvider"], "amazon-bedrock")
        self.assertEqual(merged["defaultModel"], "opus")
        self.assertIn("amazon-bedrock/opus", merged["enabledModels"])
        self.assertIn("bonsai/bonsai-27b", merged["enabledModels"])
        self.assertIn("npm:other-package@1.0.0", merged["packages"])

        self.assertFalse(
            any(
                "davis7dotsh/my-pi-setup" in configure.package_source(item)
                for item in merged["packages"]
            )
        )
        self.assertEqual(
            merged["packages"].count(str(stack_root / "pi-package")), 1
        )

    def test_merge_is_semantically_idempotent(self) -> None:
        root = Path("/tmp/stack")
        once = configure.merge_pi_settings({}, root)
        twice = configure.merge_pi_settings(once, root)
        self.assertEqual(once, twice)

    def test_model_merge_preserves_unrelated_provider(self) -> None:
        source = {"providers": {"existing": {"baseUrl": "https://example.com"}}}
        key_file = Path("/tmp/portable-stack/secrets/bonsai-api-key")
        merged = configure.merge_pi_models(source, key_file)
        self.assertIn("existing", merged["providers"])
        bonsai = merged["providers"]["bonsai"]
        self.assertEqual(bonsai["baseUrl"], "http://127.0.0.1:8080/v1")
        self.assertEqual(bonsai["models"][0]["contextWindow"], 65536)
        self.assertFalse(bonsai["compat"]["supportsDeveloperRole"])
        self.assertEqual(bonsai["apiKey"], f"!cat {key_file.resolve()}")
        self.assertTrue(bonsai["authHeader"])

    def test_cloud_profile_removes_only_local_model_entries(self) -> None:
        root = Path("/tmp/stack")
        settings = configure.merge_pi_settings(
            {"enabledModels": ["keep/model", "bonsai/bonsai-27b"]},
            root,
            include_local_model=False,
        )
        self.assertEqual(settings["enabledModels"], ["keep/model"])
        models = configure.merge_pi_models(
            {"providers": {"keep": {"baseUrl": "https://example.com"}, "bonsai": {"old": True}}},
            include_local_model=False,
        )
        self.assertEqual(models["providers"], {"keep": {"baseUrl": "https://example.com"}})

    def test_remove_only_stack_owned_entries(self) -> None:
        root = Path("/tmp/stack")
        installed = configure.merge_pi_settings(
            {"packages": ["npm:keep"], "enabledModels": ["keep/model"]},
            root,
        )
        removed = configure.remove_pi_settings(installed, root)
        self.assertEqual(removed["packages"], ["npm:keep"])
        self.assertEqual(removed["enabledModels"], ["keep/model"])


class ConfigureCliTests(unittest.TestCase):
    def test_cli_writes_private_json_without_touching_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            agent = home / ".pi" / "agent"
            stack_root = Path(temporary) / "stack"
            (stack_root / "pi-package").mkdir(parents=True)
            agent.mkdir(parents=True)
            (agent / "settings.json").write_text(
                json.dumps({"theme": "dark"}), encoding="utf-8"
            )
            (agent / "auth.json").write_text("secret", encoding="utf-8")

            command = [
                sys.executable,
                str(SCRIPT),
                "pi",
                "--home",
                str(home),
                "--stack-root",
                str(stack_root),
            ]
            subprocess.run(command, check=True)
            first_mtime = (agent / "settings.json").stat().st_mtime_ns
            subprocess.run(command, check=True)

            self.assertEqual((agent / "auth.json").read_text(), "secret")
            self.assertEqual(first_mtime, (agent / "settings.json").stat().st_mtime_ns)
            self.assertEqual(
                stat.S_IMODE((agent / "models.json").stat().st_mode), 0o600
            )
            subprocess.run([*command, "--check"], check=True)

    def test_launch_agent_renderer_escapes_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output = base / "rendered.plist"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "launch-agent",
                    "--template",
                    str(ROOT / "config" / "bonsai.launchagent.plist.in"),
                    "--output",
                    str(output),
                    "--label",
                    "com.test.stack",
                    "--server",
                    str(base / "runtime & tools" / "server"),
                    "--model",
                    str(base / "models" / "model.gguf"),
                    "--model-id",
                    "bonsai-test",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8080",
                    "--context",
                    "65536",
                    "--api-key-file",
                    str(base / "secrets" / "bonsai-api-key"),
                    "--working-dir",
                    str(base),
                    "--log-dir",
                    str(base / "logs"),
                ],
                check=True,
            )
            tree = ET.parse(output)
            strings = [element.text for element in tree.iter("string")]
            self.assertIn(str(base / "runtime & tools" / "server"), strings)
            self.assertIn("--no-ui", strings)
            self.assertIn("--api-key-file", strings)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o644)

    def test_dotenv_update_preserves_unrelated_values_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            path = base / ".env"
            value_file = base / "key"
            path.write_text(
                "KEEP=value\nHERMES_CUSTOM_BONSAI_API_KEY=old\n",
                encoding="utf-8",
            )
            value_file.write_text("new-secret\n", encoding="utf-8")
            command = [
                sys.executable,
                str(SCRIPT),
                "dotenv",
                "--path",
                str(path),
                "--key",
                "HERMES_CUSTOM_BONSAI_API_KEY",
                "--value-file",
                str(value_file),
            ]
            subprocess.run(command, check=True)
            first = path.read_text(encoding="utf-8")
            subprocess.run(command, check=True)
            self.assertEqual(path.read_text(encoding="utf-8"), first)
            self.assertEqual(
                first,
                "KEEP=value\nHERMES_CUSTOM_BONSAI_API_KEY=new-secret\n",
            )
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
