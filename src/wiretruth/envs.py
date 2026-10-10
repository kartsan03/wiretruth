"""Cached Python environments for pinned library shims."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from wiretruth.formats import ShimManifest, load_shim_manifest

MARKER = "wiretruth-install.json"


class RunError(ValueError):
    """The run cannot start because its inputs or environment are unusable."""


def find_library(root: Path, name: str) -> Path:
    matches = [
        path.parent
        for path in (root / "shims").glob("*/*/wiretruth-shim.json")
        if path.parent.name == name
    ]
    if not matches:
        raise RunError(f"no library shim named {name!r} under {root / 'shims'}")
    if len(matches) != 1:
        raise RunError(f"multiple library shims named {name!r} under {root / 'shims'}")
    return matches[0]


def _env_dir(root: Path, manifest: ShimManifest) -> Path:
    version = manifest.packages[manifest.primary_package]
    return root.resolve() / ".wiretruth" / "envs" / "py" / f"{manifest.name}-{version}"


def _python(path: Path) -> Path:
    return path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _pins(manifest: ShimManifest) -> list[str]:
    return [f"{name}=={version}" for name, version in sorted(manifest.packages.items())]


def _matches(path: Path, manifest: ShimManifest) -> bool:
    marker = path / MARKER
    return (
        _python(path).is_file()
        and marker.is_file()
        and marker.read_bytes() == json.dumps(_pins(manifest)).encode()
    )


def resolve_interpreter(root: Path, manifest: ShimManifest) -> str | None:
    if manifest.language != "python":
        return None
    if manifest.primary_package == "wiretruth":
        return sys.executable
    path = _env_dir(root, manifest)
    interpreter = str(_python(path))
    if _matches(path, manifest):
        return interpreter
    if shutil.which("uv") is None:
        raise RunError("uv is not on PATH; install uv to build library environments")
    if path.exists():
        shutil.rmtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for command in (
        ["uv", "venv", "--python", "3.12", str(path)],
        ["uv", "pip", "install", "--python", interpreter, *_pins(manifest)],
    ):
        result = subprocess.run(command, stdout=sys.stderr, check=False)
        if result.returncode:
            raise RunError(f"{' '.join(command)} exited with code {result.returncode}")
    (path / MARKER).write_bytes(json.dumps(_pins(manifest)).encode())
    return interpreter


def list_envs(root: Path) -> tuple[tuple[Path, bool], ...]:
    """List cached directories and whether their installed pins match a current manifest."""
    manifests = [load_shim_manifest(path) for path in (root / "shims").rglob("wiretruth-shim.json")]
    current = {
        _env_dir(root, manifest)
        for manifest in manifests
        if manifest.language == "python"
        and manifest.primary_package != "wiretruth"
        and _matches(_env_dir(root, manifest), manifest)
    }
    parent = root.resolve() / ".wiretruth" / "envs" / "py"
    return tuple((path, path in current) for path in sorted(parent.glob("*")) if path.is_dir())


def prune_envs(root: Path) -> tuple[Path, ...]:
    stale = tuple(path for path, current in list_envs(root) if not current)
    for path in stale:
        shutil.rmtree(path)
    return stale
