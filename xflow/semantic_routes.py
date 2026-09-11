from __future__ import annotations

from typing import TYPE_CHECKING, Literal


if TYPE_CHECKING:
    from .task_state import TaskState


SemanticReference = Literal["contract-acceptance", "gap-recognition"]

CLASSIFICATION_PHASES = {
    "capability-change": frozenset(
        {
            "none",
            "discovery",
            "classified",
            "declaring",
            "accepted-design",
            "verification-designed",
            "projected",
        }
    ),
    "implementation-gap": frozenset(
        {
            "none",
            "discovery",
            "classified",
            "gap-analysis",
            "gap-recognized",
        }
    ),
    "ui-defect": frozenset({"none", "discovery", "classified"}),
    "infrastructure": frozenset({"none", "discovery", "classified"}),
    "governance": frozenset({"none", "discovery", "classified"}),
    "future": frozenset({"none", "discovery", "classified"}),
}

EXECUTION_REQUIRES_SEMANTIC_EXIT = {
    "S0_REQUEST": False,
    "S1_LOCAL_ISSUE_DRAFT": False,
    "S2_REMOTE_ISSUE_CREATED": False,
    "S3_TASK_BRANCH_STARTED": False,
    "S4_TDD_AND_IMPLEMENTATION": True,
    "S5_LOCAL_VERIFICATION": True,
    "S6_PREPARE_COMMIT_AND_MR_DRAFT": True,
    "S7_PUSH_BRANCH": True,
    "S8_CREATE_REMOTE_MR": True,
    "S9_REMOTE_REVIEW_AND_CI": True,
    "S10_DONE": True,
}

ACTION_POLICIES = {
    "current-task": "execution",
    "task-status": "execution",
    "issue-create": "none",
    "issue-comment": "execution",
    "contract-acceptance": "none",
    "gap-recognition": "none",
    "task-branch-start": "none",
    "task-contract-relocate": "none",
    "development": "exit",
    "commit": "exit",
    "git-push": "exit",
    "git-mr": "exit",
    "git-pr-merge": "exit",
    "issue-close": "exit",
    "git-cleanup": "exit",
    "git-cleanup-force": "exit",
    "trace-closure": "exit",
}

CAPABILITY_EXIT_PHASES = frozenset({"accepted-design", "verification-designed", "projected"})
GAP_EXIT_PHASES = frozenset({"gap-recognized"})


def validate_classification_phase(classification: str, semantic_phase: str) -> None:
    phases = CLASSIFICATION_PHASES.get(classification)
    if phases is None or semantic_phase not in phases:
        raise ValueError(
            f"invalid semantic route: Classification {classification} cannot use Semantic Phase {semantic_phase}"
        )


def semantic_reference_kind(classification: str, semantic_phase: str) -> SemanticReference | None:
    validate_classification_phase(classification, semantic_phase)
    if classification == "capability-change" and semantic_phase in CAPABILITY_EXIT_PHASES:
        return "contract-acceptance"
    if classification == "implementation-gap" and semantic_phase in GAP_EXIT_PHASES:
        return "gap-recognition"
    return None


def require_route_semantics(state: TaskState, action: str) -> None:
    validate_classification_phase(state.classification, state.semantic_phase)
    policy = ACTION_POLICIES.get(action)
    if policy is None:
        raise ValueError(f"unknown semantic route action: {action}")
    requires_exit = policy == "exit" or (
        policy == "execution" and EXECUTION_REQUIRES_SEMANTIC_EXIT[state.execution_state]
    )
    if not requires_exit:
        return
    if state.contract == "legacy.current-task@0.1.0" and state.contract_file == ".xflow/current-task.md":
        return
    if state.classification == "capability-change" and state.semantic_phase not in CAPABILITY_EXIT_PHASES:
        raise ValueError(
            f"capability-change requires accepted-design before {action}; found {state.semantic_phase}"
        )
    if state.classification == "implementation-gap" and state.semantic_phase not in GAP_EXIT_PHASES:
        raise ValueError(
            f"implementation-gap requires gap-recognized before {action}; found {state.semantic_phase}"
        )
