"""Child-agent run prompt and gateway child-session startup.

This module is the narrow orchestration seam for issue #6: once a parent start
selects one runnable child issue, build a self-contained prompt for a fresh
Hermes child session and ask the Hermes gateway child-session API to run it in a
Discord child thread. GitHub, Discord, and Hermes core remain injected so the
behavior is unit-testable without live services.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any

from .branch_prep import BranchPreparationPlan
from .selection import GitHubIssue, IssueKey


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass(frozen=True)
class ChildRunPlan:
    """Prepared prompt and metadata for one child issue attempt."""

    parent: IssueKey
    child: GitHubIssue
    branch_name: str
    base_branch: str
    pr_base: str
    idempotency_key: str
    title: str
    prompt: str
    branch_preparation: BranchPreparationPlan | None = None
    attempt: int = 1


@dataclass(frozen=True)
class ChildRunStart:
    """Result of scheduling a child issue through Hermes core."""

    plan: ChildRunPlan
    result: Any


def issue_branch_slug(title: str, *, max_words: int = 7) -> str:
    """Return a short deterministic branch slug from a GitHub issue title."""

    words = re.findall(r"[a-z0-9]+", (title or "").lower())[:max_words]
    return "-".join(words) or "issue"


def issue_branch_name(child: GitHubIssue, *, attempt: int = 1) -> str:
    """Return the deterministic branch name for a child issue attempt."""

    branch = f"feat/issue-{child.number}-{issue_branch_slug(child.title)}"
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    if attempt == 1:
        return branch
    if attempt == 2:
        return f"{branch}-retry"
    return f"{branch}-retry-{attempt - 1}"


def _branch_setup_text(
    *,
    branch: str,
    base_branch: str,
    pr_base: str,
    branch_preparation: BranchPreparationPlan | None,
) -> str:
    if branch_preparation:
        command_lines = "\n".join(f"  - `{' '.join(command)}`" for command in branch_preparation.git_commands)
        if not branch_preparation.has_dependencies:
            return f"""Dependency-aware branch preparation:
- This child has no unsatisfied dependency branches in its `## Blocked by` section.
- Start from latest `{branch_preparation.base_branch}` and keep the pull request base as `{branch_preparation.pr_base}`.
- Required git command plan:
{command_lines}
- Create `{branch_preparation.child_branch}` from the prepared `{branch_preparation.base_branch}` before implementation."""

        dependency_lines = "\n".join(
            f"- {dependency.issue.ref}: `{dependency.branch}` covers "
            f"{', '.join(key.ref for key in dependency.covers) or dependency.issue.ref}"
            for dependency in branch_preparation.dependency_branches
        )
        rebase_lines = (
            "\n".join(
                f"- rebase `{dependency.branch}` into `{branch}`"
                for dependency in branch_preparation.additional_rebase_branches
            )
            or "- none; chosen base already contains all known direct dependencies"
        )
        return f"""Dependency-aware branch preparation:
- Dependency base branch: `{branch_preparation.base_branch}`
- Pull request base remains: `{branch_preparation.pr_base}`
- Dependency branches considered:
{dependency_lines}
- Additional rebase operations on the new child branch only:
{rebase_lines}
- Required git command plan:
{command_lines}"""

    return f"""Dependency-aware branch preparation:
- This child has no unsatisfied dependency branches in its `## Blocked by` section.
- Start from latest `{base_branch}` and keep the pull request base as `{pr_base}`.
- Required git command plan:
  - `git fetch origin`
  - `git checkout {base_branch}`
  - `git pull --ff-only origin {base_branch}`
  - `git checkout -B {branch} {base_branch}`
- Create `{branch}` from the freshly prepared `{base_branch}` before implementation."""


def build_auditable_child_prompt(
    *,
    parent: IssueKey,
    child: GitHubIssue,
    branch_name: str | None = None,
    base_branch: str = "main",
    pr_base: str = "main",
    branch_preparation: BranchPreparationPlan | None = None,
) -> str:
    """Build the self-contained prompt given to the fresh child agent.

    The prompt intentionally carries the full issue body and all lifecycle
    requirements so the child session does not depend on controller context.
    """

    branch = branch_name or issue_branch_name(child)
    issue_url = f"https://github.com/{child.repository}/issues/{child.number}"
    parent_url = f"https://github.com/{parent.repository}/issues/{parent.number}"
    branch_setup = _branch_setup_text(
        branch=branch,
        base_branch=base_branch,
        pr_base=pr_base,
        branch_preparation=branch_preparation,
    )
    return f"""Use the `auditable-implementation-review-loop` skill to implement this GitHub child issue end-to-end.

Repository: {child.repository}
Parent issue: {parent.ref} ({parent_url})
Selected child issue: {child.ref} — {child.title}
Child issue URL: {issue_url}
Base branch to pull before starting: {base_branch}
Implementation branch to create: {branch}
Pull request base: {pr_base}

{branch_setup}

Issue body:
{child.body.strip() or "(No issue body provided.)"}

Required process:
1. Load and follow `auditable-implementation-review-loop` exactly, including supporting GitHub/subagent skills.
2. Before implementation work begins, execute the dependency-aware branch preparation command plan above. Do not work from stale branches.
3. Mutate only the new child branch `{branch}` while combining dependency code; do not retarget, rebase, or otherwise mutate dependency PR branches or already-created PRs.
4. Implement only this child issue's acceptance criteria and commit the initial implementation with a conventional commit.
5. After the first implementation commit exists, push `{branch}` and open a draft PR targeting `{pr_base}`. Use `Fixes #{child.number}` in the PR body.
6. Run the auditable automatic implementation review loop with a separate holistic reviewer. Commit every review-driven fix separately and re-review until the final verdict is `APPROVED`.
7. If the review loop returns `REQUEST_CHANGES` or otherwise fails and cannot be fixed, stop in this child thread. Do not mark the issue done, do not mark the PR ready, and do not advance the parent runner.
8. Only after the final automatic review verdict is `APPROVED`, mark the draft PR ready for human review.
9. On success, transition the child issue label state from `agent:in-progress` to `agent:done` while leaving `ready-for-agent` intact.
10. Return a concise summary with PR URL, commits, verification output, and final reviewer verdict.

Safety boundaries:
- Do not merge PRs.
- Do not modify unrelated parent sub-issues.
- Do not close this child issue manually; the PR should close it through `Fixes #{child.number}` when merged.
- If branch preparation, a dependency rebase, or another git command fails, pause and report the real output instead of guessing through conflicts.
- If a blocking tool, git, GitHub, or test failure occurs, report the real output in this thread instead of inventing success.
"""


def prepare_child_run(
    *,
    parent: IssueKey,
    child: GitHubIssue,
    base_branch: str = "main",
    pr_base: str = "main",
    branch_preparation: BranchPreparationPlan | None = None,
    attempt: int = 1,
) -> ChildRunPlan:
    """Prepare deterministic child-run metadata and prompt."""

    branch = issue_branch_name(child, attempt=attempt)
    if branch_preparation is not None:
        base_branch = branch_preparation.base_branch
        pr_base = branch_preparation.pr_base
    title = f"Issue #{child.number}: {child.title}" if child.title else f"Issue #{child.number}"
    return ChildRunPlan(
        parent=parent,
        child=child,
        branch_name=branch,
        base_branch=base_branch,
        pr_base=pr_base,
        idempotency_key=f"github:{child.repository}/issues/{child.number}:attempt:{attempt}",
        title=title,
        prompt=build_auditable_child_prompt(
            parent=parent,
            child=child,
            branch_name=branch,
            base_branch=base_branch,
            pr_base=pr_base,
            branch_preparation=branch_preparation,
        ),
        branch_preparation=branch_preparation,
        attempt=attempt,
    )


def _request_payload(event: Any, plan: ChildRunPlan) -> dict[str, Any]:
    return {
        "parent_event": event,
        "child_title": plan.title,
        "starter_prompt": plan.prompt,
        "idempotency_key": plan.idempotency_key,
        "metadata": {
            "source": "hermes-issue-runner",
            "parent_issue": plan.parent.ref,
            "child_issue": plan.child.ref,
            "branch": plan.branch_name,
            "base_branch": plan.base_branch,
            "pr_base": plan.pr_base,
            "attempt": plan.attempt,
        },
    }


def _coerce_child_session_request(gateway: Any, payload: dict[str, Any]) -> Any:
    """Return the object expected by Hermes core's child-session seam.

    Hermes core now validates that ``start_child_session`` receives an actual
    ``GatewayChildSessionRequest`` instance, not a plain dict. Keep the older
    injected factory hook for tests/adapters, but construct the public dataclass
    directly when it is importable in the gateway process.
    """

    factory = getattr(gateway, "child_session_request_factory", None)
    if callable(factory):
        return factory(**payload)

    try:
        from gateway.child_session import GatewayChildSessionRequest
    except Exception:
        # Unit tests and older Hermes cores may not have the public seam type on
        # sys.path. Preserve the old dict fallback for those environments.
        return payload

    return GatewayChildSessionRequest(**payload)


async def start_child_issue_session(
    *,
    gateway: Any,
    event: Any,
    parent: IssueKey,
    child: GitHubIssue,
    base_branch: str = "main",
    pr_base: str = "main",
    branch_preparation: BranchPreparationPlan | None = None,
    attempt: int = 1,
) -> ChildRunStart:
    """Start a gateway-native child session for one selected issue.

    The preferred seam is ``gateway.start_child_session(request)``. Tests or adapters
    may also expose ``gateway.child_session_request_factory`` to coerce the
    dictionary payload into Hermes core's request dataclass.
    """

    starter = getattr(gateway, "start_child_session", None)
    if not callable(starter):
        raise TypeError("gateway must expose start_child_session(request) to run a child issue")

    plan = prepare_child_run(
        parent=parent,
        child=child,
        base_branch=base_branch,
        pr_base=pr_base,
        branch_preparation=branch_preparation,
        attempt=attempt,
    )
    payload = _request_payload(event, plan)
    request = _coerce_child_session_request(gateway, payload)
    result = await _maybe_await(starter(request))
    return ChildRunStart(plan=plan, result=result)
