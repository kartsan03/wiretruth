"""Cached environments for pinned library shims."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from wiretruth.formats import ShimManifest, load_shim_manifest

MARKER = "wiretruth-install.json"
PYTHON = "3.12"


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


def _marker(manifest: ShimManifest) -> bytes:
    return json.dumps({"python": PYTHON, "packages": _pins(manifest)}).encode()


def _matches(path: Path, manifest: ShimManifest) -> bool:
    marker = path / MARKER
    return _python(path).is_file() and marker.is_file() and marker.read_bytes() == _marker(manifest)


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
        ["uv", "venv", "--python", PYTHON, str(path)],
        # "--": a package name from the manifest can never become a uv option.
        ["uv", "pip", "install", "--python", interpreter, "--", *_pins(manifest)],
    ):
        result = subprocess.run(command, stdout=sys.stderr, check=False)
        if result.returncode:
            raise RunError(f"{' '.join(command)} exited with code {result.returncode}")
    (path / MARKER).write_bytes(_marker(manifest))
    return interpreter


def parse_installed_packages(output: str) -> dict[str, str]:
    packages = {}
    for line in output.splitlines():
        name, separator, version = line.partition("==")
        if not separator or not name.strip() or not version.strip():
            raise RunError("uv pip freeze: expected name==version on every line")
        packages[name.strip()] = version.strip()
    return packages


def installed_packages(interpreter: str) -> dict[str, str]:
    command = ["uv", "pip", "freeze", "--python", interpreter]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise RunError(f"uv pip freeze: {error}") from error
    if result.returncode:
        raise RunError(f"{' '.join(command)} exited with code {result.returncode}")
    return parse_installed_packages(result.stdout)


def build_javascript_env(shim_dir: Path) -> Path:
    path = shim_dir.resolve() / "node_modules"
    marker = path / MARKER
    try:
        digest = hashlib.sha256((shim_dir / "package-lock.json").read_bytes()).hexdigest()
        if marker.is_file() and marker.read_bytes() == digest.encode():
            return path
        marker.unlink(missing_ok=True)
        command = ["npm", "ci", "--ignore-scripts", "--prefix", str(shim_dir.resolve())]
        result = subprocess.run(command, stdout=sys.stderr, check=False)
        if result.returncode:
            raise RunError(f"{' '.join(command)} exited with code {result.returncode}")
        path.mkdir(exist_ok=True)
        marker.write_text(digest)
    except OSError as error:
        raise RunError(f"JavaScript environment: {error}") from error
    return path


def installed_javascript_packages(shim_dir: Path) -> dict[str, str]:
    path = shim_dir / "node_modules" / ".package-lock.json"
    try:
        packages = json.loads(path.read_text())["packages"]
        if not isinstance(packages, dict):
            raise ValueError("expected a packages object")
        installed = {}
        for name, entry in packages.items():
            match = re.fullmatch(r"node_modules/((?:@[^/]+/)?[^/]+)", name)
            if match is not None:
                version = entry["version"]
                if not isinstance(version, str) or not version:
                    raise ValueError("expected a package version")
                installed[match[1]] = version
        return installed
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RunError(f"{path}: cannot read installed packages: {error}") from error


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
