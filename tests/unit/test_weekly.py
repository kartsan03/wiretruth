import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from wiretruth import weekly
from wiretruth.formats import ShimManifest
from wiretruth.results import SavedResults, write_results
from wiretruth.runner import ExpectedGrade
from wiretruth.weekly import (
    PinCheck,
    Update,
    changelog_bullet,
    grade_changes,
    insert_changelog,
    new_red_cells,
    parse_compile_output,
    pr_body,
    python_requirements,
    replace_pins,
    select_extras,
)


@pytest.fixture
def manifest():
    return ShimManifest(
        name="sample",
        language="python",
        packages={"sample-sdk": "1", "openai": "2", "google-genai": "2.1"},
        primary_package="sample-sdk",
        entry=(),
        env={},
        providers={},
        surfaces={},
        finish_map={},
        error_map=(),
        unsupported=(),
        config_notes=(),
        docs=(),
    )


def saved(grades, version="2"):
    return SavedResults(
        "sample", version, {}, {cell: ExpectedGrade(grade, {}) for cell, grade in grades.items()}
    )


@pytest.fixture
def update():
    before = saved({("finish.text", "provider"): "LOSSY"}, "1")
    after = saved({("finish.text", "provider"): "WRONG"})
    return Update(
        before,
        after,
        ("sample-sdk 1 → 2", "companion 3 → 4"),
        grade_changes(before, after, []),
        new_red_cells(after, []),
    )


@pytest.mark.parametrize("separator", [", ", ",\n    "])
def test_replace_pins_preserves_manifest_layout_and_notes(separator):
    text = (
        '{"packages": {"sample-sdk": "1"'
        + separator
        + '"companion": "2"}, "notes": ["sample-sdk 1", "https://example.com/v1"]}\n'
    )
    rewritten = replace_pins(
        text, {"sample-sdk": "1", "companion": "2"}, {"sample-sdk": "3", "companion": "4"}
    )
    assert rewritten == text.replace('"sample-sdk": "1"', '"sample-sdk": "3"').replace(
        '"companion": "2"', '"companion": "4"'
    )


@pytest.mark.parametrize("text", ['{"sample-sdk": "other"}', '{"sample-sdk": "1"}' * 2])
def test_replace_pins_requires_exactly_one_occurrence(text):
    with pytest.raises(ValueError, match="exactly one occurrence"):
        replace_pins(text, {"sample-sdk": "1"}, {"sample-sdk": "2"})


def test_parse_compile_output_selects_normalized_manifest_packages():
    assert parse_compile_output(
        "sample-sdk==2\ngoogle-genai==2.3\ntransitive==4\n",
        {"Sample_SDK": "1", "google-genai": "2.1"},
    ) == {"Sample_SDK": "2", "google-genai": "2.3"}


@pytest.mark.parametrize("output", ["sample-sdk>=2", "sample-sdk=="])
def test_parse_compile_output_rejects_unpinned_lines(output):
    with pytest.raises(ValueError, match="name==version"):
        parse_compile_output(output, {"sample-sdk": "1"})


def test_parse_compile_output_requires_every_manifest_package():
    with pytest.raises(KeyError):
        parse_compile_output("transitive==1\n", {"sample-sdk": "1"})


def test_select_extras_prefers_companion_name_then_first_and_deduplicates():
    assert select_extras(
        [
            'openai>=3; extra == "alternate"',
            'openai>=3; extra == "openai"',
            "google_genai<3; extra == 'google'",
            'google-genai<3; extra == "all"',
            'second>=1; extra == "google"',
        ],
        ["openai", "google-genai", "second", "absent"],
    ) == ("openai", "google")


@pytest.mark.parametrize("unconditional_first", [False, True])
def test_select_extras_ignores_extras_for_unconditional_companions(unconditional_first):
    requirements = ['openai>=3; extra == "openai"', 'openai>=3; python_version >= "3.12"']
    if unconditional_first:
        requirements.reverse()
    assert select_extras(requirements, ["openai"]) == ()


def test_python_requirements_apply_holds_and_selected_extras(manifest):
    assert python_requirements(
        manifest,
        "5",
        ['openai>=3; extra == "openai"', 'google-genai>=2; extra == "google"'],
    ) == ("sample-sdk[google,openai]==5", "google-genai<3", "openai")
    assert python_requirements(manifest, "5", []) == (
        "sample-sdk==5",
        "google-genai<3",
        "openai",
    )


def test_grade_changes_include_added_removed_and_upstream_issues():
    before = saved(
        {("finish.text", "provider"): "LOSSY", ("finish.removed", "provider"): "PASS"}, "1"
    )
    after = saved(
        {("finish.text", "provider"): "PASS", ("finish.added", "provider"): "UNSUPPORTED"}
    )
    upstream = [
        {
            "library": "sample",
            "scenarios": ["finish.text"],
            "providers": ["provider"],
            "issue": "https://example.com/issues/1",
        }
    ]
    assert grade_changes(before, after, upstream) == (
        "`finish.added` over `provider` absent → UNSUPPORTED",
        "`finish.removed` over `provider` PASS → absent",
        "`finish.text` over `provider` LOSSY → PASS ([upstream](https://example.com/issues/1))",
    )
    assert grade_changes(after, after, upstream) == ()


@pytest.mark.parametrize("grade", ["WRONG", "CRASH", "HANG"])
def test_new_red_cells_require_matching_library_scenario_and_provider(grade):
    after = saved({("finish.text", "provider"): grade})
    entry = {
        "library": "sample",
        "scenarios": ["finish.text", "finish.other"],
        "providers": ["provider", "other"],
        "issue": None,
        "status": "documented-behavior",
    }
    assert new_red_cells(after, [entry]) == ()
    for field, value in (
        ("library", "other"),
        ("scenarios", ["finish.other"]),
        ("providers", ["other"]),
    ):
        assert new_red_cells(after, [{**entry, field: value}]) == (
            f"`finish.text` over `provider`: {grade}",
        )


def test_new_red_cells_ignore_other_grades():
    assert (
        new_red_cells(
            saved(
                {
                    ("finish.pass", "provider"): "PASS",
                    ("finish.lossy", "provider"): "LOSSY",
                    ("finish.unsupported", "provider"): "UNSUPPORTED",
                    ("finish.harness", "provider"): "HARNESS",
                }
            ),
            [],
        )
        == ()
    )


def test_changelog_inserts_at_top_of_existing_results_and_preserves_other_sections():
    text = (
        "# Log\n\n## [Unreleased]\n\n### Added\n- keep\n\n### Results\n- old\n\n"
        "### Fixed\n- fix\n\n## [1]\n\n### Results\n- released\n"
    )
    assert insert_changelog(text, ["- new", "- another"]) == text.replace(
        "### Results\n", "### Results\n- new\n- another\n", 1
    )


@pytest.mark.parametrize("released", ["", "## [1]\n\n### Results\n- released\n"])
def test_changelog_creates_results_as_last_unreleased_subsection(released):
    unreleased = "# Log\n\n## [Unreleased]\n\n### Added\n- keep\n\n### Fixed\n- fix\n\n"
    assert insert_changelog(unreleased + released, ["- new"]) == (
        unreleased + "### Results\n- new\n\n" + released
    )


def test_changelog_requires_unreleased_section():
    with pytest.raises(ValueError, match="no Unreleased section"):
        insert_changelog("# Log\n", ["- new"])


def test_changelog_bullet_counts_grade_order_wrap_and_companion_only_update(update):
    after = saved(
        {
            ("finish.wrong", "provider"): "WRONG",
            ("finish.unsupported", "provider"): "UNSUPPORTED",
            ("finish.pass", "provider"): "PASS",
            ("finish.lossy", "provider"): "LOSSY",
        },
        "1",
    )
    bullet = changelog_bullet(replace(update, after=after, changes=()))
    assert "1 PASS, 1 LOSSY, 1 WRONG and 1 UNSUPPORTED of 4 cells" in bullet
    assert "No grade changes from 1." in " ".join(bullet.split())
    assert "([results/sample/1.json](results/sample/1.json))." in bullet
    assert "CRASH" not in bullet and "HANG" not in bullet and "HARNESS" not in bullet
    assert max(map(len, bullet.splitlines())) <= 116
    assert "Grade changes from 1:" in " ".join(changelog_bullet(update).split())


def test_pr_body_lists_pins_skip_drift_blocked_and_actions_run(update):
    body = pr_body(
        [update],
        ["skipped other 3: run exited 1: HARNESS cell"],
        {
            "sample": PinCheck(
                "differs",
                (
                    "finish.text provider: grade LOSSY, expected PASS",
                    "packages differ from results/sample/1.json: transitive 1 → 2",
                ),
            ),
            "other": PinCheck("error", ("cannot build environment",)),
        },
        [],
        "https://example.com/actions/runs/1",
    )
    for expected in (
        "sample: 1 → 2; sample-sdk 1 → 2; companion 3 → 4",
        "LOSSY → WRONG",
        "sample: `finish.text` over `provider`: WRONG",
        "labelled `blocked` until each cell has an entry in `results/upstream.json`",
        "skipped other 3: run exited 1: HARNESS cell",
        "sample: differs",
        "packages differ from results/sample/1.json: transitive 1 → 2",
        "other: error",
        "cannot build environment",
        "[Actions run](https://example.com/actions/runs/1)",
        "version-anchored documentation links",
        "`docs/validation.md`",
        "`docs/img/matrix.png`",
        "`results/upstream.json` statuses",
    ):
        assert expected in body
    assert "Closes" not in body


def test_pr_body_none_matches_and_up_to_date(update):
    update = replace(update, after=update.before, changes=(), red=(), pins=("companion 3 → 4",))
    body = pr_body([update], [], {"sample": PinCheck("matches")}, ["other"], None)
    assert "sample: 1 → 1; companion 3 → 4" in body
    assert "- other: up to date" in body
    assert "## Grade changes\n\n- sample: none" in body
    assert "## New WRONG / CRASH / HANG cells without an upstream report\n\nnone" in body
    assert "## Skipped releases\n\nnone" in body
    assert "- sample: matches" in body
    assert "blocked" not in body and "Actions run" not in body
    assert "## Versions\n\nnone" in pr_body([], [], {}, [], None)


@pytest.fixture
def weekly_repo(tmp_path, monkeypatch):
    samples = Path(__file__).resolve().parents[1] / "samples/valid"
    root = tmp_path / "repo"
    path = root / "shims/python/sample/wiretruth-shim.json"
    path.parent.mkdir(parents=True)
    manifest = json.loads((samples / "shim-manifest/minimal.json").read_text())
    manifest.update(
        name="sample", primary_package="sample-sdk", packages={"sample-sdk": "1", "companion": "1"}
    )
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    document = json.loads((samples / "results/litellm-1.103.1.json").read_text())
    document["library"].update(name="sample", version="1")
    document["run"]["packages"] = manifest["packages"]
    document["cells"] = document["cells"][:1]
    document["cells"][0]["observation"] = None
    write_results(root, document)
    (root / "results/upstream.json").write_text("[]\n")
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    weekly.commit(root, "test: initial data", ".")
    pins = {"sample-sdk": "2", "companion": "1"}
    monkeypatch.setattr(weekly, "release_metadata", lambda *args: {"version": pins["sample-sdk"]})
    monkeypatch.setattr(weekly, "resolve_pins", lambda *args: pins.copy())
    monkeypatch.setattr(weekly, "build_env", lambda *args: None)
    monkeypatch.setattr(weekly, "offline_run", lambda *args: pytest.fail("unexpected run"))
    return root, pins


HARNESS_ERROR = "wiretruth run: --write-results: 1 HARNESS cell; fix the shim or the harness first"


def write_weekly_results(root):
    manifest = weekly.formats.load_shim_manifest(root / "shims/python/sample/wiretruth-shim.json")
    document = json.loads((root / "results/sample/1.json").read_text())
    document["library"]["version"] = manifest.packages[manifest.primary_package]
    document["run"].update(
        git_sha=weekly.git(root, "rev-parse", "HEAD"), packages=dict(manifest.packages)
    )
    document["cells"][0]["grade"] = "WRONG"
    document["cells"][0]["requirements"][0]["status"] = "CONTRADICTED"
    return write_results(root, document)


def test_bump_unchanged_pins_makes_no_commit(weekly_repo):
    root, pins = weekly_repo
    pins["sample-sdk"] = "1"
    before = weekly.git(root, "rev-parse", "HEAD")
    assert weekly.bump(root, "sample", []) is None
    assert weekly.git(root, "rev-parse", "HEAD") == before
    assert weekly.git(root, "status", "--porcelain") == ""


@pytest.mark.parametrize("companion_only", [False, True], ids=["new-primary", "new-companion"])
def test_bump_harness_restores_head_pins_and_results(weekly_repo, monkeypatch, companion_only):
    root, pins = weekly_repo
    if companion_only:
        pins.update({"sample-sdk": "1", "companion": "2"})
    before = weekly.git(root, "rev-parse", "HEAD")
    manifest = root / "shims/python/sample/wiretruth-shim.json"
    baseline = root / "results/sample/1.json"
    original_manifest, original_results = manifest.read_bytes(), baseline.read_bytes()

    def fail_run(root, name, flag):
        assert (name, flag) == ("sample", "--write-results")
        assert weekly.git(root, "rev-parse", "HEAD") != before
        write_weekly_results(root)
        return subprocess.CompletedProcess(
            [], 1, "truncation.text-stream ? HARNESS\n", HARNESS_ERROR
        )

    monkeypatch.setattr(weekly, "offline_run", fail_run)
    outcome = weekly.bump(root, "sample", [])
    assert f"skipped sample {pins['sample-sdk']}:" in outcome
    assert HARNESS_ERROR in outcome and "truncation.text-stream ? HARNESS" in outcome
    assert weekly.git(root, "rev-parse", "HEAD") == before
    assert manifest.read_bytes() == original_manifest
    assert baseline.read_bytes() == original_results
    assert not (root / "results/sample/2.json").exists()
    assert weekly.git(root, "status", "--porcelain") == ""


@pytest.mark.parametrize("success", [True, False], ids=["blocked-results", "skipped-release"])
def test_main_writes_artifacts_and_outputs(weekly_repo, tmp_path, monkeypatch, success):
    root, _ = weekly_repo
    out, output, summary = (tmp_path / name for name in ("out", "github-output", "summary"))
    monkeypatch.chdir(root)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    def run(root, name, flag):
        assert name == "sample"
        if flag == "--check-against":
            return subprocess.CompletedProcess([], 0, "", "")
        assert flag == "--write-results"
        write_weekly_results(root)
        return subprocess.CompletedProcess(
            [], 0 if success else 1, "", "" if success else HARNESS_ERROR
        )

    monkeypatch.setattr(weekly, "offline_run", run)
    assert weekly.main(["--lib", "sample", "--out", str(out)]) == (0 if success else 1)
    assert (out / "weekly.bundle").exists() is success
    assert (out / "blocked").exists() is success
    body = (out / "pr-body.md").read_text()
    assert "- sample: matches" in body
    assert summary.read_text() == body
    if success:
        assert "`truncation.text-stream` over `openai-chat`: WRONG" in body
    else:
        assert "skipped sample 2:" in body and HARNESS_ERROR in body
    lines = output.read_text().splitlines()
    assert f"changed={'true' if success else 'false'}" in lines
    assert any(line.startswith("date=") for line in lines)
