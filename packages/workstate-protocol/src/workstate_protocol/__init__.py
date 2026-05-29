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
from .skills import SkillManifest, SkillScope

__version__ = "0.1.5"

__all__ = [
    "ActiveTask",
    "BootstrapManifest",
    "DecisionRef",
    "HandoffState",
    "HandoffStatus",
    "OverlayConfigEntry",
    "OverlaySurface",
    "PostToolUseEvent",
    "PreToolUseEvent",
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
    "format_suggested_branch_name",
    "resolve_env_alias",
]
