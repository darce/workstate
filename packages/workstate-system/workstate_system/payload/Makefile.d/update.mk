# Workstate one-shot stack update surface (implementation note implementation note; managed payload).
# `make workstate-update` upgrades the workstate-stack meta-package in the
# runtime owning workstate-bootstrap, refreshes the overlay from its recorded
# source, runs doctor, and prints the stack version table.
#
#   REMOTE_REF=<tag>            git_overlay consumers: ref to update to
#   WORKSTATE_UPDATE_DRY_RUN=1  preview every mutating step without running it

.PHONY: workstate-update
workstate-update: ## Upgrade the workstate stack (one version anchor) + refresh overlay + doctor
	@sh scripts/workstate/update.sh
