from __future__ import annotations

import gzip
import io
import os
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import runtime_artifact  # noqa: E402


class RuntimeArtifactTests(unittest.TestCase):
    def source(self, base: Path) -> Path:
        source = base / "source with spaces"
        binary = source / "build" / "bin"
        binary.mkdir(parents=True)
        for name, content in (
            ("llama-server", b"server"),
            ("llama-cli", b"cli"),
            ("libllama.dylib", b"library"),
        ):
            path = binary / name
            path.write_bytes(content)
            path.chmod(0o755)
        (binary / "libllama.0.dylib").symlink_to("libllama.dylib")
        return source

    def test_package_is_deterministic_and_installs_from_space_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = self.source(base)
            first = base / "first artifact.tar.gz"
            second = base / "second artifact.tar.gz"
            one = runtime_artifact.package_runtime(source, first, "abc", "darwin-arm64")
            two = runtime_artifact.package_runtime(source, second, "abc", "darwin-arm64")
            self.assertEqual(one, two)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            destination = base / "installed runtime with spaces"
            runtime_artifact.install_archive(
                first, destination, one, "abc", "darwin-arm64", "checksum"
            )
            manifest = runtime_artifact.verify_runtime(
                destination, "abc", "darwin-arm64", "checksum"
            )
            self.assertEqual(manifest["llama_commit"], "abc")
            self.assertTrue(os.access(destination / "build/bin/llama-server", os.X_OK))

    def test_corrupt_archive_is_rejected_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = self.source(base)
            archive = base / "runtime.tar.gz"
            digest = runtime_artifact.package_runtime(source, archive, "abc", "darwin-arm64")
            archive.write_bytes(archive.read_bytes() + b"corrupt")
            with self.assertRaisesRegex(runtime_artifact.ArtifactError, "checksum"):
                runtime_artifact.install_archive(
                    archive, base / "target", digest, "abc", "darwin-arm64", "checksum"
                )
            self.assertFalse((base / "target").exists())

    def test_path_traversal_archive_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            archive = base / "malicious.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                info = tarfile.TarInfo("llama-runtime/../../escape")
                info.size = 3
                handle.addfile(info, io.BytesIO(b"bad"))
            with self.assertRaisesRegex(runtime_artifact.ArtifactError, "unsafe"):
                runtime_artifact.safe_extract(archive, base / "stage")
            self.assertFalse((base / "escape").exists())


if __name__ == "__main__":
    unittest.main()
