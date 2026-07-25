"""Tests for scripts/release.py.

The script exists to stop the release tail drifting, so the tests pin the two
failures it was written for: a version site left stale, and a tag pointing at a
commit that does not carry the version being tagged.

Every filesystem edit runs against a throwaway fixture repo (REPO_ROOT is
redirected), and every git/gh call goes through the module's `run` wrapper,
which is stubbed. Nothing here touches the real repository or the network.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release.py"
_spec = importlib.util.spec_from_file_location("release_script", _SCRIPT)
assert _spec and _spec.loader
release = importlib.util.module_from_spec(_spec)
sys.modules["release_script"] = release
_spec.loader.exec_module(release)


PYPROJECT_TEXT = """\
[build-system]
requires = ["setuptools>=77", "wheel"]

[project]
name = "blue-prism-v7-mcp"
version = "0.18.0"
requires-python = ">=3.11"
"""

INIT_TEXT = '''\
"""Package root."""

__version__ = "0.18.0"
'''

README_TEXT = """\
# blue-prism-v7-mcp

## Status

**Current release: v0.18.0** — see [CHANGELOG.md](CHANGELOG.md) for the full
release history.
"""

UV_LOCK_TEXT = """\
[[package]]
name = "certifi"
version = "2025.1.1"

[[package]]
name = "blue-prism-v7-mcp"
version = "0.18.0"
source = { editable = "." }
"""

CHANGELOG_TEXT = """\
# Changelog

## [Unreleased]

### Added
- A release script.

## [0.18.0] — 2026-07-22

Transport governance.

### Added
- A token bucket.

## [0.17.0] — 2026-07-22

Scoped session reads.

[Unreleased]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.18.0...HEAD
[0.18.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.16.0...v0.17.0
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway repo carrying all five version sites at 0.18.0."""
    (tmp_path / "src" / "blue_prism_v7_mcp").mkdir(parents=True)
    (tmp_path / release.PYPROJECT).write_text(PYPROJECT_TEXT, encoding="utf-8")
    (tmp_path / release.INIT).write_text(INIT_TEXT, encoding="utf-8")
    (tmp_path / release.README).write_text(README_TEXT, encoding="utf-8")
    (tmp_path / release.UV_LOCK).write_text(UV_LOCK_TEXT, encoding="utf-8")
    (tmp_path / release.CHANGELOG).write_text(CHANGELOG_TEXT, encoding="utf-8")
    monkeypatch.setattr(release, "REPO_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def commands(monkeypatch):
    """Record every git/gh invocation and answer the ones the flow reads back."""
    calls: list[tuple[str, ...]] = []
    answers: dict[tuple[str, ...], str] = {
        ("git", "status", "--porcelain"): "",
        ("git", "tag"): "v0.17.0\nv0.18.0",
        ("git", "show", "HEAD:pyproject.toml"): PYPROJECT_TEXT,
    }

    def fake_run(*args: str) -> str:
        calls.append(args)
        return answers.get(args, "")

    monkeypatch.setattr(release, "run", fake_run)
    return calls, answers


# --- version parsing --------------------------------------------------------


def test_parse_version_accepts_plain_semver():
    assert release.parse_version("1.2.3") == (1, 2, 3)
    assert release.parse_version("  0.19.0  ") == (0, 19, 0)


@pytest.mark.parametrize("bad", ["v1.2.3", "1.2", "1.2.3.4", "1.2.3rc1", "", "x.y.z"])
def test_parse_version_refuses_anything_else(bad):
    with pytest.raises(release.ReleaseError, match="must be X.Y.Z"):
        release.parse_version(bad)


def test_current_version_reads_pyproject():
    assert release.current_version(PYPROJECT_TEXT) == "0.18.0"


def test_current_version_raises_without_a_version_line():
    with pytest.raises(release.ReleaseError, match="no `version"):
        release.current_version("[project]\nname = 'x'\n")


# --- the five version sites -------------------------------------------------


def test_bump_pyproject():
    assert 'version = "0.19.0"' in release.bump_pyproject(PYPROJECT_TEXT, "0.19.0")


def test_bump_pyproject_leaves_other_keys_alone():
    bumped = release.bump_pyproject(PYPROJECT_TEXT, "0.19.0")
    assert 'requires-python = ">=3.11"' in bumped
    assert 'requires = ["setuptools>=77", "wheel"]' in bumped


def test_bump_init():
    assert '__version__ = "0.19.0"' in release.bump_init(INIT_TEXT, "0.19.0")


def test_bump_readme():
    assert "**Current release: v0.19.0**" in release.bump_readme(README_TEXT, "0.19.0")


def test_bump_uv_lock_targets_only_this_package():
    bumped = release.bump_uv_lock(UV_LOCK_TEXT, "0.19.0")
    assert 'name = "blue-prism-v7-mcp"\nversion = "0.19.0"' in bumped
    assert 'name = "certifi"\nversion = "2025.1.1"' in bumped


@pytest.mark.parametrize(
    "bump",
    [release.bump_pyproject, release.bump_init, release.bump_readme, release.bump_uv_lock],
)
def test_each_site_refuses_when_its_anchor_is_missing(bump):
    with pytest.raises(release.ReleaseError, match="could not locate the version line"):
        bump("nothing to see here\n", "0.19.0")


# --- CHANGELOG --------------------------------------------------------------


def test_unreleased_body_returns_pending_entries():
    assert "A release script." in release.unreleased_body(CHANGELOG_TEXT)


def test_unreleased_body_empty_when_nothing_pending():
    text = CHANGELOG_TEXT.replace("### Added\n- A release script.\n\n", "")
    assert release.unreleased_body(text) == ""


def test_unreleased_body_raises_without_the_section():
    with pytest.raises(release.ReleaseError, match="no `## \\[Unreleased\\]`"):
        release.unreleased_body("# Changelog\n\n## [0.1.0] — 2026-01-01\n")


def test_roll_changelog_moves_entries_into_a_dated_section():
    rolled = release.roll_changelog(CHANGELOG_TEXT, "0.19.0", "2026-07-25")
    assert "## [0.19.0] — 2026-07-25" in rolled
    assert release.unreleased_body(rolled) == ""
    assert "A release script." in release.extract_release_notes(rolled, "0.19.0")


def test_roll_changelog_rewrites_the_compare_links():
    rolled = release.roll_changelog(CHANGELOG_TEXT, "0.19.0", "2026-07-25")
    assert f"[Unreleased]: {release.COMPARE_URL}/v0.19.0...HEAD" in rolled
    assert f"[0.19.0]: {release.COMPARE_URL}/v0.18.0...v0.19.0" in rolled
    # The existing links survive untouched.
    assert f"[0.18.0]: {release.COMPARE_URL}/v0.17.0...v0.18.0" in rolled


def test_roll_changelog_refuses_an_empty_unreleased():
    text = CHANGELOG_TEXT.replace("### Added\n- A release script.\n\n", "")
    with pytest.raises(release.ReleaseError, match="nothing to release"):
        release.roll_changelog(text, "0.19.0", "2026-07-25")


def test_current_version_from_changelog_takes_the_topmost_release():
    assert release.current_version_from_changelog(CHANGELOG_TEXT) == "0.18.0"


def test_current_version_from_changelog_raises_when_none_released():
    with pytest.raises(release.ReleaseError, match="no released"):
        release.current_version_from_changelog("# Changelog\n\n## [Unreleased]\n")


def test_extract_release_notes_stops_at_the_next_section():
    notes = release.extract_release_notes(CHANGELOG_TEXT, "0.18.0")
    assert "Transport governance." in notes
    assert "Scoped session reads." not in notes


def test_extract_release_notes_stops_at_the_link_block():
    rolled = release.roll_changelog(CHANGELOG_TEXT, "0.19.0", "2026-07-25")
    notes = release.extract_release_notes(rolled, "0.17.0")
    assert "Scoped session reads." in notes
    assert "[Unreleased]:" not in notes


def test_extract_release_notes_raises_for_an_unknown_version():
    with pytest.raises(release.ReleaseError, match="run `prepare 9.9.9` first"):
        release.extract_release_notes(CHANGELOG_TEXT, "9.9.9")


def test_extract_release_notes_raises_on_an_empty_section():
    text = "# Changelog\n\n## [0.19.0] — 2026-07-25\n\n## [0.18.0] — 2026-07-22\n\nreal notes\n"
    with pytest.raises(release.ReleaseError, match="is empty"):
        release.extract_release_notes(text, "0.19.0")


# --- process wrappers -------------------------------------------------------


def test_run_returns_stdout(repo):
    assert release.run(sys.executable, "-c", "print('hello')") == "hello"


def test_run_raises_on_failure(repo):
    with pytest.raises(release.ReleaseError, match="failed"):
        release.run(sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(1)")


def test_require_clean_tree_passes_when_clean(commands):
    release.require_clean_tree()  # no raise


def test_require_clean_tree_raises_when_dirty(commands, monkeypatch):
    monkeypatch.setattr(release, "run", lambda *a: " M README.md")
    with pytest.raises(release.ReleaseError, match="not clean"):
        release.require_clean_tree()


def test_committed_version_reads_head_not_the_working_tree(repo, commands):
    (repo / release.PYPROJECT).write_text(
        PYPROJECT_TEXT.replace("0.18.0", "9.9.9"), encoding="utf-8"
    )
    assert release.committed_version() == "0.18.0"


# --- prepare ----------------------------------------------------------------


def test_prepare_updates_every_site(repo, commands, capsys):
    release.prepare("0.19.0", dry_run=False)

    assert 'version = "0.19.0"' in (repo / release.PYPROJECT).read_text()
    assert '__version__ = "0.19.0"' in (repo / release.INIT).read_text()
    assert "**Current release: v0.19.0**" in (repo / release.README).read_text()
    assert 'name = "blue-prism-v7-mcp"\nversion = "0.19.0"' in (repo / release.UV_LOCK).read_text()

    changelog = (repo / release.CHANGELOG).read_text()
    assert "## [0.19.0]" in changelog
    assert release.unreleased_body(changelog) == ""

    out = capsys.readouterr().out
    assert "Bumped 0.18.0 -> 0.19.0 in 5 files." in out
    assert out.count("[ok ]") == 5


def test_prepare_requires_a_clean_tree(repo, monkeypatch):
    monkeypatch.setattr(release, "run", lambda *a: " M src/blue_prism_v7_mcp/client.py")
    with pytest.raises(release.ReleaseError, match="not clean"):
        release.prepare("0.19.0", dry_run=False)


@pytest.mark.parametrize("not_newer", ["0.18.0", "0.17.9", "0.1.0"])
def test_prepare_refuses_a_version_that_does_not_advance(repo, commands, not_newer):
    with pytest.raises(release.ReleaseError, match="does not come after"):
        release.prepare(not_newer, dry_run=False)


def test_prepare_refuses_when_nothing_is_pending(repo, commands):
    (repo / release.CHANGELOG).write_text(
        CHANGELOG_TEXT.replace("### Added\n- A release script.\n\n", ""), encoding="utf-8"
    )
    with pytest.raises(release.ReleaseError, match="nothing to release"):
        release.prepare("0.19.0", dry_run=False)


def test_prepare_dry_run_changes_nothing(repo, commands, capsys):
    before = {site: (repo / site).read_text() for site in (release.PYPROJECT, release.CHANGELOG)}

    release.prepare("0.19.0", dry_run=True)

    for site, text in before.items():
        assert (repo / site).read_text() == text
    out = capsys.readouterr().out
    assert "[dry run] 0.18.0 -> 0.19.0" in out
    assert "A release script." in out


def test_prepare_dry_run_does_not_consult_git(repo, commands):
    calls, _ = commands
    release.prepare("0.19.0", dry_run=True)
    assert calls == []


# --- the read-back verification --------------------------------------------


def test_report_version_sites_raises_when_one_is_stale(repo):
    """The v0.3.0 gotcha: a bump that looks applied but is not."""
    for site, text in (
        (release.PYPROJECT, PYPROJECT_TEXT.replace("0.18.0", "0.19.0")),
        (release.INIT, INIT_TEXT.replace("0.18.0", "0.19.0")),
        (
            release.UV_LOCK,
            UV_LOCK_TEXT.replace('mcp"\nversion = "0.18.0', 'mcp"\nversion = "0.19.0'),
        ),
        (release.CHANGELOG, CHANGELOG_TEXT.replace("## [0.18.0]", "## [0.19.0]")),
    ):
        (repo / site).write_text(text, encoding="utf-8")
    # README deliberately left at 0.18.0.
    with pytest.raises(release.ReleaseError, match=r"do not read back as 0\.19\.0: README\.md"):
        release._report_version_sites("0.19.0")


def test_search_raises_when_the_pattern_is_absent():
    with pytest.raises(release.ReleaseError, match="could not read the version back"):
        release._search("nothing here", r"^__version__ = \"([^\"]+)\"", "somefile")


# --- publish ----------------------------------------------------------------


def test_publish_refuses_a_tag_that_already_exists(repo, commands):
    with pytest.raises(release.ReleaseError, match="already exists locally"):
        release.publish("0.18.0", dry_run=False)


def test_publish_refuses_when_head_carries_a_different_version(repo, commands):
    with pytest.raises(release.ReleaseError, match="HEAD declares version 0.18.0, not 0.19.0"):
        release.publish("0.19.0", dry_run=False)


def test_publish_dry_run_prints_notes_and_touches_nothing(repo, commands, capsys):
    calls, answers = commands
    answers[("git", "tag")] = "v0.17.0"
    release.publish("0.18.0", dry_run=True)

    out = capsys.readouterr().out
    assert "[dry run] would tag v0.18.0" in out
    assert "Transport governance." in out
    assert ("git", "tag", "v0.18.0") not in calls
    assert not any(call[0] == "gh" for call in calls)


def test_publish_happy_path_issues_the_three_commands(repo, commands, capsys):
    calls, answers = commands
    answers[("git", "tag")] = "v0.17.0"
    release.publish("0.18.0", dry_run=False)

    assert ("git", "tag", "v0.18.0") in calls
    assert ("git", "push", "origin", "v0.18.0") in calls
    gh = [call for call in calls if call[0] == "gh"]
    assert len(gh) == 1
    assert gh[0][:4] == ("gh", "release", "create", "v0.18.0")
    assert "Transport governance." in gh[0][-1]


def test_publish_refuses_a_malformed_version(repo, commands):
    with pytest.raises(release.ReleaseError, match="must be X.Y.Z"):
        release.publish("v0.19", dry_run=False)


# --- CLI --------------------------------------------------------------------


def test_main_dispatches_prepare(repo, commands):
    assert release.main(["prepare", "0.19.0", "--dry-run"]) == 0


def test_main_dispatches_publish(repo, commands):
    _, answers = commands
    answers[("git", "tag")] = "v0.17.0"
    assert release.main(["publish", "0.18.0", "--dry-run"]) == 0


def test_main_reports_a_refusal_on_stderr(repo, commands, capsys):
    assert release.main(["prepare", "0.1.0"]) == 1
    assert "error: 0.1.0 does not come after" in capsys.readouterr().err


def test_main_requires_a_subcommand(repo):
    with pytest.raises(SystemExit):
        release.main([])
