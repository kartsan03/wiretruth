from dataclasses import replace

import pytest

from wiretruth.formats import ShimManifest
from wiretruth.results import SavedResults
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
