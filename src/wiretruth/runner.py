"""Run one shim process and replay server per cell, then grade their final observations."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, get_args

from wiretruth.checks import Status
from wiretruth.envs import RunError as RunError
from wiretruth.envs import build_javascript_env, resolve_interpreter
from wiretruth.formats import (
    CallSpec,
    Fixture,
    Observation,
    Scenario,
    ShimManifest,
    Tier,
    call_spec_document,
    load_fixture,
    load_shim_manifest,
    observation_from_document,
    parse_json,
    read_json,
)
from wiretruth.grading import CellGrade, Grade, ShimRun, grade_cell, is_model_lookup
from wiretruth.providers import PROVIDERS
from wiretruth.recorder import find_scenarios
from wiretruth.replay import BASE_PATHS, HOST, LoggedRequest, ReplayServer

DEFAULT_JOBS = min(4, os.cpu_count() or 1)
LOCAL_ERRORS = ("validation", "internal")
INHERITED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "SYSTEMROOT")
GLYPHS: Mapping[Grade, str] = {
    "PASS": "✓",
    "LOSSY": "~",
    "WRONG": "✗",
    "CRASH": "!",
    "HANG": "⧗",
    "UNSUPPORTED": "\u2013",
    "HARNESS": "?",
}


@dataclass(frozen=True)
class Cell:
    scenario: Scenario
    provider: str
    fixture: Fixture

    @property
    def call_spec(self) -> CallSpec:
        model = self.fixture.provenance.requested_model
        if model is None:
            model = PROVIDERS[self.provider].DEFAULT_MODEL
        return CallSpec(
            self.scenario.id, self.provider, model, self.scenario.call_for(self.provider)
        )


@dataclass(frozen=True)
class CellResult:
    scenario: str
    provider: str
    fixture: str
    fixture_tier: Tier
    grade: CellGrade
    observation: Observation | None
    request_count: int
    notes: tuple[str, ...]


@dataclass(frozen=True)
class RunReport:
    cells: tuple[CellResult, ...]
    stderr_dir: Path


def find_cells(root: Path, pattern: str = "*", provider: str | None = None) -> tuple[Cell, ...]:
    scenarios, _ = find_scenarios(root, pattern, provider)
    cells = []
    for scenario in scenarios:
        for candidate in scenario.providers:
            if provider is not None and candidate != provider:
                continue
            path = root / "suite" / "fixtures" / candidate / f"{scenario.id}.json"
            if not path.is_file():
                continue
            if candidate not in PROVIDERS:
                raise RunError(f"no provider module for {candidate}")
            fixture = load_fixture(path)
            if fixture.id != f"{candidate}/{scenario.id}":
                raise RunError(f"{path}: expected fixture {candidate}/{scenario.id}")
            cells.append(Cell(scenario, candidate, fixture))
    if not cells:
        raise RunError(f"no cell matches {pattern!r} with an undated fixture")
    return tuple(sorted(cells, key=lambda cell: (cell.scenario.id, cell.provider)))


def build_env(
    provider: str,
    server_url: str,
    closed_port: int,
    manifest_env: Mapping[str, str],
    inherited: Mapping[str, str],
) -> dict[str, str]:
    """The complete shim environment; unrelated provider and SDK settings are excluded."""
    env = {name: inherited[name] for name in INHERITED_ENV if name in inherited}
    for candidate, path in BASE_PATHS.items():
        base = server_url if candidate == provider else f"http://{HOST}:{closed_port}"
        env[f"WIRETRUTH_BASE_URL_{candidate.upper().replace('-', '_')}"] = base + path
    env.update(
        WIRETRUTH_API_KEY="wt-test-key",
        WIRETRUTH_PROVIDER=provider,
        NO_PROXY="127.0.0.1,localhost",
        no_proxy="127.0.0.1,localhost",
    )
    env.update(manifest_env)
    return env


@contextlib.contextmanager
def closed_port() -> Iterator[int]:
    """Reserve a TCP port without listening, so other providers get connection refused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind((HOST, 0))
        yield reserved.getsockname()[1]


def process_to_run(
    *,
    timed_out: bool,
    returncode: int,
    stdout: bytes,
    request_log: Sequence[LoggedRequest],
    stream: bool,
    provider: str,
) -> ShimRun:
    """Interpret process output and a final request log without any process or socket access."""
    observation = None
    invalid = None
    if returncode == 0 and stdout:
        try:
            observation = observation_from_document(parse_json(stdout))
        except (ValueError, RecursionError) as error:
            invalid = (str(error).splitlines() or [type(error).__name__])[0][:300]
    mismatches = []
    for index, request in enumerate(request_log):
        if is_model_lookup(provider, request, request_log):
            continue
        if request.exchange is None:
            mismatches.append(f"request {index}: no fixture for {request.method} {request.path}")
        requested = PROVIDERS[provider].requested_stream(request.path, request.json_body)
        if requested != stream:
            mismatches.append(
                f"request {index}: {request.path} requested stream={requested}, expected {stream}"
            )
    if observation is not None and not request_log:
        # A library can refuse a call itself or crash while building it. Any other result or error
        # without a request came from somewhere other than the replay server.
        category = observation.error.category if observation.error else None
        if observation.outcome == "returned":
            mismatches.append("returned a result without sending a request")
        elif observation.outcome == "raised" and category not in LOCAL_ERRORS:
            mismatches.append(f"raised an error ({category}) without sending a request")
    return ShimRun(
        timed_out=timed_out,
        signal=-returncode if returncode < 0 else None,
        observation=observation,
        invalid=invalid,
        mismatch="; ".join(mismatches) or None,
        request_log=tuple(request_log),
    )


def run_cell(
    cell: Cell,
    *,
    shim_dir: Path,
    manifest: ShimManifest,
    stderr_dir: Path,
    reserved_port: int,
    timeout: float = 20,
    interpreter: str | None = None,
) -> CellResult:
    argv = list(manifest.entry)
    if manifest.language == "python" and argv[0] == "python":
        argv[0] = interpreter or sys.executable
    spec = cell.call_spec
    stdin = json.dumps(call_spec_document(spec), ensure_ascii=False).encode("utf-8")
    timed_out = False
    with ReplayServer(cell.fixture) as server:
        env = build_env(cell.provider, server.url, reserved_port, manifest.env, os.environ)
        with subprocess.Popen(
            argv,
            cwd=shim_dir,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        ) as process:
            try:
                stdout, stderr = process.communicate(stdin, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                with contextlib.suppress(ProcessLookupError):
                    if hasattr(os, "killpg"):
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                stdout, stderr = process.communicate()
    (stderr_dir / f"{cell.provider}.{cell.scenario.id}.stderr").write_bytes(stderr)
    run = process_to_run(
        timed_out=timed_out,
        returncode=process.returncode,
        stdout=stdout,
        request_log=server.request_log,
        stream=spec.call.stream,
        provider=cell.provider,
    )
    grade = grade_cell(
        run,
        scenario=cell.scenario,
        provider=cell.provider,
        truth=cell.fixture.truth,
        unsupported=manifest.unsupported,
    )
    notes = grade.notes
    if not timed_out and process.returncode > 0:
        notes += (f"the shim exited with code {process.returncode}",)
    return CellResult(
        cell.scenario.id,
        cell.provider,
        cell.fixture.id,
        cell.fixture.tier,
        grade,
        run.observation,
        len(run.request_log),
        notes,
    )


def run_suite(
    root: Path,
    shim_dir: Path,
    *,
    pattern: str = "*",
    provider: str | None = None,
    jobs: int = DEFAULT_JOBS,
    timeout: float = 20,
) -> RunReport:
    if jobs < 1:
        raise RunError("jobs must be at least 1")
    shim_dir = shim_dir.resolve()
    manifest = load_shim_manifest(shim_dir / "wiretruth-shim.json")
    cells = find_cells(root, pattern, provider)
    if manifest.language == "javascript":
        build_javascript_env(shim_dir)
    interpreter = resolve_interpreter(root, manifest)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    stderr_dir = root / ".wiretruth" / "runs" / stamp
    stderr_dir.mkdir(parents=True)
    with closed_port() as reserved_port, ThreadPoolExecutor(max_workers=jobs) as executor:
        results = executor.map(
            lambda cell: run_cell(
                cell,
                shim_dir=shim_dir,
                manifest=manifest,
                stderr_dir=stderr_dir,
                reserved_port=reserved_port,
                timeout=timeout,
                interpreter=interpreter,
            ),
            cells,
        )
        return RunReport(tuple(results), stderr_dir)


@dataclass(frozen=True)
class ExpectedGrade:
    grade: Grade
    requirements: Mapping[str, Status]


def load_expected_grades(path: Path) -> dict[tuple[str, str], ExpectedGrade]:
    doc = read_json(path)
    if not isinstance(doc, dict) or set(doc) != {"cells"} or not isinstance(doc["cells"], list):
        raise RunError(f"{path}: expected an object with a cells array")
    expected = {}
    for index, cell in enumerate(doc["cells"]):
        where = f"{path}: cells[{index}]"
        if not isinstance(cell, dict) or set(cell) != {
            "scenario",
            "provider",
            "grade",
            "requirements",
        }:
            raise RunError(f"{where}: expected scenario, provider, grade and requirements")
        scenario, provider = cell["scenario"], cell["provider"]
        if (
            not isinstance(scenario, str)
            or not scenario
            or not isinstance(provider, str)
            or not provider
        ):
            raise RunError(f"{where}: scenario and provider must be non-empty strings")
        if cell["grade"] not in get_args(Grade):
            raise RunError(f"{where}: unknown grade")
        requirements = cell["requirements"]
        if not isinstance(requirements, dict) or any(
            not name or value not in get_args(Status) for name, value in requirements.items()
        ):
            raise RunError(f"{where}: requirements must map ids to requirement statuses")
        key = (scenario, provider)
        if key in expected:
            raise RunError(f"{where}: duplicate cell {scenario} {provider}")
        expected[key] = ExpectedGrade(
            cast(Grade, cell["grade"]), cast("dict[str, Status]", requirements)
        )
    return expected


def compare_grades(
    cells: Sequence[CellResult], expected: Mapping[tuple[str, str], ExpectedGrade]
) -> tuple[str, ...]:
    """Every grade, status and membership difference, in cell and requirement id order."""
    actual = {(cell.scenario, cell.provider): cell.grade for cell in cells}
    differences = []
    for key in sorted(actual.keys() | expected.keys()):
        label = " ".join(key)
        if key not in actual:
            differences.append(f"{label}: missing from the run")
            continue
        if key not in expected:
            differences.append(f"{label}: not listed in expected grades")
            continue
        grade, wanted = actual[key], expected[key]
        if grade.grade != wanted.grade:
            differences.append(f"{label}: grade {grade.grade}, expected {wanted.grade}")
        statuses = {result.id: result.status for result in grade.requirements}
        for name in sorted(statuses.keys() | wanted.requirements.keys()):
            got = statuses.get(name, "<missing>")
            want = wanted.requirements.get(name, "<missing>")
            if got != want:
                differences.append(f"{label}: requirement {name}: {got}, expected {want}")
    return tuple(differences)
