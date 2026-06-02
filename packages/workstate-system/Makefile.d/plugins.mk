# Makefile.d/plugins.mk — WORKSTATE-REF-01 plugin-tree emission targets.
#
# `plugins-build`  emits Claude + Codex plugin trees under the same
#                  bootstrap-generated base root used by marketplace pins:
#                  .workstate/generated/plugins/workstate-system/base/
#                  from the canonical inputs
#                  (skills/, config/agent-workflows/portable_commands.json,
#                  config/agent-workflows/mcp_servers.yaml).
# `plugins-check`  re-runs the generator with --check against the same
#                  destination and exits non-zero on drift.
#
# Path resolution mirrors workflows.mk so the fragment works both in the
# monorepo source layout and when hoisted into a consumer repo.

PLUGINS_MK_DIR        := $(dir $(lastword $(MAKEFILE_LIST)))
PLUGINS_ROOT          := $(abspath $(PLUGINS_MK_DIR)..)
ifeq ($(notdir $(PLUGINS_ROOT)),workstate-system)
PLUGINS_TARGET_ROOT   := $(abspath $(PLUGINS_ROOT)/../..)
else
PLUGINS_TARGET_ROOT   := $(PLUGINS_ROOT)
endif
# Reuse the PyYAML-aware interpreter probe from workflows.mk by sharing
# the WORKFLOWS_PYTHON variable name; if workflows.mk has not been
# included yet, repeat the same probe locally so plugins-* targets can be
# invoked standalone.
WORKFLOWS_PYTHON      ?= $(shell \
  cand=$$(pyenv which python 2>/dev/null); \
  if [ -n "$$cand" ] && "$$cand" -c 'import yaml' >/dev/null 2>&1; then \
    echo "$$cand"; \
  else \
    command -v python3; \
  fi)
PLUGINS_GENERATOR     := $(abspath $(PLUGINS_MK_DIR)../scripts/generate_agent_workflows.py)
PLUGINS_DIST_ROOT     ?= $(PLUGINS_TARGET_ROOT)/.workstate/generated/plugins/workstate-system/base

.PHONY: plugins-build plugins-check

plugins-build: ## Emit Claude + Codex plugin trees under the bootstrap base plugin root
	@$(WORKFLOWS_PYTHON) $(PLUGINS_GENERATOR) --mode=plugin --plugin-out "$(PLUGINS_DIST_ROOT)"

plugins-check: ## Verify plugin trees match the canonical inputs
	@$(WORKFLOWS_PYTHON) $(PLUGINS_GENERATOR) --mode=plugin --plugin-out "$(PLUGINS_DIST_ROOT)" --check
