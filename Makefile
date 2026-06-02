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
#   make release-pending                    # release only unpublished package versions + cut next monorepo tag
#   make release-prepare PKG=workstate-protocol BUMP=patch
#   make release-package PKG=workstate-protocol
#   make release-all
#   make release-monorepo TAG=v0.1.3
#   make dry-run-all                        # show what release-all would do
#   make dry-run-pending                    # preview release-pending without uploads or tag pushes
#   make release-all FLAGS=--skip-tests     # pass-through flag to release.sh
#
# Variables:
#   PKG     — package directory under packages/ for release-package
#   TAG     — monorepo tag (vX.Y.Z) for release-monorepo
#   FLAGS   — extra flags forwarded to scripts/release.sh

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c

MANIFEST_HELPER := scripts/release_manifest.py
PACKAGES := $(shell python $(MANIFEST_HELPER) list --field name)
RELEASE_PACKAGES := $(shell python $(MANIFEST_HELPER) list --release-only --field name)
RELEASE  := scripts/release.sh
FLAGS    ?=

# Wire the lifecycle.mk `make format` target (the hoist-safe entry
# point referenced by the branch-lifecycle skill) to this monorepo's
# `format-all` walker. Bootstrap consumers without a `format-all`
# target override this in their own Makefile (or accept the loud no-op
# default that lifecycle.mk ships).
LIFECYCLE_FORMATTER := $(MAKE) format-all

# Pull in package-owned Make fragments, including the lifecycle target family
# via packages/workstate-system/Makefile.d/.
# Use `-include` so a missing fragment never blocks the root `Makefile`.
-include packages/workstate-system/Makefile.d/*.mk

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
	    PY=$$(pwd)/.venv/bin/python; \
	    test -x "$$PY" || PY=python; \
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

.PHONY: format-all
format-all: ## Auto-format every package (ruff format + fix-lint)
	$(MAKE) -C packages/mcp-workstate-handoff format-handoff
	$(MAKE) -C packages/mcp-workstate-orchestrator format-orchestrator
	$(MAKE) -C packages/workstate-codex-bridge format-bridge

.PHONY: check-all
check-all: ## Format + lint + mypy + tests across every package, then contract test
	$(MAKE) format-all
	$(MAKE) -C packages/mcp-workstate-handoff check-handoff
	$(MAKE) -C packages/mcp-workstate-orchestrator check-orchestrator
	$(MAKE) -C packages/workstate-codex-bridge check-bridge
	$(MAKE) check-system
	$(MAKE) check-mcp-pins
	$(MAKE) test-contract

.PHONY: test-rehearsal
test-rehearsal: ## Run the bootstrap install rehearsal test
	cd packages/workstate-bootstrap && python -m pytest tests/test_bootstrap_install_rehearsal.py -q

.PHONY: ensure-hooks-path
ensure-hooks-path: ## Rewire core.hooksPath to scripts/hooks/git
	@desired=scripts/hooks/git; \
	    current=$$(git config --get core.hooksPath 2>/dev/null || true); \
	    if [ "$$current" != "$$desired" ]; then \
	        git config core.hooksPath "$$desired"; \
	        echo "==> core.hooksPath: '$$current' -> '$$desired'"; \
	    fi

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
release-public: ## Orchestrate the public-release flow (dry-run by default); pass FLAGS=--execute to push/tag/publish after confirmation, FLAGS=--json for machine-readable output
	python scripts/release_public.py $(FLAGS)

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
.PHONY: dogfood
dogfood: ## Install the latest (or TAG=) monorepo overlay into this same repo
	@remote_url="$(DOGFOOD_REMOTE_URL)"; \
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
	    uv run --project packages/workstate-bootstrap workstate-bootstrap install --target "$(CURDIR)" --remote-url "$$remote_url" --remote-ref "$$tag" && \
	    uv run --project packages/workstate-bootstrap workstate-bootstrap status --target "$(CURDIR)"; \
	else \
	    uv run --project packages/workstate-bootstrap workstate-bootstrap install --target "$(CURDIR)" --remote-ref "$$tag" && \
	    uv run --project packages/workstate-bootstrap workstate-bootstrap status --target "$(CURDIR)"; \
	fi

# >>> WORKSTATE_BOOTSTRAP LIFECYCLE INCLUDE >>>
ifeq ($(wildcard packages/workstate-system/Makefile.d/*.mk),)
-include Makefile.d/*.mk
endif
# <<< WORKSTATE_BOOTSTRAP LIFECYCLE INCLUDE <<<
