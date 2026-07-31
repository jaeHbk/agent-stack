#!/usr/bin/env python3
"""Create and install checksum-pinned, deterministic llama.cpp runtimes."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
ARCHIVE_ROOT = PurePosixPath("llama-runtime")


class ArtifactError(ValueError):
    """Raised when a runtime artifact violates its interface."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_platform() -> str:
    return f"{platform.system().lower()}-{platform.machine().lower()}"


def _entry(path: Path, relative: PurePosixPath) -> dict[str, Any]:
    info = path.lstat()
    item: dict[str, Any] = {
        "path": relative.as_posix(),
        "mode": stat.S_IMODE(info.st_mode),
    }
    if path.is_symlink():
        item.update({"type": "symlink", "target": os.readlink(path)})
    elif path.is_file():
        item.update(
            {
                "type": "file",
                "size": info.st_size,
                "sha256": sha256_file(path),
            }
        )
    else:
        raise ArtifactError(f"unsupported runtime entry: {path}")
    return item


def build_manifest(source: Path, commit: str, target_platform: str) -> dict[str, Any]:
    bin_dir = source / "build" / "bin"
    if not (bin_dir / "llama-server").is_file():
        raise ArtifactError(f"missing llama-server under {bin_dir}")
    if not (bin_dir / "llama-cli").is_file():
        raise ArtifactError(f"missing llama-cli under {bin_dir}")
    entries = [
        _entry(path, PurePosixPath("build", "bin", path.name))
        for path in sorted(bin_dir.iterdir(), key=lambda item: item.name)
        if path.is_file() or path.is_symlink()
    ]
    external_dependencies: set[str] = set()
    if shutil.which("otool"):
        for executable in (bin_dir / "llama-server", bin_dir / "llama-cli"):
            result = subprocess.run(
                ["otool", "-L", str(executable)], capture_output=True, text=True
            )
            if result.returncode == 0:
                for raw in result.stdout.splitlines()[1:]:
                    dependency = raw.strip().split(" ", 1)[0]
                    if dependency.startswith("/") and not dependency.startswith(
                        ("/System/", "/usr/lib/")
                    ):
                        external_dependencies.add(dependency)
    return {
        "schema_version": SCHEMA_VERSION,
        "llama_commit": commit,
        "platform": target_platform,
        "external_dependencies": sorted(external_dependencies),
        "entries": entries,
    }


def _tar_info(name: PurePosixPath, mode: int, size: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name.as_posix())
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "wheel"
    info.mtime = 0
    info.mode = mode
    info.size = size
    return info


def package_runtime(source: Path, output: Path, commit: str, target_platform: str) -> str:
    source = source.resolve()
    output = output.expanduser().resolve()
    manifest = build_manifest(source, commit, target_platform)
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.partial")
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
                    for directory in (
                        ARCHIVE_ROOT,
                        ARCHIVE_ROOT / "build",
                        ARCHIVE_ROOT / "build" / "bin",
                    ):
                        info = _tar_info(directory, 0o755)
                        info.type = tarfile.DIRTYPE
                        archive.addfile(info)
                    manifest_info = _tar_info(
                        ARCHIVE_ROOT / "runtime-manifest.json", 0o644, len(manifest_bytes)
                    )
                    import io

                    archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
                    for item in manifest["entries"]:
                        relative = PurePosixPath(item["path"])
                        source_path = source / Path(relative.as_posix())
                        info = _tar_info(ARCHIVE_ROOT / relative, int(item["mode"]))
                        if item["type"] == "symlink":
                            info.type = tarfile.SYMTYPE
                            info.linkname = str(item["target"])
                            archive.addfile(info)
                        else:
                            info.size = int(item["size"])
                            with source_path.open("rb") as handle:
                                archive.addfile(info, handle)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return sha256_file(output)


def _safe_member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ArtifactError(f"unsafe archive path: {name}")
    if not path.parts or path.parts[0] != ARCHIVE_ROOT.name:
        raise ArtifactError(f"archive entry is outside {ARCHIVE_ROOT}: {name}")
    return path


def safe_extract(archive_path: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            relative = _safe_member_path(member.name)
            target = root / Path(relative.as_posix())
            if member.isdev() or member.isfifo() or member.islnk():
                raise ArtifactError(f"unsupported archive entry: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                os.chmod(target, member.mode)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.issym():
                link = PurePosixPath(member.linkname)
                if link.is_absolute() or ".." in link.parts:
                    raise ArtifactError(f"unsafe symlink target: {member.name}")
                target.unlink(missing_ok=True)
                target.symlink_to(member.linkname)
                continue
            if not member.isfile():
                raise ArtifactError(f"unsupported archive entry: {member.name}")
            source = archive.extractfile(member)
            if source is None:
                raise ArtifactError(f"could not read archive entry: {member.name}")
            temporary = target.with_name(f".{target.name}.partial")
            with temporary.open("wb") as output:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, member.mode)
            os.replace(temporary, target)
    return root / ARCHIVE_ROOT.name


def load_manifest(runtime: Path) -> dict[str, Any]:
    manifest_path = runtime / "runtime-manifest.json"
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ArtifactError(f"invalid runtime manifest: {error}") from error
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ArtifactError("unsupported runtime manifest schema")
    if not isinstance(value.get("entries"), list):
        raise ArtifactError("runtime manifest has no entries")
    return value


def verify_runtime(
    runtime: Path,
    expected_commit: str,
    expected_platform: str,
    signature_policy: str = "checksum",
) -> dict[str, Any]:
    runtime = runtime.resolve()
    manifest = load_manifest(runtime)
    if manifest.get("llama_commit") != expected_commit:
        raise ArtifactError("runtime commit does not match the immutable pin")
    if manifest.get("platform") != expected_platform:
        raise ArtifactError("runtime platform does not match this host")
    for item in manifest["entries"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ArtifactError("malformed runtime manifest entry")
        relative = _safe_member_path(f"{ARCHIVE_ROOT}/{item['path']}")
        path = runtime / Path(*relative.parts[1:])
        if item.get("type") == "symlink":
            if not path.is_symlink() or os.readlink(path) != item.get("target"):
                raise ArtifactError(f"runtime symlink mismatch: {item['path']}")
        elif item.get("type") == "file":
            if not path.is_file() or path.stat().st_size != item.get("size"):
                raise ArtifactError(f"runtime file mismatch: {item['path']}")
            if sha256_file(path) != item.get("sha256"):
                raise ArtifactError(f"runtime checksum mismatch: {item['path']}")
        else:
            raise ArtifactError(f"unknown runtime entry type: {item.get('type')}")
    expected_entries = {item["path"] for item in manifest["entries"]}
    actual_entries = {
        path.relative_to(runtime).as_posix()
        for path in runtime.rglob("*")
        if (path.is_file() or path.is_symlink())
        and path.name != "runtime-manifest.json"
    }
    if actual_entries != expected_entries:
        raise ArtifactError("runtime contains files not covered by its manifest")
    for dependency in manifest.get("external_dependencies", []):
        if not isinstance(dependency, str) or not dependency.startswith("/"):
            raise ArtifactError("runtime manifest has an invalid external dependency")
        if not Path(dependency).is_file():
            raise ArtifactError(f"external runtime dependency is missing: {dependency}")
    for required in ("llama-server", "llama-cli"):
        path = runtime / "build" / "bin" / required
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ArtifactError(f"runtime executable is missing: {required}")
    if signature_policy == "require-codesign":
        for path in (runtime / "build" / "bin").iterdir():
            if path.is_file() and (os.access(path, os.X_OK) or path.suffix == ".dylib"):
                result = subprocess.run(
                    ["codesign", "--verify", "--strict", str(path)],
                    capture_output=True,
                    text=True,
                )
                if result.returncode:
                    raise ArtifactError(f"code-signature verification failed: {path.name}")
    elif signature_policy != "checksum":
        raise ArtifactError(f"unknown signature policy: {signature_policy}")
    return manifest


def install_archive(
    archive: Path,
    destination: Path,
    expected_sha256: str,
    expected_commit: str,
    expected_platform: str,
    signature_policy: str,
) -> Path:
    actual = sha256_file(archive)
    if not expected_sha256 or actual != expected_sha256:
        raise ArtifactError("runtime archive checksum mismatch")
    destination = Path(os.path.abspath(destination.expanduser()))
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".llama-runtime-", dir=destination.parent))
    try:
        extracted = safe_extract(archive, stage)
        verify_runtime(extracted, expected_commit, expected_platform, signature_policy)
        previous: Path | None = None
        if destination.exists() or destination.is_symlink():
            from datetime import datetime, timezone

            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            previous = destination.with_name(f"{destination.name}.previous-{stamp}")
            suffix = 1
            while previous.exists() or previous.is_symlink():
                previous = destination.with_name(
                    f"{destination.name}.previous-{stamp}-{suffix}"
                )
                suffix += 1
            os.replace(destination, previous)
        try:
            os.replace(extracted, destination)
        except BaseException:
            if previous is not None and previous.exists() and not destination.exists():
                os.replace(previous, destination)
            raise
        return destination
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)
    package = commands.add_parser("package")
    package.add_argument("--source", type=Path, required=True)
    package.add_argument("--output", type=Path, required=True)
    package.add_argument("--commit", required=True)
    package.add_argument("--platform", default=current_platform())
    verify = commands.add_parser("verify")
    verify.add_argument("--runtime", type=Path, required=True)
    verify.add_argument("--commit", required=True)
    verify.add_argument("--platform", default=current_platform())
    verify.add_argument(
        "--signature-policy", choices=("checksum", "require-codesign"), default="checksum"
    )
    extract = commands.add_parser("extract")
    extract.add_argument("--archive", type=Path, required=True)
    extract.add_argument("--destination", type=Path, required=True)
    extract.add_argument("--sha256", required=True)
    extract.add_argument("--commit", required=True)
    extract.add_argument("--platform", default=current_platform())
    extract.add_argument(
        "--signature-policy", choices=("checksum", "require-codesign"), default="checksum"
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "package":
            digest = package_runtime(args.source, args.output, args.commit, args.platform)
            print(f"{digest}  {args.output}")
        elif args.command == "verify":
            verify_runtime(args.runtime, args.commit, args.platform, args.signature_policy)
            print("runtime artifact verified")
        else:
            install_archive(
                args.archive,
                args.destination,
                args.sha256,
                args.commit,
                args.platform,
                args.signature_policy,
            )
            print(f"runtime installed at {args.destination}")
    except (ArtifactError, OSError, tarfile.TarError) as error:
        print(f"runtime_artifact.py: {error}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
