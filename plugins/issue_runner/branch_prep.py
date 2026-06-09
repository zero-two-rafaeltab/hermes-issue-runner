"""Dependency-aware git branch preparation for child issue attempts.

The runner keeps GitHub as durable state, but branch preparation needs a small
unit-testable planning seam.  This module derives dependency branches from child
issue blockers, chooses the best dependency base using the issue graph first, and
returns the git operations that should be run against the new child branch only.
Live git/GitHub adapters are optional and injected; tests can use simple fakes.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field, replace
from typing import Any

from .selection import GitHubIssue, IssueKey, coerce_github_issue, parse_blockers

MAIN_BRANCH = "main"


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass(frozen=True)
class DependencyBranch:
    """Git branch associated with a dependency issue."""

    issue: IssueKey
    branch: str
    covers: tuple[IssueKey, ...] = ()


@dataclass(frozen=True)
class BranchPreparationPlan:
    """Concrete branch-preparation plan for a child issue attempt."""

    child_branch: str
    pr_base: str = MAIN_BRANCH
    base_branch: str = MAIN_BRANCH
    dependency_branches: tuple[DependencyBranch, ...] = ()
    additional_rebase_branches: tuple[DependencyBranch, ...] = ()
    git_commands: tuple[tuple[str, ...], ...] = field(default_factory=tuple)

    @property
    def has_dependencies(self) -> bool:
        return bool(self.dependency_branches)

    @property
    def additional_rebase_count(self) -> int:
        return len(self.additional_rebase_branches)


@dataclass(frozen=True)
class BranchPreparationFailure(Exception):
    """Explicit pause-worthy branch preparation failure."""

    message: str
    command: tuple[str, ...] | None = None

    def __str__(self) -> str:
        if self.command:
            return f"{self.message}: {' '.join(self.command)}"
        return self.message


def _default_branch_name(issue: IssueKey) -> str:
    return f"feat/issue-{issue.number}"


async def _get_issue(github_client: Any, key: IssueKey) -> GitHubIssue:
    getter = getattr(github_client, "get_issue", None)
    if not callable(getter):
        raise TypeError("github_client must expose get_issue(owner, repo, number) for dependency graph lookups")
    return coerce_github_issue(key.owner, key.repo, await _maybe_await(getter(key.owner, key.repo, key.number)))


async def _dependency_branch_for_issue(github_client: Any, key: IssueKey) -> str:
    """Resolve a dependency issue to its implementation branch.

    Adapters may expose a direct branch lookup.  If unavailable, the deterministic
    fallback branch prefix keeps planning useful for child prompts and tests; live
    git verification may later narrow/confirm containment.
    """

    for name in ("branch_for_issue", "get_branch_for_issue", "find_issue_branch"):
        method = getattr(github_client, name, None)
        if callable(method):
            branch = await _maybe_await(method(key.owner, key.repo, key.number))
            if branch:
                return str(branch)
    for name in ("pull_request_for_issue", "get_pull_request_for_issue", "find_pull_request_for_issue"):
        method = getattr(github_client, name, None)
        if callable(method):
            pr = await _maybe_await(method(key.owner, key.repo, key.number))
            if isinstance(pr, dict):
                head = pr.get("head")
                if isinstance(head, dict) and head.get("ref"):
                    return str(head["ref"])
                if pr.get("headRefName"):
                    return str(pr["headRefName"])
                if pr.get("branch"):
                    return str(pr["branch"])
            for attr in ("headRefName", "branch"):
                value = getattr(pr, attr, None)
                if value:
                    return str(value)
            head = getattr(pr, "head", None)
            value = getattr(head, "ref", None)
            if value:
                return str(value)
    return _default_branch_name(key)


async def _issue_graph_coverage(github_client: Any, issue: IssueKey, direct_dependencies: set[IssueKey]) -> tuple[IssueKey, ...]:
    """Return direct child dependencies covered by an issue's dependency graph."""

    covered: set[IssueKey] = {issue}
    stack = [issue]
    visited: set[IssueKey] = set()
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        try:
            payload = await _get_issue(github_client, current)
        except Exception:
            continue
        for blocker in parse_blockers(payload.body, payload.owner, payload.repo):
            if blocker in direct_dependencies and blocker not in covered:
                covered.add(blocker)
            if blocker not in visited:
                stack.append(blocker)
    return tuple(sorted(covered, key=lambda key: key.number))


async def _verify_git_coverage(
    git_client: Any | None,
    candidate: DependencyBranch,
    dependencies: tuple[DependencyBranch, ...],
) -> DependencyBranch:
    if git_client is None:
        return candidate
    verifier = getattr(git_client, "branch_contains", None)
    if not callable(verifier):
        verifier = getattr(git_client, "merge_base_is_ancestor", None)
    if not callable(verifier):
        return candidate

    covered = set(candidate.covers)
    for dependency in dependencies:
        if dependency.issue in covered:
            continue
        try:
            contains = await _maybe_await(verifier(dependency.branch, candidate.branch))
        except Exception:
            continue
        if contains:
            covered.add(dependency.issue)
    return DependencyBranch(candidate.issue, candidate.branch, tuple(sorted(covered, key=lambda key: key.number)))


def _commands_for(plan: BranchPreparationPlan) -> tuple[tuple[str, ...], ...]:
    base_ref = f"origin/{plan.base_branch}"
    commands: list[tuple[str, ...]] = [
        ("git", "fetch", "origin"),
        ("git", "checkout", "-B", plan.child_branch, base_ref),
    ]
    for dependency in plan.additional_rebase_branches:
        commands.append(("git", "rebase", f"origin/{dependency.branch}"))
    return tuple(commands)


async def plan_branch_preparation(
    *,
    child: GitHubIssue,
    child_branch: str,
    github_client: Any,
    git_client: Any | None = None,
    pr_base: str = MAIN_BRANCH,
) -> BranchPreparationPlan:
    """Plan dependency-aware preparation for a child branch.

    No-dependency children start from latest main.  Dependency children choose the
    dependency branch covering the most direct blockers (issue graph first,
    optionally verified with git containment).  Remaining dependency branches are
    rebased into the newly-created child branch; dependency branches themselves
    are never checked out or mutated by the plan.
    """

    blockers = parse_blockers(child.body, child.owner, child.repo)
    if not blockers:
        plan = BranchPreparationPlan(child_branch=child_branch, pr_base=pr_base, base_branch=MAIN_BRANCH)
        return replace(plan, git_commands=_commands_for(plan))

    direct_dependency_set = set(blockers)
    dependencies: list[DependencyBranch] = []
    for blocker in blockers:
        branch = await _dependency_branch_for_issue(github_client, blocker)
        covers = await _issue_graph_coverage(github_client, blocker, direct_dependency_set)
        dependencies.append(DependencyBranch(blocker, branch, covers or (blocker,)))

    verified_items: list[DependencyBranch] = []
    for dependency in dependencies:
        verified_items.append(await _verify_git_coverage(git_client, dependency, tuple(dependencies)))
    verified = tuple(verified_items)
    base = min(verified, key=lambda dependency: (-len(set(dependency.covers)), dependency.issue.number))
    covered_by_base = set(base.covers)
    additional = tuple(dependency for dependency in verified if dependency.issue not in covered_by_base)
    plan = BranchPreparationPlan(
        child_branch=child_branch,
        pr_base=pr_base,
        base_branch=base.branch,
        dependency_branches=verified,
        additional_rebase_branches=additional,
    )
    return replace(plan, git_commands=_commands_for(plan))


async def execute_branch_preparation(plan: BranchPreparationPlan, command_runner: Any) -> None:
    """Execute a branch-preparation plan, raising pause-worthy failures.

    ``command_runner`` may be a callable accepting a command tuple or an object
    exposing ``run(command)``. Any non-zero return code or exception becomes a
    ``BranchPreparationFailure`` so the runner can pause instead of guessing
    through rebase conflicts.
    """

    run = getattr(command_runner, "run", None) if not callable(command_runner) else command_runner
    if not callable(run):
        raise TypeError("command_runner must be callable or expose run(command)")
    for command in plan.git_commands:
        try:
            result = await _maybe_await(run(command))
        except Exception as exc:  # pragma: no cover - exact runner exceptions are adapter-specific
            raise BranchPreparationFailure("Branch preparation command failed", command) from exc
        returncode = getattr(result, "returncode", None)
        if returncode is None and isinstance(result, dict):
            returncode = result.get("returncode", result.get("exit_code"))
        if returncode not in (None, 0):
            raise BranchPreparationFailure("Branch preparation command failed", command)
