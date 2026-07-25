#!/usr/bin/env python3
"""Automate the release tail: version bump → CHANGELOG roll → tag → GitHub release.

The tail is five files that must agree on one number, and it got skipped once
before v0.8.0 — a stale tag pointing at a pre-merge commit, a CHANGELOG section
that never landed. Neither is caught by CI, because both are correct Python and
correct Markdown; they are only wrong relative to each other. This script makes
the agreement mechanical.

Two subcommands, matching the two moments the tail has:

    uv run --no-sync python scripts/release.py prepare X.Y.Z   # on the release branch
    uv run --no-sync python scripts/release.py publish X.Y.Z   # on main, after the squash-merge

`prepare` edits; `publish` tags. They are separate because a squash-merge sits
between them, and that merge is what creates the commit the tag must point at.

Stdlib only, by design: this runs in the same interpreter as the project and
must not drag a release-time dependency into a repo whose install is
deliberately light. Every mutation is a targeted line replace — in particular
`uv.lock` is hand-edited rather than regenerated, because `uv lock` churns 200+
lines and drops the spaCy model pin (see CONTRIBUTING.md).

Both subcommands take `--dry-run`, which prints exactly what would change and
touches nothing.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PYPROJECT = "pyproject.toml"
INIT = "src/blue_prism_v7_mcp/__init__.py"
README = "README.md"
UV_LOCK = "uv.lock"
CHANGELOG = "CHANGELOG.md"

PACKAGE_NAME = "blue-prism-v7-mcp"
COMPARE_URL = "https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare"

# The CHANGELOG uses an em-dash between version and date ("## [0.18.0] — 2026-07-22").
# Matched and emitted exactly, so a rolled section is indistinguishable from a
# hand-written one.
_DASH = "—"

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class ReleaseError(Exception):
    """A refusal: the repo is not in a state where this step is safe."""


# --- version parsing --------------------------------------------------------


def parse_version(value: str) -> tuple[int, int, int]:
    """Parse X.Y.Z into a comparable tuple, refusing anything else.

    Deliberately strict — no `v` prefix, no pre-release suffixes. The project is
    plain semver and a typo here would otherwise propagate into five files and a
    tag before anyone noticed.
    """
    match = _VERSION_RE.match(value.strip())
    if not match:
        raise ReleaseError(f"version must be X.Y.Z (digits only); got {value!r}.")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def current_version(pyproject_text: str) -> str:
    """The version currently declared in pyproject.toml — the source of truth.

    pyproject is the one site the build actually reads, so every other site is
    checked against it rather than against each other.
    """
    match = re.search(r'^version = "([^"]+)"', pyproject_text, flags=re.MULTILINE)
    if not match:
        raise ReleaseError(f'no `version = "..."` line found in {PYPROJECT}.')
    return match.group(1)


# --- the five version sites -------------------------------------------------
#
# Each takes text and returns text, so every one is testable without touching
# the filesystem, and each raises if its anchor is missing rather than returning
# the input unchanged — a silent no-op here is exactly the drift this replaces.


def _replace_once(text: str, pattern: str, replacement: str, site: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ReleaseError(
            f"could not locate the version line in {site} (pattern {pattern!r}). "
            "The file's shape changed — fix this script rather than editing by hand, "
            "or the next release drifts again."
        )
    return updated


def bump_pyproject(text: str, new: str) -> str:
    return _replace_once(text, r'^version = "[^"]+"', f'version = "{new}"', PYPROJECT)


def bump_init(text: str, new: str) -> str:
    return _replace_once(text, r'^__version__ = "[^"]+"', f'__version__ = "{new}"', INIT)


def bump_readme(text: str, new: str) -> str:
    """The README status line — a version site since it names the current release."""
    return _replace_once(
        text,
        r"\*\*Current release: v[^*]+\*\*",
        f"**Current release: v{new}**",
        README,
    )


def bump_uv_lock(text: str, new: str) -> str:
    """Hand-edit THIS package's version line in uv.lock.

    Never `uv lock`: regenerating churns 200+ lines and drops the spaCy model.
    The lock lists many packages, so the edit is anchored to the `name =` line
    for this one and rewrites the `version =` that immediately follows it.
    """
    return _replace_once(
        text,
        rf'^(name = "{re.escape(PACKAGE_NAME)}"\nversion = )"[^"]+"',
        rf'\g<1>"{new}"',
        UV_LOCK,
    )


# --- CHANGELOG --------------------------------------------------------------


def unreleased_body(text: str) -> str:
    """The content currently sitting under `## [Unreleased]`.

    Empty means there is nothing to release — `prepare` refuses on that, which
    also closes the gap the v0.8.0 miss went through (a release whose CHANGELOG
    entry was never written).
    """
    match = re.search(r"^## \[Unreleased\]\n(.*?)(?=^## \[)", text, flags=re.MULTILINE | re.DOTALL)
    if not match:
        raise ReleaseError(f"no `## [Unreleased]` section found in {CHANGELOG}.")
    return match.group(1).strip()


def roll_changelog(text: str, new: str, today: str) -> str:
    """Move `[Unreleased]` into a dated `[X.Y.Z]` section and rewrite the links.

    Two edits, both anchored: the section heading, and the link block at the
    bottom (`[Unreleased]` re-points at the new tag, and a new compare line is
    inserted for the release itself).
    """
    body = unreleased_body(text)
    if not body:
        raise ReleaseError(
            f"`## [Unreleased]` in {CHANGELOG} is empty — nothing to release. "
            "Write the entry as part of the change, not as a release-time afterthought."
        )

    previous = current_version_from_changelog(text)

    rolled = _replace_once(
        text,
        r"^## \[Unreleased\]\n",
        f"## [Unreleased]\n\n## [{new}] {_DASH} {today}\n",
        CHANGELOG,
    )

    rolled = _replace_once(
        rolled,
        r"^\[Unreleased\]: .*$",
        f"[Unreleased]: {COMPARE_URL}/v{new}...HEAD\n[{new}]: {COMPARE_URL}/v{previous}...v{new}",
        CHANGELOG,
    )
    return rolled


def current_version_from_changelog(text: str) -> str:
    """The most recent released version in the CHANGELOG (the one above the new one)."""
    match = re.search(r"^## \[(\d+\.\d+\.\d+)\]", text, flags=re.MULTILINE)
    if not match:
        raise ReleaseError(f"no released `## [X.Y.Z]` section found in {CHANGELOG}.")
    return match.group(1)


def extract_release_notes(text: str, version: str) -> str:
    """The body of `## [version]`, for use as the GitHub release notes."""
    pattern = rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[Unreleased\]:)"
    match = re.search(pattern, text, flags=re.MULTILINE | re.DOTALL)
    if not match:
        raise ReleaseError(
            f"no `## [{version}]` section in {CHANGELOG} — run `prepare {version}` first."
        )
    notes = match.group(1).strip()
    if not notes:
        raise ReleaseError(f"the `## [{version}]` section in {CHANGELOG} is empty.")
    return notes


# --- thin I/O + process wrappers (stubbed in tests) -------------------------


def read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def write(relative: str, text: str) -> None:
    (REPO_ROOT / relative).write_text(text, encoding="utf-8")


def run(*args: str) -> str:
    """Run a command in the repo root, returning stdout and raising on failure."""
    result = subprocess.run(
        args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ReleaseError(f"`{' '.join(args)}` failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def require_clean_tree() -> None:
    if run("git", "status", "--porcelain"):
        raise ReleaseError(
            "working tree is not clean. Commit or stash first — a release commit "
            "should contain the bump and nothing else."
        )


def committed_version() -> str:
    """The version as committed at HEAD, not as it sits in the working tree.

    `publish` runs after the squash-merge, and the failure it exists to catch is
    tagging a commit that does not carry the version being tagged. Reading the
    working tree would pass on exactly that mistake.
    """
    return current_version(run("git", "show", f"HEAD:{PYPROJECT}"))


# --- subcommands ------------------------------------------------------------


def prepare(new: str, dry_run: bool) -> None:
    """Apply the bump across all five sites and roll the CHANGELOG."""
    new_parts = parse_version(new)

    pyproject_text = read(PYPROJECT)
    old = current_version(pyproject_text)
    if new_parts <= parse_version(old):
        raise ReleaseError(f"{new} does not come after the current version {old}.")

    if not dry_run:
        require_clean_tree()

    changelog_text = read(CHANGELOG)
    edits = {
        PYPROJECT: bump_pyproject(pyproject_text, new),
        INIT: bump_init(read(INIT), new),
        README: bump_readme(read(README), new),
        UV_LOCK: bump_uv_lock(read(UV_LOCK), new),
        CHANGELOG: roll_changelog(changelog_text, new, date.today().isoformat()),
    }

    if dry_run:
        print(f"[dry run] {old} -> {new}; would rewrite:")
        for site in edits:
            print(f"  {site}")
        print(
            f"\n[dry run] release notes would be:\n{extract_release_notes(edits[CHANGELOG], new)}"
        )
        return

    for site, text in edits.items():
        write(site, text)

    print(f"Bumped {old} -> {new} in {len(edits)} files.")
    _report_version_sites(new)
    print(
        "\nNext: review the diff, commit on the release branch, open the PR, "
        f"squash-merge, then run `publish {new}` on main."
    )


def _report_version_sites(expected: str) -> None:
    """Read every site back and show it.

    The v0.3.0 gotcha was a bump that looked applied and was not, so the script
    does not trust its own writes — it re-reads and prints, and says plainly if
    a site disagrees.
    """
    found = {
        PYPROJECT: current_version(read(PYPROJECT)),
        INIT: _search(read(INIT), r'^__version__ = "([^"]+)"', INIT),
        README: _search(read(README), r"\*\*Current release: v([^*]+)\*\*", README),
        UV_LOCK: _search(
            read(UV_LOCK),
            rf'^name = "{re.escape(PACKAGE_NAME)}"\nversion = "([^"]+)"',
            UV_LOCK,
        ),
        CHANGELOG: current_version_from_changelog(read(CHANGELOG)),
    }
    print("\nVerification — every version site, read back:")
    for site, value in found.items():
        mark = "ok " if value == expected else "BAD"
        print(f"  [{mark}] {site}: {value}")
    disagreeing = [site for site, value in found.items() if value != expected]
    if disagreeing:
        raise ReleaseError(f"these sites do not read back as {expected}: {', '.join(disagreeing)}")


def _search(text: str, pattern: str, site: str) -> str:
    match = re.search(pattern, text, flags=re.MULTILINE)
    if not match:
        raise ReleaseError(f"could not read the version back from {site}.")
    return match.group(1)


def publish(version: str, dry_run: bool) -> None:
    """Tag the merged commit and create the GitHub release from the CHANGELOG."""
    parse_version(version)
    tag = f"v{version}"

    committed = committed_version()
    if committed != version:
        raise ReleaseError(
            f"HEAD declares version {committed}, not {version}. "
            "You are on the wrong commit — tagging here would point the release at "
            "the wrong code (the v0.14.0 failure). Check out the squash-merge commit."
        )

    if tag in run("git", "tag").splitlines():
        raise ReleaseError(f"tag {tag} already exists locally.")

    notes = extract_release_notes(read(CHANGELOG), version)

    if dry_run:
        print(f"[dry run] would tag {tag} at HEAD, push it, and create the release with:\n")
        print(notes)
        return

    require_clean_tree()
    run("git", "tag", tag)
    run("git", "push", "origin", tag)
    run("gh", "release", "create", tag, "--title", f"{tag}", "--notes", notes)
    print(f"Tagged {tag}, pushed it, and published the GitHub release.")


# --- entry point ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release.py",
        description="Prepare and publish a release (see CONTRIBUTING.md).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("prepare", "bump the five version sites and roll the CHANGELOG"),
        ("publish", "tag the merged commit and create the GitHub release"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("version", help="the release version, as X.Y.Z")
        sub.add_argument(
            "--dry-run",
            action="store_true",
            help="print what would change and touch nothing",
        )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args.version, args.dry_run)
        else:
            publish(args.version, args.dry_run)
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
