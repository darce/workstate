# Workstate — distribution automation
#
# Thin wrapper over scripts/release.sh (the authoritative release driver)
# plus the test gates the maintainer runs by hand before each release.
# Authoritative playbook: docs/RELEASING.md.
#
# Usage examples:
#   make preflight                          # checklist only
#   make release-status                     # show package/tag/PyPI release state
#   make release-plan FLAGS=--json          # show the canonical machine-readable release plan
#   make release-public                     # orchestrate the public-release flow (dry-run by default)
#   make release-public FLAGS=--execute     # push/tag/publish after interactive confirmation
#   make release-public FLAGS=--execute PROBE_PUBLISHERS=1  # force the live publisher probe
#                                           # (verified publishers are ASSUMED by default — the
#                                           # bindings were confirmed once in the PyPI settings UI
#                                           # and the JSON API hides publisher metadata anyway)
#   make release-pending                    # release only unpublished package versions + cut next monorepo tag
#   make release-prepare PKG=workstate-protocol BUMP=patch
#   make release-package PKG=workstate-protocol
#   make release-all
#   make release-monorepo TAG=v0.1.3
#   make dry-run-all                        # show what release-all would do
#   make dry-run-pending                    # preview release-pending without uploads or tag pushes
#   make release-all FLAGS=--skip-tests     # pass-through flag to release.sh
#   make dogfood DOGFOOD_SOURCE=package     # install from released PyPI wheels
#
# Variables:
#   PKG     — package directory under packages/ for release-package
#   TAG     — monorepo tag (vX.Y.Z) for release-monorepo
#   FLAGS   — extra flags forwarded to scripts/release.sh
#   DOGFOOD_SOURCE         — git_overlay (default), package, or worktree
#   DOGFOOD_BOOTSTRAP_SPEC — uv package spec used when DOGFOOD_SOURCE=package
#   DOGFOOD_SYSTEM_SPEC    — uv package spec used when DOGFOOD_SOURCE=package

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c

MANIFEST_HELPER := scripts/release_manifest.py
PACKAGES := $(shell python $(MANIFEST_HELPER) list --field name)
RELEASE_PACKAGES := $(shell python $(MANIFEST_HELPER) list --release-only --field name)
RELEASE  := scripts/release.sh
FLAGS    ?=
DOGFOOD_SOURCE ?= git_overlay
DOGFOOD_BOOTSTRAP_SPEC ?= workstate-bootstrap
DOGFOOD_SYSTEM_SPEC ?= workstate-system
DOGFOOD_UVX_FLAGS ?= --refresh
# Extra flags forwarded verbatim to every `workstate-bootstrap install`
# invocation inside `make dogfood` — the sanctioned opt-in path for the
# cross-harness Stop adapters declared in portable_commands.json, e.g.
# `make dogfood DOGFOOD_INSTALL_FLAGS=--install-claude-stop-hook-local`
# (also: --install-codex-stop-hook, --install-vscode-stop-hook,
# --install-grok-stop-hook). Never applied to
# `status` invocations.
DOGFOOD_INSTALL_FLAGS ?=

# Wire the lifecycle.mk `make format` target (the hoist-safe entry
# point referenced by the branch-lifecycle skill) to this monorepo's
# `format-all` walker. Bootstrap consumers without a `format-all`
# target override this in their own Makefile (or accept the loud no-op
# default that lifecycle.mk ships).
LIFECYCLE_FORMATTER := $(MAKE) format-all
# implementation note: npm deps for the in-repo canvas app on fresh linked worktrees.

# Pull in package-owned Make fragments. implementation note S3: the shipped overlay
# fragments are co-located under the payload; the internal-only evals fragment
# stays at its pre-S3 location, so include it separately. Use `-include` so a
# missing fragment never blocks the root `Makefile`.
-include packages/workstate-system/workstate_system/payload/Makefile.d/*.mk
-include packages/workstate-system/Makefile.d/evals.mk

.DEFAULT_GOAL := help

.PHONY: help
help:
	@awk 'BEGIN{FS=":.*##"} /^[a-zA-Z0-9_.-]+:.*##/ {printf "  \033[1;36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ----- gates ----------------------------------------------------------------

.PHONY: preflight
preflight: ## Run the pre-release checklist (clean tree, tests, contract, rehearsal)
	$(RELEASE) preflight $(FLAGS)

.PHONY: test
test: ## Run every package's pytest suite
	@for pkg in $(PACKAGES); do \
	    echo "==> $$pkg"; \
	    (cd packages/$$pkg && python -m pytest -q) || exit 1; \
	done

# The contract test runs `workstate_handoff_mcp`, `workstate_orchestrator_mcp`, and
# `workstate_protocol` in-process against each other. Resolve all three from the
# worktree's own `src/` via PYTHONPATH (mirroring `check-system`) so the gate
# needs no editable install and survives worktree teardown — a stale ambient
# editable install pointing at a removed worktree must not break this gate.
CONTRACT_PYTHONPATH := $(CURDIR)/packages/workstate-protocol/src:$(CURDIR)/packages/mcp-workstate-handoff/src:$(CURDIR)/packages/mcp-workstate-orchestrator/src$(if $(PYTHONPATH),:$(PYTHONPATH))

.PHONY: test-contract
test-contract: ## Run the cross-package protocol contract test
	@cd packages/mcp-workstate-orchestrator && \
	    REQ='import pytest, fastmcp'; \
	    PY=$$(pwd)/.venv/bin/python; \
	    if test -x "$$PY" && "$$PY" -c "$$REQ" >/dev/null 2>&1; then :; \
	    else \
	        if test -x "$$PY"; then \
	            echo "test-contract: $$PY cannot import pytest+fastmcp (base-synced dev venv); trying pyenv python." >&2; \
	            echo "  Restore the preferred venv with: uv pip install --python \"$$PY\" 'pytest>=8' 'hypothesis>=6'" >&2; \
	            echo "  (do NOT 'uv sync --extra dev' here: the unpublished mcp-workstate-handoff sibling breaks the resolve)." >&2; \
	        fi; \
	        PY=python; \
	        "$$PY" -c "$$REQ" >/dev/null 2>&1 || { \
	            echo "test-contract: pyenv python ($$PY) also cannot import pytest+fastmcp; cannot run the contract test." >&2; \
	            echo "  Install dev test deps: uv pip install --python packages/mcp-workstate-orchestrator/.venv/bin/python 'pytest>=8' 'hypothesis>=6'" >&2; \
	            exit 1; \
	        }; \
	    fi; \
	    PYTHONPATH="$(CONTRACT_PYTHONPATH)" "$$PY" -m pytest tests/test_protocol_contract.py -q

# workstate-system owns no installable package — its suite pins the plugin,
# skill, and Make-target contracts and imports only `workstate_handoff_mcp` and
# `workstate_protocol`. Run it against the sibling `src/` trees via PYTHONPATH
# (mirroring CI's workstate-system job) so the gate needs no editable install;
# the subprocess-spawning plan-target tests inherit and prepend to PYTHONPATH,
# so `workstate_protocol` resolves inside those subprocesses too. Probe the
# pyenv project interpreter (it carries pytest) and fall back to python3.
# Override explicitly with `make check-system SYSTEM_PYTHON=/path/to/python`.
SYSTEM_PYTHON ?= $(shell \
  cand=$$(pyenv which python 2>/dev/null); \
  if [ -n "$$cand" ] && "$$cand" -c 'import pytest' >/dev/null 2>&1; then \
    echo "$$cand"; \
  else \
    command -v python3; \
  fi)
SYSTEM_PYTHONPATH := $(CURDIR)/packages/workstate-protocol/src:$(CURDIR)/packages/mcp-workstate-handoff/src$(if $(PYTHONPATH),:$(PYTHONPATH))

.PHONY: check-system
check-system: ## Run the workstate-system suite (plugin/skill/Make-target contracts)
	@PYTHONPATH="$(SYSTEM_PYTHONPATH)" $(SYSTEM_PYTHON) -m pytest packages/workstate-system/tests -q

.PHONY: check-mcp-pins
check-mcp-pins: ## Verify managed MCP-server uvx pins agree across both pin sites + the published version
	python scripts/check_mcp_pin_drift.py

.PHONY: mcp-pins-sync
mcp-pins-sync: ## Regenerate bootstrap _mcp_pins.py from the canonical mcp_servers.yaml (implementation note)
	python scripts/mcp_pins.py sync

.PHONY: mcp-pins-check
mcp-pins-check: ## Fail if bootstrap _mcp_pins.py drifts from the canonical mcp_servers.yaml
	python scripts/mcp_pins.py check

.PHONY: stack-pins-sync
stack-pins-sync: ## Regenerate workstate-stack exact pins from sibling pyproject versions
	python scripts/stack_pins.py sync

.PHONY: stack-pins-check
stack-pins-check: ## Fail if workstate-stack pins drift from sibling pyproject versions
	python scripts/stack_pins.py check

.PHONY: check-release-version-drift
check-release-version-drift: ## Fail if a publishable package's shipped payload changed since its version was set (no bump). Runs inside `make preflight`.
	python scripts/check_release_version_drift.py

.PHONY: check-overlay-drift
check-overlay-drift: ## Fail when root docs/workstate/{contracts,rules} drift from payload canon
	python scripts/check_overlay_drift.py

.PHONY: test-handoff check-handoff
test-handoff: ## Run mcp-workstate-handoff pytest (PYTEST_TARGETS= overrides default tests/)
	$(MAKE) -C packages/mcp-workstate-handoff test-handoff

check-handoff: ## Lint + mypy + tests for mcp-workstate-handoff
	$(MAKE) -C packages/mcp-workstate-handoff check-handoff

.PHONY: format-py
format-py: ## Auto-format the Python packages (ruff fix-lint + format); single source for format-all + format-check
	$(MAKE) -C packages/mcp-workstate-handoff format-handoff
	$(MAKE) -C packages/mcp-workstate-orchestrator format-orchestrator
	$(MAKE) -C packages/workstate-codex-bridge format-bridge

.PHONY: format-all
format-all: format-py ## Auto-format every package (Python via ruff + the canvas-web app via eslint)

.PHONY: format-check
format-check: ## CI gate: on a clean tree, fail if Python autoformatting (format-py) would change any tracked file (stops format drift accruing and sweeping into unrelated slices)
	@git diff --quiet || { \
		echo "format-check: working tree has uncommitted changes — commit or stash first." >&2; \
		echo "  (this gate runs the formatter and diffs against HEAD, so it needs a clean tree)." >&2; \
		exit 2; \
	}
	@$(MAKE) format-py
	@git diff --exit-code || { \
		echo "" >&2; \
		echo "format-check: FAIL — Python autoformatting changed tracked files (drift shown above)." >&2; \
		echo "  Fix: run 'make format-py' (or 'make format-all') and commit the result." >&2; \
		exit 1; \
	}
	@echo "format-check: OK — Python formatter is a no-op; no format drift."

.PHONY: check-all
check-all: ## Format + lint + mypy + tests across every package, then contract test
	$(MAKE) format-all
	$(MAKE) -C packages/mcp-workstate-handoff check-handoff
	$(MAKE) -C packages/mcp-workstate-orchestrator check-orchestrator
	$(MAKE) -C packages/workstate-codex-bridge check-bridge
	$(MAKE) check-system
	$(MAKE) check-overlay-drift
	$(MAKE) check-mcp-pins
	$(MAKE) mcp-pins-check
	$(MAKE) stack-pins-check
	$(MAKE) check-harness-coherence
	$(MAKE) check-harness-sync
	$(MAKE) test-contract

# internal: installed hook-surface coherence gate. Fails on any
# error-severity finding (config naming an unresolvable script; mixed-snapshot
# hook mounts); warnings (stale clone, hybrid receipt) stay green.
.PHONY: check-harness-coherence
check-harness-coherence: ensure-hook-surfaces ## Assess installed hook-surface coherence at the repo root
	uv run --project packages/workstate-bootstrap python -m workstate_bootstrap.coherence $(CURDIR)

.PHONY: check-harness-sync
check-harness-sync: plugins-build ## Verify rendered harness content matches harness-protocol.yaml
	uv run --project packages/workstate-system --with pyyaml python packages/workstate-system/scripts/check_harness_sync.py

.PHONY: test-rehearsal
test-rehearsal: ## Run the bootstrap install rehearsal test
	cd packages/workstate-bootstrap && python -m pytest tests/test_bootstrap_install_rehearsal.py -q

# implementation note S3: the git-hooks surface is the one repo-root path git forces
# (core.hooksPath cannot live inside packages/). Wire scripts/hooks as a tracked
# in-tree symlink into the co-located payload so a fresh clone self-wires with no
# bootstrap, then rewire core.hooksPath. Both steps are idempotent.
.PHONY: ensure-hooks-path
ensure-hooks-path: ensure-hook-surfaces ## Wire scripts/hooks + .github/hooks -> payload + rewire core.hooksPath
	@desired=scripts/hooks/git; \
	    current=$$(git config --get core.hooksPath 2>/dev/null || true); \
	    if [ "$$current" != "$$desired" ]; then \
	        git config core.hooksPath "$$desired"; \
	        echo "==> core.hooksPath: '$$current' -> '$$desired'"; \
	    fi

.PHONY: dogfood-link
dogfood-link: ## (Re)create the tracked repo-root git-hooks symlink into the payload
	@link=scripts/hooks; \
	    target=../packages/workstate-system/workstate_system/payload/scripts/hooks; \
	    if [ "$$(readlink "$$link" 2>/dev/null)" != "$$target" ]; then \
	        rm -rf "$$link"; \
	        ln -s "$$target" "$$link"; \
	        echo "==> linked $$link -> $$target"; \
	    fi

.PHONY: ensure-github-hooks-link
ensure-github-hooks-link: ## Symlink .github/hooks into the co-located payload (implementation note)
	@link=.github/hooks; \
	    target=../packages/workstate-system/workstate_system/payload/.github/hooks; \
	    if [ "$$(readlink "$$link" 2>/dev/null)" != "$$target" ]; then \
	        rm -rf "$$link"; \
	        mkdir -p .github; \
	        ln -s "$$target" "$$link"; \
	        echo "==> linked $$link -> $$target"; \
	    fi

.PHONY: ensure-hook-surfaces
ensure-hook-surfaces: dogfood-link ensure-github-hooks-link ## Wire scripts/hooks + .github/hooks to payload

# ----- release --------------------------------------------------------------

.PHONY: release-package
release-package: ## Release one package: make release-package PKG=<name>
	@test -n "$(PKG)" || { echo "PKG is required (e.g. PKG=workstate-protocol)"; exit 2; }
	$(RELEASE) package $(PKG) $(FLAGS)

.PHONY: release-prepare
release-prepare: ## Prepare one package release: make release-prepare PKG=<name> BUMP=patch|minor|major|X.Y.Z
	@test -n "$(PKG)" || { echo "PKG is required (e.g. PKG=workstate-protocol)"; exit 2; }
	@test -n "$(BUMP)" || { echo "BUMP is required (e.g. BUMP=patch)"; exit 2; }
	python scripts/release_prepare.py $(PKG) $(BUMP) $(FLAGS)

.PHONY: release-status
release-status: ## Show package tag/PyPI release state and the suggested next monorepo tag
	$(RELEASE) status $(FLAGS)

.PHONY: release-plan
release-plan: ## Show the computed release plan; pass FLAGS=--json for machine-readable output
	$(RELEASE) plan $(TAG) $(FLAGS)

.PHONY: check-release-manifest
check-release-manifest: ## Validate config/release/packages.json package paths and metadata
	python $(MANIFEST_HELPER) validate

.PHONY: check-release-workflow
check-release-workflow: ## Validate the Trusted Publishing runway workflow with actionlint
	@command -v actionlint >/dev/null 2>&1 || { echo "actionlint is required to validate .github/workflows/release-publish.yml"; exit 2; }
	actionlint .github/workflows/release-publish.yml


.PHONY: release-public
release-public: ## Orchestrate the public-release flow (dry-run by default); FLAGS=--execute to push/tag/publish after confirmation, verified publishers assumed by default (PROBE_PUBLISHERS=1 to force the live probe), FLAGS=--json for machine-readable output
	python scripts/release_public.py $(FLAGS) $(if $(PROBE_PUBLISHERS),--probe-publishers)

.PHONY: release-pending
release-pending: ## Release unpublished package versions and cut the next monorepo tag
	$(RELEASE) pending $(TAG) $(FLAGS)

.PHONY: release-all
release-all: ## Preflight + release all pending packages in dep order
	$(RELEASE) all $(FLAGS)

.PHONY: release-monorepo
release-monorepo: ## Cut the consumer-facing monorepo tag: make release-monorepo TAG=v0.1.3
	@test -n "$(TAG)" || { echo "TAG is required (e.g. TAG=v0.1.3)"; exit 2; }
	$(RELEASE) monorepo $(TAG) $(FLAGS)

.PHONY: dry-run-all
dry-run-all: ## Preview release-all without uploads or tag pushes
	$(RELEASE) --dry-run all $(FLAGS)

.PHONY: dry-run-pending
dry-run-pending: ## Preview release-pending without uploads or tag pushes
	$(RELEASE) --dry-run pending $(TAG) $(FLAGS)

.PHONY: dry-run-monorepo
dry-run-monorepo: ## Preview release-monorepo: make dry-run-monorepo TAG=v0.1.3
	@test -n "$(TAG)" || { echo "TAG is required (e.g. TAG=v0.1.3)"; exit 2; }
	$(RELEASE) --dry-run monorepo $(TAG) $(FLAGS)

# ----- housekeeping ---------------------------------------------------------

.PHONY: clean
clean: ## Remove all packages/*/dist build artifacts
	@for pkg in $(PACKAGES); do rm -rf packages/$$pkg/dist; done
	@echo "cleaned $(PACKAGES:%=packages/%/dist)"

.PHONY: versions
versions: ## Print each package's pyproject version
	@for pkg in $(PACKAGES); do \
	    v=$$(grep -m1 '^version' packages/$$pkg/pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/'); \
	    printf "  %-26s %s\n" "$$pkg" "$$v"; \
	done

.PHONY: tags
tags: ## List release-related tags on origin
	@git ls-remote --tags origin | awk '{print $$2}' | sed 's|refs/tags/||' | grep -E '^(v[0-9]|.+-v[0-9])' | sort -V

.PHONY: smoke
smoke: ## One-shot smoke install of the latest monorepo tag into /tmp
	@latest=$$(git tag -l 'v[0-9]*' | sort -V | tail -1); \
	test -n "$$latest" || { echo "no v* monorepo tag found"; exit 1; }; \
	dir=/tmp/workstate-smoke-$$$$-$$(date +%s); \
	echo "==> smoke testing $$latest in $$dir"; \
	mkdir -p "$$dir" && cd "$$dir" && git init -q && \
	uvx --from "git+https://github.com/darce/workstate@$$latest#subdirectory=packages/workstate-bootstrap" \
	    workstate-bootstrap install --target "$$dir" --remote-ref "$$latest"

# The dogfood target deterministically installs the just-released monorepo
# overlay back into this same repo (the monorepo eating its own release).
# Auto-stashes any dirty state in the vendored .workstate/remote/ snapshot
# clone, since that path is bootstrap-managed and not a dev surface.
# Override the tag with `make dogfood TAG=v0.1.42`.
# Override the source branch with
# `make dogfood DOGFOOD_REMOTE_URL=<private-monorepo-remote> DOGFOOD_REF=main`.
# Install from the just-published package delivery path with
# `make dogfood DOGFOOD_SOURCE=package`. Override package specs with e.g.
# `DOGFOOD_BOOTSTRAP_SPEC=workstate-bootstrap==0.7.3`
# `DOGFOOD_SYSTEM_SPEC=workstate-system==0.1.3`.
# Opt into harness Stop adapters (any mode) with e.g.
# `make dogfood DOGFOOD_INSTALL_FLAGS=--install-claude-stop-hook-local`.
.PHONY: dogfood
dogfood: ## Install latest/TAG overlay or DOGFOOD_SOURCE=package PyPI overlay into this repo
	@source="$(DOGFOOD_SOURCE)"; \
	if [ "$$source" = "package" ]; then \
	    bootstrap_spec="$(DOGFOOD_BOOTSTRAP_SPEC)"; \
	    system_spec="$(DOGFOOD_SYSTEM_SPEC)"; \
	    uvx_flags="$(DOGFOOD_UVX_FLAGS)"; \
	    echo "==> dogfood installing package overlay into $(CURDIR)"; \
	    echo "==> using $$bootstrap_spec with $$system_spec"; \
	    uvx $$uvx_flags --from "$$bootstrap_spec" --with "$$system_spec" \
	        workstate-bootstrap install --source package --target "$(CURDIR)" $(DOGFOOD_INSTALL_FLAGS) && \
	    uvx $$uvx_flags --from "$$bootstrap_spec" --with "$$system_spec" \
	        workstate-bootstrap status --target "$(CURDIR)"; \
	    exit $$?; \
	fi; \
	if [ "$$source" = "worktree" ]; then \
	    echo "==> dogfood installing worktree overlay into $(CURDIR)"; \
	    uv run --project packages/workstate-bootstrap workstate-bootstrap install --source worktree --target "$(CURDIR)" $(DOGFOOD_INSTALL_FLAGS) && \
	    uv run --project packages/workstate-bootstrap workstate-bootstrap status --target "$(CURDIR)"; \
	    exit $$?; \
	fi; \
	if [ "$$source" != "git_overlay" ]; then \
	    echo "DOGFOOD_SOURCE must be 'git_overlay', 'package', or 'worktree' (got '$$source')" >&2; \
	    exit 2; \
	fi; \
	remote_url="$(DOGFOOD_REMOTE_URL)"; \
	ref="$(DOGFOOD_REF)"; \
	tag="$(TAG)"; \
	if [ -n "$$ref" ]; then \
	    tag="$$ref"; \
	fi; \
	if [ -z "$$tag" ]; then \
	    tag=$$(git tag -l 'v[0-9]*' | sort -V | tail -1); \
	    test -n "$$tag" || { echo "no v* monorepo tag found"; exit 1; }; \
	fi; \
	clone=.workstate/remote; \
	if [ -d "$$clone/.git" ]; then \
	    if ! git -C "$$clone" diff --quiet || ! git -C "$$clone" diff --cached --quiet; then \
	        ts=$$(date -u +%Y%m%dT%H%M%SZ); \
	        echo "==> stashing dirty state in $$clone (pre-dogfood-$$tag-$$ts)"; \
	        git -C "$$clone" stash push -u -m "pre-dogfood-$$tag-$$ts" >/dev/null; \
	    fi; \
	fi; \
	echo "==> dogfood installing $$tag into $(CURDIR)"; \
	if [ -n "$$remote_url" ]; then \
	    echo "==> using remote $$remote_url"; \
	    uv run --project packages/workstate-bootstrap workstate-bootstrap install --target "$(CURDIR)" --remote-url "$$remote_url" --remote-ref "$$tag" $(DOGFOOD_INSTALL_FLAGS) && \
	    uv run --project packages/workstate-bootstrap workstate-bootstrap status --target "$(CURDIR)"; \
	else \
	    uv run --project packages/workstate-bootstrap workstate-bootstrap install --target "$(CURDIR)" --remote-ref "$$tag" $(DOGFOOD_INSTALL_FLAGS) && \
	    uv run --project packages/workstate-bootstrap workstate-bootstrap status --target "$(CURDIR)"; \
	fi

# >>> WORKSTATE_BOOTSTRAP LIFECYCLE INCLUDE >>>
ifeq ($(wildcard packages/workstate-system/Makefile.d/*.mk),)
-include Makefile.d/*.mk
endif
# <<< WORKSTATE_BOOTSTRAP LIFECYCLE INCLUDE <<<
