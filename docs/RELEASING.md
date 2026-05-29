# Releasing the workstate

This document describes how to cut a release. It is the operator
playbook for the maintainer running `twine` and pushing tags. Consumers
(`uvx workstate-bootstrap install --remote-ref vX.Y.Z`) do not need to
read it.

## Quickstart

Three layers, one source of truth. Most days you only touch the top one:

```bash
make help                       # list every distribution target
make preflight                  # checklist only
make release-status             # show package/tag/PyPI state + suggested next monorepo tag
make release-pending            # release unpublished package versions + cut the next monorepo tag
make release-all                # release all publishable manifest packages in dep order
make release-monorepo TAG=v0.1.3  # cut the consumer tag
make smoke                      # install the latest tag into /tmp
```

The package catalog and release order come from `config/release/packages.json`.
Inspect the publishable set with `python scripts/release_manifest.py list --release-only --field name`.

The `Makefile` wraps `scripts/release.sh`, which in turn implements the
manual sequence documented in this file. All three layers stay in sync
— change one, change the others.

| Layer                  | When to use                                                 |
|------------------------|-------------------------------------------------------------|
| `make <target>`        | Day-to-day driver. Tab-completes; safest.                   |
| `scripts/release.sh`   | When you need flags the Makefile doesn't expose, or in CI.  |
| Manual `git`/`twine`   | Debugging a script failure, or releasing out of band.       |

See **[Common deployment tasks](#common-deployment-tasks)** below for the
full playbook keyed by intent ("ship a patch", "back out a bad wheel",
"coordinate a multi-package release", etc.).

## Scripted vs. manual

**The script** (`scripts/release.sh`) is the default — it implements
the same checklist + per-step sequence documented below, with safety
rails (refuses dirty tree, refuses to re-publish an existing version,
refuses to upload `.dev0`/`aN`/`bN`/`rcN` without `--allow-pre`, scopes
the upload glob to the bumped version, verifies the wheel reaches PyPI
before tagging, asserts per-package tags are ancestors of the monorepo
tag).

```bash
scripts/release.sh preflight                 # run the checklist only
scripts/release.sh status                    # show package/tag/PyPI state + suggested next monorepo tag
scripts/release.sh pending [vX.Y.Z]          # release unpublished package versions + cut a monorepo tag
scripts/release.sh package workstate-protocol  # release one package at its pyproject version
scripts/release.sh all                       # release all publishable manifest packages in dep order
scripts/release.sh monorepo v0.1.0           # cut the consumer-facing tag (after package tags)
scripts/release.sh --dry-run all             # show what would happen, take no action
```

For repos where the only dirty path is the generated `DASHBOARD.txt`, add
`--auto-stash-dashboard` to temporarily stash and restore that file during the
release gate instead of clearing it by hand.

**The manual sequence** below is what the script automates. Read it
when debugging a script failure, or when releasing a single package
out of band. The three layers (Makefile, script, manual) are kept
consistent; if you change one, change the others.

## Two tag families

The monorepo carries two families of tags. Only one of them is
load-bearing for consumers:

| Tag                          | Audience               | Purpose                                                                                          |
|------------------------------|------------------------|--------------------------------------------------------------------------------------------------|
| `vX.Y.Z`                     | external consumers     | The single ref that `workstate-bootstrap install --remote-ref vX.Y.Z` pins. One bump per release.  |
| `<package-name>-vX.Y.Z`      | informational          | Marks the commit that produced a given PyPI wheel. Useful for `git log`; not consumed directly.  |

Rule: **every monorepo `vX.Y.Z` is preceded by all the per-package tags
it contains, in the same commit chain**, and tagged in dependency
order:

```text
workstate-protocol → mcp-workstate-handoff → mcp-workstate-orchestrator → workstate-bootstrap → workstate-codex-bridge → vX.Y.Z (monorepo)
```

The dependency order is not optional: each downstream package's
`pyproject.toml` may pin the upstream version that just shipped, and
`twine upload` of a downstream wheel that resolves an unreleased
upstream from PyPI will fail at install time on the consumer.

## Tooling prerequisites

The release commands below invoke `build` and `twine` via `uvx`, so
no per-environment install of those tools is required. The only
hard prerequisites on the maintainer's machine are:

- `uv` / `uvx` on `PATH` (already required for everyday consumer use).
- `git` with push access to `origin` and tag-create permission.
- A configured `~/.pypirc` (or `TWINE_USERNAME` / `TWINE_PASSWORD`
  env vars) with PyPI publish credentials.

If you previously ran releases against a venv with `pip install build
twine`, that still works — just substitute `python -m build` for
`uvx --from build pyproject-build` and `python -m twine ...` for
`uvx twine ...` in every command below.

## Pre-release checklist

Before any `twine upload` or `git push --tags`:

1. **Working tree is clean and on `main`.**
   `git status` empty; `git rev-parse --abbrev-ref HEAD` returns `main`.
2. **All publishable package test suites pass.**

   ```bash
   for pkg in $(python scripts/release_manifest.py list --release-only --field name); do
       (cd packages/$pkg && python -m pytest -q) || { echo "FAIL: $pkg"; exit 1; }
   done
   ```

3. **The cross-package contract test passes.**
   `cd packages/mcp-workstate-orchestrator && python -m pytest tests/test_protocol_contract.py -q`
4. **The bootstrap install rehearsal passes.**
   `cd packages/workstate-bootstrap && python -m pytest tests/test_bootstrap_install_rehearsal.py -q`
5. **Generated client surfaces contain the release fixes.**
   The rehearsal must show `.mcp.json`, `.vscode/mcp.json`, and
   `.codex/config.toml` registering both managed MCP servers with
   `--workspace-root . serve-stdio`. Also run
   `cd packages/workstate-system && python -m pytest tests/test_generator_round_trip.py -q`
   so `.claude/skills` and `.codex/skills` carry the current
   `$branch-review` persistence guidance.
6. **Each `pyproject.toml` version matches the tag you are about to
   cut.** Mismatch here means the wheel uploaded to PyPI will carry
   the wrong version string.
7. **No private `git+ssh://` cross-package pins remain in any
   `pyproject.toml` you are about to publish.** The orchestrator's
   legacy `workstate-handoff-mcp @ git+ssh://...` line was replaced by
   `mcp-workstate-handoff>=A.B.C,<A+1.0.0` in the rename commit; verify
   the line is still a PyPI version range — `pypi` rejects direct VCS
   dependencies on upload.
8. **Each package CHANGELOG.md has an entry at the new version**
   (one line per shipped change is fine; this is for `git log`
   readers, not for marketing).

## The release sequence

Publishable packages release in manifest dependency order. Each step is
independent — if a later step fails, earlier package uploads are still good
and need not be backed out.

### 1. `workstate-protocol`

```bash
cd packages/workstate-protocol
# Confirm version in pyproject.toml matches the next tag.
rm -rf dist/                                    # avoid old artifacts sneaking into the upload glob
uvx --from build pyproject-build                # produces dist/*.whl + dist/*.tar.gz
uvx twine check dist/*
uvx twine upload dist/workstate_protocol-X.Y.Z*   # scope to the version you just built
cd ../..
git tag workstate-protocol-vX.Y.Z
git push origin workstate-protocol-vX.Y.Z
```

Verify the wheel is reachable: `pip index versions workstate-protocol`
must list the new version (PyPI's index can lag a few seconds).

### 2. `mcp-workstate-handoff`

Pins `workstate-protocol>=A.B.C,<A+1.0.0` from step 1. Upload only after
that line resolves on PyPI.

```bash
cd packages/mcp-workstate-handoff
rm -rf dist/
uvx --from build pyproject-build
uvx twine check dist/*
uvx twine upload dist/mcp_workstate_handoff-X.Y.Z*
cd ../..
git tag mcp-workstate-handoff-vX.Y.Z
git push origin mcp-workstate-handoff-vX.Y.Z
```

Verify: `uvx mcp-workstate-handoff --help` from a clean venv exits 0.

### 3. `mcp-workstate-orchestrator`

The git+ssh:// pin on `mcp-workstate-handoff` was already dropped in the
rename commit (`mcp-workstate-handoff>=0.5.0,<0.6.0`). For subsequent
release cycles, just bump the lower bound to match whatever
`mcp-workstate-handoff` version step 2 just shipped, then:

```bash
cd packages/mcp-workstate-orchestrator
rm -rf dist/
uvx --from build pyproject-build
uvx twine check dist/*
uvx twine upload dist/mcp_workstate_orchestrator-X.Y.Z*
cd ../..
git tag mcp-workstate-orchestrator-vX.Y.Z
git push origin mcp-workstate-orchestrator-vX.Y.Z
```

Verify: `uvx mcp-workstate-orchestrator --help` from a clean venv exits 0.

PyPI rejects direct VCS deps; if `twine check` fails because a
`git+ssh://` or `git+https://` URL has crept back in, fix it before
re-uploading.

### 4. `workstate-bootstrap`

```bash
cd packages/workstate-bootstrap
rm -rf dist/
uvx --from build pyproject-build
uvx twine check dist/*
uvx twine upload dist/workstate_bootstrap-X.Y.Z*
cd ../..
git tag workstate-bootstrap-vX.Y.Z
git push origin workstate-bootstrap-vX.Y.Z
```

Verify: `uvx workstate-bootstrap --help` from a clean venv exits 0.

### 5. `workstate-codex-bridge`

```bash
cd packages/workstate-codex-bridge
rm -rf dist/
uvx --from build pyproject-build
uvx twine check dist/*
uvx twine upload dist/workstate_codex_bridge-X.Y.Z*
cd ../..
git tag workstate-codex-bridge-vX.Y.Z
git push origin workstate-codex-bridge-vX.Y.Z
```

Verify: `uvx workstate-codex-bridge --help` from a clean venv exits 0.

### 6. The monorepo distribution tag

After every publishable package tag is pushed, cut the consumer-facing tag
on the same commit chain:

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

Smoke-test against a throwaway target. Note the `mkdir -p` — bootstrap
refuses to install into a path that doesn't already exist, so the
target must be created first:

```bash
SRC="git+https://github.com/darce/workstate@vX.Y.Z#subdirectory=packages/workstate-bootstrap"
TARGET="/tmp/release-smoke-$(date +%s)"
mkdir -p "$TARGET"
uvx --from "$SRC" workstate-bootstrap install --target "$TARGET"
```

The install must succeed without manual fixes, write `.mcp.json` and
`.vscode/mcp.json` referencing both managed servers, and produce a
populated `.claude/skills/` directory. If any of those fail, treat
the release as bad and back out (next section).

## Common upload failures

- **`HTTPError: 400 Bad Request from https://upload.pypi.org/legacy/`
  on the *first* file in `dist/`.** Almost always means that filename
  is already on PyPI — either a previous release of the same version,
  or an old wheel from before you bumped that's still sitting in
  `dist/`. PyPI rejects re-uploads of an existing filename. Fix:
  `rm -rf dist/`, rebuild, and use the version-scoped upload glob
  (`dist/<pkg_underscored>-X.Y.Z*`) so only the wheel + sdist for the
  *new* version are uploaded. PyPI normalises the project name to
  hyphens but the file artifacts use underscores
   (`workstate_protocol-0.1.1-...`, not `workstate-protocol-0.1.1-...`).

- **`SetuptoolsDeprecationWarning: project.license as a TOML table
  is deprecated`.** Warning only; safe to ship before 2027-Feb-18.
  Track the migration to a SPDX-string license + `project.license-files`
   separately — applies uniformly to package `pyproject.toml` files.

- **`twine check` fails with "direct VCS deps are not allowed".** A
  `git+ssh://` or `git+https://` line crept back into the package
  being uploaded. Replace with a `>=A.B.C,<NEXT_MAJOR` PyPI range
  before re-running build + upload.

- **`uvx twine upload` rejects credentials silently.** Confirm
  `~/.pypirc` has a `[pypi]` section with `username = __token__` and
  `password = pypi-...`, or export `TWINE_USERNAME=__token__` and
  `TWINE_PASSWORD=pypi-...` for the shell.

## Backing out a bad release

PyPI does not allow re-uploading a yanked version with the same
number. Recovery is always **bump-and-fix**, never "re-publish the
same version".

1. **Yank the bad wheel** (does not delete it; it tells `pip` to skip
   it for new installs). PyPI exposes yank via the project page only:

   ```bash
   open "https://pypi.org/manage/project/<pkg>/release/<version>/"
   ```

   Use the "Yank" button and supply a one-line reason. Yanking is
   reversible; deletion is not, so prefer yank.
2. **Delete the bad git tag locally and on the remote**:

   ```bash
   git tag -d <pkg>-vX.Y.Z
   git push --delete origin <pkg>-vX.Y.Z
   ```

   If the bad release was the monorepo `vX.Y.Z` tag, also delete
   that. Consumers who already pulled it keep working; new
   `--remote-ref vX.Y.Z` invocations will fail until the tag is
   re-pushed at the corrected commit.
3. **Bump the patch version**, fix the issue, and run the full
   release sequence again from step 1. Do not skip steps that
   succeeded the first time — `mcp-workstate-handoff` may need a no-op
   patch bump just to repin against a freshly-yanked
   `workstate-protocol`.

If the backout discovers a contract drift caught only by the monorepo
smoke test, add a regression test in
`packages/workstate-bootstrap/tests/test_bootstrap_install_rehearsal.py`
or the relevant package's contract suite as part of the bump-and-fix
commit.

## Common deployment tasks

A flat playbook keyed by intent. Every task assumes a clean working
tree on `main` synced with `origin/main` (the script enforces this; the
Makefile delegates to it).

### Task: confirm what is currently shipped

```bash
make versions   # local pyproject versions, package by package
make tags       # release tags on origin (per-package + monorepo vX.Y.Z)
make release-status
```

The output tells you (a) what each package's `pyproject.toml` claims
and (b) what's been pushed. A version listed under `make versions`
that has no matching `<pkg>-vX.Y.Z` under `make tags` is unreleased.
`make release-status` folds in the PyPI probe and prints the suggested next
consumer-facing monorepo tag.

### Task: pre-flight before any release

```bash
make preflight
```

Runs the full pre-release checklist (clean tree, every package's
pytest suite, the cross-package contract test, the bootstrap install
rehearsal, and a scan for direct-VCS deps in any `pyproject.toml`).
Fast-path with `make preflight FLAGS=--skip-tests` when you've just
run `make test` and only need the contract + rehearsal + tree checks.

### Task: ship a patch to a single package

The packages release in dependency order, but a true single-package
patch (no upstream change in the same release cycle) only needs that
one package re-published.

```bash
# 1. Bump packages/<pkg>/pyproject.toml version + add CHANGELOG entry.
$EDITOR packages/<pkg>/pyproject.toml packages/<pkg>/CHANGELOG.md

# 2. Commit and push to main.
git commit -am "<pkg>: <one-line summary>"
git push origin main

# 3. Release just this package.
make release-package PKG=<pkg>

# 4. If consumers pin <pkg> via the monorepo tag (i.e. the bootstrap
#    or the SHARED surfaces moved), cut a new monorepo tag too:
make release-monorepo TAG=v<X.Y.Z>

# 5. Smoke-test from a fresh shell against the new monorepo tag.
make smoke
```

Skip step 4 when the patched package is purely a library consumed via
PyPI (e.g. `workstate-protocol`) and no consumer-visible surface in the
overlay clone changed. The per-package tag (`<pkg>-v<X.Y.Z>`) is
sufficient in that case.

### Task: ship a coordinated multi-package release

When upstream changes ripple downstream (e.g. `workstate-protocol` adds
a field, `mcp-workstate-handoff` adopts it, orchestrator adopts the new
handoff version, bootstrap pins the new orchestrator):

```bash
# 1. Bump every affected pyproject.toml + CHANGELOG.
#    Bump downstream pin ranges to match the new upstream lower bound.
#    Leave the upper bound at the next major (e.g. <0.6.0).

# 2. Single commit per package, in dependency order — keeps git log
#    parseable and lets you stop midway without orphan tags.

# 3. Push to main, then:
make release-all                  # releases publishable manifest packages in dep order, halts on first failure
make release-monorepo TAG=v<X.Y.Z>
make smoke
```

If `release-all` halts after an earlier package succeeds, that package is
published and tagged. Fix the issue, push the fix, and **re-run** `release-all` —
the script's `ensure_no_published_version` guard skips packages whose
pyproject version is already on PyPI, so it resumes from the failure
point rather than re-uploading.

### Task: ship only the versions that are still pending

When a release cycle stalls halfway through, or when only a subset of the
package versions in `pyproject.toml` have not been published yet, use the
pending-release path instead of manually deciding which package to run next.

```bash
make release-status
make release-pending                    # auto-chooses the next vX.Y.Z tag

# If the only dirty file is the generated dashboard:
make release-pending FLAGS=--auto-stash-dashboard

# If you want to override the suggested monorepo tag:
make release-pending TAG=v<X.Y.Z>
```

The script releases only packages whose current `pyproject.toml` version has
no matching `<pkg>-vX.Y.Z` tag and is not already on PyPI, then cuts the
consumer-facing monorepo tag (default: next patch tag after the latest `v*`).
It refuses the unsafe case where a version appears on PyPI but no matching
package tag exists.

### Task: preview a release without taking action

```bash
make dry-run-all                      # publishable package + tag commands, no uploads
make dry-run-monorepo TAG=v<X.Y.Z>    # monorepo tag flow only
```

Dry-run prints every command it would execute, prefixed `[dry-run]`.
The preflight gate (clean tree, tests, contract, rehearsal) still runs
under dry-run — that's a feature, not a bug; you want to know whether
the *real* run would clear preflight before you commit to it.

### Task: ship a pre-release (.devN, aN, bN, rcN)

The script refuses pre-release versions by default to prevent slipping
a `.dev0` to PyPI by accident. Opt in explicitly:

```bash
make release-package PKG=<pkg> FLAGS=--allow-pre
```

Test installers must opt into pre-releases too (`pip install --pre <pkg>`,
`uv add --prerelease=allow <pkg>`); a stable consumer tag will not
resolve a pre-release. Cut a monorepo tag for a pre-release only when
you actually want consumers using `--remote-ref` to receive it.

### Task: smoke-test the latest tag end-to-end

```bash
make smoke
```

Picks the highest `v*` tag on `origin`, mkdirs `/tmp/agentic-smoke-...`,
`git init`s it, and runs the full `uvx --from "git+ssh://...@<tag>..."`
install. The install must:

- exit 0,
- write `.mcp.json`, `.vscode/mcp.json`, `.codex/config.toml` referencing
  both managed servers,
- populate `.claude/skills`, `.claude/commands`, `.github/prompts`,
  `.codex/skills`,
- set `core.hooksPath` to `scripts/hooks` and resolve to a populated
  hooks directory.

If any of those fail, treat the release as broken and follow
**[Backing out a bad release](#backing-out-a-bad-release)** above.

### Task: clean stale build artifacts

```bash
make clean    # removes packages/*/dist/
```

Always safe; the build tooling regenerates `dist/` on the next release.
Useful when a previous failed build left artifacts that would trip
`twine`'s version-scoped upload glob.

### Task: back out a bad release

The compact form of the [back-out section](#backing-out-a-bad-release)
above:

```bash
# 1. (optional) Yank the bad PyPI wheel via the project page UI.
open "https://pypi.org/manage/project/<pkg>/release/<X.Y.Z>/"

# 2. Delete the bad git tag locally and on origin.
git tag -d <pkg>-v<X.Y.Z>
git push --delete origin <pkg>-v<X.Y.Z>
# Drop the monorepo tag too if the bad release reached the consumer surface:
git tag -d v<X.Y.Z> && git push --delete origin v<X.Y.Z>

# 3. Bump-and-fix.
$EDITOR packages/<pkg>/pyproject.toml packages/<pkg>/CHANGELOG.md
git commit -am "<pkg>: fix <issue> (bumps to <X.Y.Z+1>)"
git push origin main
make release-package PKG=<pkg>
make release-monorepo TAG=v<X'.Y'.Z'>   # next monorepo number
make smoke
```

PyPI never permits re-uploading a yanked or deleted version under the
same number, so the answer to *every* "release went out broken" is
**bump-and-fix**, never "redo with the same number".

### Task: rotate PyPI credentials

`twine` reads `~/.pypirc` (or `TWINE_USERNAME` / `TWINE_PASSWORD` env
vars). To rotate:

```bash
# 1. Generate a new project-scoped token at:
open "https://pypi.org/manage/account/token/"

# 2. Replace the password line in [pypi] in ~/.pypirc, then:
make preflight FLAGS=--skip-tests   # cheap end-to-end auth probe via the dirty-tree check
make dry-run-all                    # verifies twine still resolves credentials
```

(The dry-run won't *use* credentials, but it loads the same code path
and surfaces malformed `.pypirc` early.)

### Task: rehearse the release flow without touching origin or PyPI

Useful before a high-stakes coordinated release, or when training a new
maintainer:

```bash
git checkout -b throwaway/release-rehearsal
# Bump pyproject + CHANGELOG to a fake version on this branch.
make dry-run-all
make dry-run-monorepo TAG=v99.99.99
git checkout main && git branch -D throwaway/release-rehearsal
```

Dry-run never pushes tags, never uploads, and never mutates remote
state — but exercises every preflight check and every code path the
real run would hit.

## Who runs the release

While the monorepo is private (every acceptance criterion in implementation note
must be green before the public flip), only the maintainer with both
PyPI publish credentials and `darce/workstate` push
access runs releases. CI does not auto-publish — the
`.github/workflows/test.yml` matrix is a gate, not a release driver.

When the repo flips public, the same playbook applies; only the
authentication footnote changes (consumers no longer need
`darce/*` SSH access).

## Trusted Publishing runway

The repo now carries a top-level runway workflow at
`.github/workflows/release-publish.yml`. It is intentionally narrow:
one package per dispatch, a separate build job, and a final publish job
that is the only place with `id-token: write` for PyPI Trusted
Publishing. The workflow always computes `scripts/release.sh plan
--json`, uploads that plan as an artifact, and refuses to publish unless
the selected package is currently `pending_upload`.

Until each PyPI project is configured with a matching Trusted Publisher,
local `twine` publishing remains the approved path. Use the workflow with
`dry_run=true` to rehearse the plan/build path without attempting an
upload; switch `dry_run=false` only after the PyPI publisher and the
GitHub `pypi` environment approval gate are both configured.
