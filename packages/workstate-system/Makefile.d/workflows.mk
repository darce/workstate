# Makefile.d/workflows.mk — workflow generation + facade lint targets.
#
# Path resolution mirrors lifecycle.mk so the fragment works both in the
# monorepo source layout and when hoisted into a consumer repo.

WORKFLOWS_MK_DIR      := $(dir $(lastword $(MAKEFILE_LIST)))
WORKFLOWS_ROOT        := $(abspath $(WORKFLOWS_MK_DIR)..)
# The generator + facade lint scripts depend on PyYAML, which the bare system
# `python3` typically lacks. Probe the pyenv-managed project interpreter first
# and verify it actually imports `yaml`; fall back to `python3` only if it does
# not. This keeps `make check-agent-workflows` working when pyenv is installed
# but the project Python has not been built/synced (and therefore lacks PyYAML).
# Operators can still override explicitly:
#   make check-agent-workflows WORKFLOWS_PYTHON=/path/to/python
WORKFLOWS_PYTHON      ?= $(shell \
  cand=$$(pyenv which python 2>/dev/null); \
  if [ -n "$$cand" ] && "$$cand" -c 'import yaml' >/dev/null 2>&1; then \
    echo "$$cand"; \
  else \
    command -v python3; \
  fi)
WORKFLOW_GENERATOR    := $(abspath $(WORKFLOWS_MK_DIR)../scripts/generate_agent_workflows.py)
WORKFLOW_FACADE_CHECK := $(abspath $(WORKFLOWS_MK_DIR)../scripts/check_workflow_facade.py)
SETTINGS_PIN_CHECK    := $(abspath $(WORKFLOWS_MK_DIR)../scripts/validate_claude_settings_pin.py)
SETTINGS_PIN_FILE     := $(abspath $(WORKFLOWS_MK_DIR)../../../.claude/settings.json)
WORKFLOW_TARGET_ROOT  ?=
WORKFLOW_TARGET_ARG   := $(if $(WORKFLOW_TARGET_ROOT),--target "$(WORKFLOW_TARGET_ROOT)")

.PHONY: generate-agent-workflows check-agent-workflows check-claude-settings-pin

generate-agent-workflows: ## Regenerate Claude, VS Code, and Codex workflow adapters
	@$(WORKFLOWS_PYTHON) $(WORKFLOW_GENERATOR) $(WORKFLOW_TARGET_ARG)

check-claude-settings-pin: ## Validate .claude/settings.json source discriminator + path + enabledPlugins
	@$(WORKFLOWS_PYTHON) $(SETTINGS_PIN_CHECK) "$(SETTINGS_PIN_FILE)"

check-agent-workflows: check-claude-settings-pin ## Verify generated workflow adapters and source cold-start facade guidance
	@$(WORKFLOWS_PYTHON) $(WORKFLOW_GENERATOR) $(WORKFLOW_TARGET_ARG) --check
	@$(WORKFLOWS_PYTHON) $(WORKFLOW_FACADE_CHECK) --root "$(WORKFLOWS_ROOT)"