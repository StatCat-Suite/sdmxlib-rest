# sdmxlib-rest development tasks

# `.env` supplies UV_PUBLISH_TOKEN to release-5-pypi; a missing file is fine.
set dotenv-load

default:
    @just --list

# ── Dev ───────────────────────────────────────────────────────────────────────

# Ruff exits 0 on a `warning:` line (an invalid `# noqa`, a deprecated
# setting), so a warning never failed the gate and became noise everyone
# filtered out. sdmxlib had two survive three milestones that way (#283 there).
#
# Ruff, with its warnings failing the gate too
[group('dev')]
lint:
    #!/usr/bin/env bash
    set -uo pipefail
    out=$(uv run ruff check src tests tools 2>&1)
    status=$?
    echo "$out"
    if [[ $status -ne 0 ]]; then exit $status; fi
    if grep -q "^warning:" <<< "$out"; then
      echo "error: ruff emitted warnings; they fail the gate." >&2
      exit 1
    fi

# pyrefly at `preset = "all"` minus a documented deny-list (see pyrefly.toml)
# is the only type checker. Keep it at 0 findings. It covers `src` and
# `tests`, and dead suppressions fail here too: `unused-ignore` and
# `unused-type-ignore` are both on.
#
# Type check: pyrefly at preset = "all", src + tests
[group('dev')]
typecheck:
    uv run pyrefly check

[group('dev')]
checkfmt:
    uv run ruff format --check src tests tools

# Ruff's `convention = "google"` checks a docstring's structure and never its
# markup, so it let 61 Sphinx roles accumulate here. Four arms, as in
# statcatlib: Sphinx roles and RST literal blocks, NumPy section headers,
# argument sections not named `Args:`, and pydoclint in full.
#
# Fail if a docstring in src/ is not plain Google style
[group('dev')]
check-docstrings:
    uv run python tools/check_docstrings.py --check

# ANN401 sees a bare `Any` but not one inside a generic argument
# (`dict[str, Any]`), so the count of those may only go down, and going down
# must be recorded (`uv run python tools/count_any.py --write`).
#
# Fail if the count of `Any` in src/ moves off tools/any_baseline.txt
[group('dev')]
check-any:
    uv run python tools/count_any.py --check

# Only public PyPI may be named in this repository: an index URL, a lockfile or
# a `.netrc` committed from a machine behind a private mirror would publish
# that mirror to every consumer. An allow-list, because naming the mirror in
# the gate would itself be the leak.
#
# Fail if any tracked file names a package index other than public PyPI
[group('dev')]
check-indexes:
    uv run python tools/check_indexes.py --check

# Fail on the Writing style rules a pattern can check exactly (GH-55): a
# Markdown heading that is a question, a fixed list of colon reveals and hook
# clauses, and a bare issue reference on the docs site. The rest of the rules
# need a reader; see CLAUDE.md.
[group('dev')]
check-prose:
    uv run python tools/check_prose.py --check

# Lint + typecheck (inner dev loop)
[group('dev')]
check: lint checkfmt check-docstrings check-prose check-any check-indexes typecheck

# Format code
[group('dev')]
fmt:
    uv run ruff format src tests tools
    uv run ruff check --fix src tests tools

# ── Test ──────────────────────────────────────────────────────────────────────

[group('test')]
test:
    uv run pytest -m unit

[group('test')]
test-all:
    uv run pytest

# The same selection as `just test`, so the number reported is for the tests
# the gate runs.
#
# Unit tests with coverage (terminal, XML and HTML reports)
[group('test')]
coverage:
    uv run pytest -m unit --cov --cov-report=term-missing --cov-report=xml --cov-report=html
    @echo "HTML report: htmlcov/index.html"

# The ratios behind milestone 1. Needs a real bundle: the effects that motivate
# that work do not exist at fixture size, so without SDMXLIB_REST_BENCH_BUNDLE
# every test skips rather than measuring something meaningless.
#
# Bundle-tier benchmarks against a real bundle directory
[group('test')]
bench bundle="":
    SDMXLIB_REST_BENCH_BUNDLE="{{bundle}}" uv run pytest -m bundle -s -v

# A real bundle cannot leave the machine holding it, so the bundle tier also
# runs against `sdmxlib.testing.synthetic_bundle` (GH-37). SHAPE is a file
# under tests/bench/shapes/ or shapes/local/, or a .json path, chosen by hand:
# `large` is one 20k-code three-level hierarchy and four small dimensions, 20M
# rows, built in about 20 s. `census` is modelled on the public 2021 Census
# Profile: a ~580k-code six-level geography, a ~2.6k-code nine-level
# characteristic list, names in English and French, 20M rows, about 2 min. The
# bundle is cached under .bench/ by shape and seed.
#
# Build (or reuse) a synthetic bundle of SHAPE and run the bundle tier on it
[group('test')]
bench-synthetic shape="large" seed="1":
    #!/usr/bin/env bash
    set -euo pipefail
    name=$(basename "{{shape}}" .json)
    out="$(pwd)/.bench/synthetic-${name}-{{seed}}"
    uv run python tools/synthetic_bench.py build "{{shape}}" "${out}" --seed {{seed}}
    just bench "${out}"

# Its output is gitignored (tests/bench/shapes/local/): a real bundle's codelist
# sizes and observation count identify it. Needs a bundle sdmxlib 0.72 can open.
#
# Write a real bundle's shape for `just bench-synthetic NAME`
[group('test')]
bench-profile bundle flow name:
    uv run python tools/synthetic_bench.py profile "{{bundle}}" "{{flow}}" "{{name}}"

[group('test')]
ci: check test docs-build

# ── Docs ──────────────────────────────────────────────────────────────────────

# Serve the docs site locally with live reload
[group('docs')]
docs:
    uv run mkdocs serve

# --strict fails on a broken link, a link to a missing heading, or a page
# missing from the nav. It fetches sdmxlib's objects.inv, so it needs network.
#
# Build the docs site into site/
[group('docs')]
docs-build:
    uv run mkdocs build --strict

# ── Release ───────────────────────────────────────────────────────────────────
# The library versions independently of any consumer: a consumer pins a range
# and upgrades deliberately. Breaking a seam Protocol is a minor bump while
# we're pre-1.0, and will be a major bump after.

# Step 1 — Preview the next version and its generated changelog entry
[group('release')]
release-1-preview:
    @echo "Next version: $(git-cliff --bumped-version)"
    @echo ""
    @git-cliff --bump --unreleased

# Prepends the unreleased section, never regenerates the file: GitHub's history
# starts at a single "sdmxlib-rest 0.6.2" commit, so every entry before 0.6.3
# exists only in CHANGELOG.md, as do the curated edits to the generated ones.
# The prepend writes CHANGELOG.md's header once only because cliff.toml's
# header matches it exactly. Generated output is the *starting point* for an
# entry, not the entry.
#
# Step 2 — Start release/vX from origin/main and prepend a generated entry (pass VERSION to pin)
[group('release')]
release-2-changelog VERSION='':
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ -n "$(git status --porcelain)" ]]; then
      echo "error: the working tree has changes; commit or stash them first" >&2
      exit 1
    fi
    git fetch origin --tags
    git switch --detach origin/main
    OVERRIDE="{{VERSION}}"
    if [[ -n "${OVERRIDE}" ]]; then
      NEXT="v${OVERRIDE#v}"
      git switch -c "release/${NEXT}"
      echo "Writing changelog pinned to ${NEXT}..."
      git-cliff --unreleased --tag "${NEXT}" --prepend CHANGELOG.md
    else
      NEXT=$(git-cliff --bumped-version)
      git switch -c "release/${NEXT}"
      echo "Writing changelog for ${NEXT}..."
      git-cliff --unreleased --bump --prepend CHANGELOG.md
    fi
    echo ""
    echo "Generated entry prepended. EDIT IT — group the breaking changes and say"
    echo "what a consumer has to do — then run: just release-3-pr"

# The version comes from the branch release-2-changelog started, so a pinned
# version is given once and cannot disagree with the changelog entry.
#
# Step 3 — Bump the version, commit, push release/vX and open its PR
[group('release')]
release-3-pr:
    #!/usr/bin/env bash
    set -euo pipefail
    BRANCH=$(git branch --show-current)
    if [[ "${BRANCH}" != release/v* ]]; then
      echo "error: not on a release/vX branch (run just release-2-changelog)" >&2
      exit 1
    fi
    VER="${BRANCH#release/}"
    # Extracted BEFORE anything is pushed: a missing or empty section should
    # abort while it is still a local problem.
    NOTES=$(just release-notes "${VER}")
    uv version "${VER#v}"
    # Not uv.lock — it is gitignored on purpose (see .gitignore: consumers of a
    # published library resolve from pyproject.toml). `git add` on an ignored
    # path errors, which under `set -e` aborted the release half-done.
    git add CHANGELOG.md pyproject.toml
    git commit -m "chore(release): ${VER}"
    git push -u origin "${BRANCH}"
    gh pr create --base main --head "${BRANCH}" --title "chore(release): ${VER}" --body "${NOTES}"
    echo "When the PR's checks are green, run: just release-4-merge"

# Notes come from CHANGELOG.md, NOT `git-cliff --latest`. Generated output is
# the starting point for an entry, not the entry (see release-2-changelog), so
# publishing it raw contradicts the section a human just curated. v0.2.1 is the
# worked example: cliff listed a uv_build pin that lost a merge and never
# shipped, because the superseded commit is still unreleased-since-the-last-tag.
#
# Step 4 — Merge the release PR once CI is green, tag its merge commit, mint the GitHub release
[group('release')]
release-4-merge:
    #!/usr/bin/env bash
    set -euo pipefail
    BRANCH=$(git branch --show-current)
    if [[ "${BRANCH}" != release/v* ]]; then
      echo "error: not on a release/vX branch" >&2
      exit 1
    fi
    VER="${BRANCH#release/}"
    NOTES=$(just release-notes "${VER}")
    if [[ "$(gh pr view "${BRANCH}" --json state --jq .state)" != MERGED ]]; then
      # Exits non-zero if any check fails, so a red CI stops the release here.
      gh pr checks "${BRANCH}" --watch --fail-fast
      gh pr merge "${BRANCH}" --merge
    fi
    # `gh pr merge` has exited 0 without merging; trust the state, not the exit.
    STATE=$(gh pr view "${BRANCH}" --json state --jq .state)
    if [[ "${STATE}" != MERGED ]]; then
      echo "error: the ${BRANCH} PR is ${STATE}, not merged" >&2
      exit 1
    fi
    MERGE=$(gh pr view "${BRANCH}" --json mergeCommit --jq .mergeCommit.oid)
    git fetch origin
    git switch main
    git merge --ff-only origin/main
    git tag -a "${VER}" -m "${VER}" "${MERGE}"
    # Only this tag: the local clone may still hold the GitLab-era tags, which
    # point into history GitHub does not have.
    git push origin "${VER}"
    gh release create "${VER}" --verify-tag --title "${VER}" --notes "${NOTES}"
    git branch -D "${BRANCH}"
    git push origin --delete "${BRANCH}" 2>/dev/null || true
    git switch --detach "${VER}"
    echo "Released ${VER} on GitHub; checked out at the tag"
    echo "Next: just release-5-pypi"

# Steps 3 and 4 take their text from here. Accepts 0.7.0 or v0.7.0.
#
# Print a version's CHANGELOG.md section, as edited, without its heading line
[group('release')]
release-notes VERSION:
    #!/usr/bin/env bash
    set -euo pipefail
    VER="{{VERSION}}"
    NOTES=$(awk -v head="## [${VER#v}]" 'index($0, head) == 1 {f = 1; next} /^## \[/ {f = 0} f' CHANGELOG.md | sed '/./,$!d')
    if [[ -z "${NOTES//[[:space:]]/}" ]]; then
      echo "error: CHANGELOG.md has no section for ${VER#v}, or it is empty — run: just release-2-changelog" >&2
      exit 1
    fi
    printf '%s\n' "${NOTES}"

# Not wired into CI on purpose — a publish is permanent, so it stays a
# deliberate manual act.
#
# Step 5 — Build from the checked-out tag and publish to PyPI (UV_PUBLISH_TOKEN, from .env)
[group('release')]
release-5-pypi:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ -z "${UV_PUBLISH_TOKEN:-}" ]]; then
      echo "error: UV_PUBLISH_TOKEN is not set (put it in .env)" >&2
      exit 1
    fi
    # Build from a checked-out tag, never from a branch that has moved past it.
    if ! VER=$(git describe --tags --exact-match HEAD 2>/dev/null); then
      echo "error: HEAD is not a release tag; run git switch --detach vX.Y.Z" >&2
      exit 1
    fi
    if [[ -n "$(git status --porcelain)" ]]; then
      echo "error: the working tree has changes; a release builds the tag as tagged" >&2
      exit 1
    fi
    # `uv publish` defaults to public PyPI, but a shell configured for a
    # private mirror redirects it silently. Check the target by allow-list.
    ALLOWED="upload.pypi.org test.pypi.org"
    TARGET="${UV_PUBLISH_URL:-https://upload.pypi.org/legacy/}"
    HOST=$(printf '%s' "${TARGET}" | sed -E 's#^[a-z]+://([^/]+).*#\1#')
    if ! grep -qw -- "${HOST}" <<< "${ALLOWED}"; then
      echo "error: publish target is not a public index (${HOST})" >&2
      echo "       unset UV_PUBLISH_URL to publish to PyPI." >&2
      exit 1
    fi
    # A fresh dist/: `uv publish` uploads everything in it, and a stale wheel
    # from an earlier build would go up too.
    rm -rf dist
    uv build
    uv publish
    echo "Published ${VER} to PyPI"
