"""workstate-protocol: typed cross-repo contracts for the Workstate system.

Pydantic v2 models that consumer packages (mcp-workstate-handoff,
mcp-workstate-orchestrator, workstate-bootstrap, workstate-system) import to
guarantee wire-level compatibility across out-of-process boundaries.
"""

from __future__ import annotations

from . import branch_naming as branch_naming  # re-exported submodule
from .bootstrap import BootstrapManifest, OverlayConfigEntry, OverlaySurface
from .branch_naming import (
    TASK_REF_RE,
    derive_task_ref_candidates,
    format_suggested_branch_name,
)
from .compaction import DecisionRef, StructuredSummary, TurnRange
from .env_aliases import resolve_env_alias
from .handoff import (
    ActiveTask,
    HandoffState,
    HandoffStatus,
    TargetWorktree,
    TaskPlanRef,
    TaskPlanResolution,
    TaskRef,
)
from .hooks import (
    PostToolUseEvent,
    PreToolUseEvent,
    SessionStartEvent,
    StopEvent,
    UserPromptSubmitEvent,
)
from .paths import (
    CONTRACTS_DIR,
    DOCS_MIRROR_DIR,
    HARNESS_CONTRACT_RELPATH,
    INSTRUCTIONS_RELPATH,
    LEGACY_DOCS_MIRROR_DIR,
    LEGACY_RUNTIME_ROOT_DIRNAME,
    RULES_DIR,
    RUNTIME_PATH_RENAMES,
    RUNTIME_ROOT_DIRNAME,
    docs_mirror_path,
    runtime_root_path,
)
from .skills import SkillManifest, SkillScope

__version__ = "0.1.6"

__all__ = [
    "ActiveTask",
    "BootstrapManifest",
    "CONTRACTS_DIR",
    "DOCS_MIRROR_DIR",
    "DecisionRef",
    "HARNESS_CONTRACT_RELPATH",
    "HandoffState",
    "HandoffStatus",
    "INSTRUCTIONS_RELPATH",
    "LEGACY_DOCS_MIRROR_DIR",
    "LEGACY_RUNTIME_ROOT_DIRNAME",
    "OverlayConfigEntry",
    "OverlaySurface",
    "PostToolUseEvent",
    "PreToolUseEvent",
    "RULES_DIR",
    "RUNTIME_PATH_RENAMES",
    "RUNTIME_ROOT_DIRNAME",
    "SessionStartEvent",
    "SkillManifest",
    "SkillScope",
    "StopEvent",
    "StructuredSummary",
    "TASK_REF_RE",
    "TargetWorktree",
    "TaskPlanRef",
    "TaskPlanResolution",
    "TaskRef",
    "TurnRange",
    "UserPromptSubmitEvent",
    "__version__",
    "branch_naming",
    "derive_task_ref_candidates",
    "docs_mirror_path",
    "format_suggested_branch_name",
    "resolve_env_alias",
    "runtime_root_path",
]
