# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,pii]"
python -m spacy download en_core_web_sm   # only needed for the [pii] backend
```

## The checks

```bash
pytest --cov=blue_prism_v7_mcp --cov=scripts --cov-report=term-missing   # tests + coverage
ruff check .                                            # lint (E/F + bugbear)
ruff format --check .                                   # formatting
mypy                                                    # types (src/, see pyproject)
mutmut run && mutmut results                            # mutation audit (periodic)
```

`pytest` (with the 100% coverage gate), `ruff check`, `ruff format --check`,
and `mypy` run in CI on every PR — across Python 3.11–3.13 — and must pass.
`mutmut` does **not** run in CI — it's a per-phase audit you run by hand (see
below).

## Before opening a PR: the degenerate-input pass

Tests prove the code works on *clean* inputs. Most defects here live at
**boundaries** and on **deployment-variable inputs** — the same seams the code
flags with "verify against your v7 API spec on day one." Before each PR, read
your own diff cold and, for every value that crosses a boundary (config in, an
HTTP response out, the clock), ask what each branch does when that value is
degenerate:

- **Empty / missing:** `""`, `None`, `[]`, `{}`, an absent dict key. Is "absent"
  distinguished from "present but falsy"? (A token key present with an empty
  value is *not* the same as no token key.)
- **Zero / boundary:** `0`, a TTL of `0`, exactly `page_size` rows, a value
  exactly *at* a limit. Is the comparison `>` or `>=`, and is that deliberate?
- **Formatting drift:** a trailing slash on a URL, trailing whitespace, mixed
  case — anything a human pastes from a console.
- **Duplicates / repeats:** the same page returned twice, a retried request.

Normalise such inputs **once, at the boundary where they enter** (e.g. in
`BPConfig`), so call sites stay simple and the rule can't be forgotten later.

## Testing conventions

- **Parametrize the edges, don't pick one nice example.** A function that takes a
  token, a date, or a URL should be tested with the empty/null/zero/at-limit
  forms, not just a well-formed one.
- **Every "varies by deployment" comment is an untested assumption.** When you
  write one, add the test for the variation in the same change.
- Keep `client`, `config`, and `mock` at 100% line coverage; a new branch needs a
  test that exercises it.

## Mutation testing (per phase)

Coverage tells you a line *ran*; it doesn't tell you a test would *fail* if the
line were wrong. `mutmut` flips operators and constants (`>` → `>=`, `and` →
`or`, …) and reports any mutant the suite fails to kill — a surviving mutant is
behaviour the tests don't actually pin.

Run it once per phase, not per commit. Triage the survivors by hand: some are
**equivalent mutants** (e.g. changing an internal cache-key string changes
nothing observable) and are expected noise. Don't chase a 100% kill rate — use
the list to find the *real* gaps and add a test for each.

```bash
mutmut run            # mutate + run the suite against each mutant
mutmut results        # list survivors
mutmut show <id>      # see the exact surviving mutation
```

## Cutting a release

The release tail is five files that must agree on one number, plus a tag that
must point at the right commit. Both have drifted before — a CHANGELOG section
that never landed, and a tag left pointing at a pre-merge commit — and neither
is something CI can catch, because each file is individually valid and only
wrong relative to the others. `scripts/release.py` makes the agreement
mechanical. It is stdlib-only and takes `--dry-run` on both subcommands.

**On the release branch**, once the work is merged-ready and `[Unreleased]`
holds the entry for it:

```bash
uv run --no-sync python scripts/release.py prepare 0.19.0 --dry-run
uv run --no-sync python scripts/release.py prepare 0.19.0
```

`prepare` bumps `pyproject.toml`, `src/blue_prism_v7_mcp/__init__.py`, the
README status line, and this package's own `version` line in `uv.lock`
(hand-edited — **never** `uv lock`, which churns 200+ lines and drops the spaCy
model pin), then moves `[Unreleased]` into a dated section and rewrites the
compare links. It refuses on a dirty tree, on a version that does not advance,
and on an empty `[Unreleased]` — a release with nothing recorded is the gap the
CHANGELOG rule exists to close. Afterwards it re-reads all five sites and
prints them, so a bump that silently failed to apply is visible rather than
assumed.

**On `main`, after the squash-merge:**

```bash
uv run --no-sync python scripts/release.py publish 0.19.0
```

`publish` reads the version out of `HEAD` — not the working tree, which would
pass on the exact mistake it guards — refuses if it disagrees or if the tag
already exists, then tags, pushes, and creates the GitHub release using the
CHANGELOG section as its notes.

## Branches, commits, PRs

- Branch per unit of work off `main`; open a PR; squash-merge so `main` stays one
  clean commit per change. Keep `main` green and releasable.
- Phase-sized work gets a PR with a real description (what / why / trade-offs).
  Trivial changes (typo, dep bump) can go straight to a branch and merge — don't
  ceremony-wrap them.
- **Landing a phase ticks its box** in the README "Status" roadmap, in the same
  PR — so the roadmap always reflects what's actually merged to `main`.
- Commit messages: plain, first-person, imperative. No `Co-Authored-By` trailers.
