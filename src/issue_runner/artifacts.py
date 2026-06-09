"""Commit and pull-request artifact lifecycle helpers.

The issue runner keeps routine state transitions quiet, but implementation
attempts still need durable GitHub artifacts at the agreed points:

- after the first normal implementation commit, open a draft PR;
- after a failure with uncommitted file changes, create a WIP commit and draft PR;
- after a failure before any file changes, do not invent a commit or PR;
- mark the PR ready only after the automatic review loop passes.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Literal

from .child_run import ChildRunPlan

ArtifactOutcome = Literal["draft_pr", "wip_draft_pr", "no_artifact"]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass(frozen=True)
class PullRequestArtifact:
    """Normalized PR artifact returned by an adapter."""

    number: int | None
    url: str | None
    branch: str
    draft: bool = True


@dataclass(frozen=True)
class ArtifactLifecycleResult:
    """Result of ensuring an attempt's GitHub artifact state."""

    outcome: ArtifactOutcome
    pull_request: PullRequestArtifact | None = None
    commit_created: bool = False
    message: str = ""


def _value(payload: Any, name: str, default: Any = None) -> Any:
    if isinstance(payload, dict):
        return payload.get(name, default)
    return getattr(payload, name, default)


def _coerce_pr(payload: Any, *, branch: str) -> PullRequestArtifact:
    return PullRequestArtifact(
        number=_value(payload, "number"),
        url=_value(payload, "url"),
        branch=str(_value(payload, "headRefName", branch) or branch),
        draft=bool(_value(payload, "isDraft", _value(payload, "draft", True))),
    )


async def _has_file_changes(git_client: Any) -> bool:
    for name in ("has_file_changes", "has_changes", "is_dirty"):
        method = getattr(git_client, name, None)
        if callable(method):
            return bool(await _maybe_await(method()))
    status = getattr(git_client, "status", None)
    if callable(status):
        value = await _maybe_await(status())
        return bool(value)
    raise TypeError("git_client must expose has_file_changes()/has_changes()/is_dirty() or status()")


async def _create_wip_commit(git_client: Any, plan: ChildRunPlan) -> str | None:
    message = f"wip: preserve failed attempt for issue {plan.child.number}"
    for name in ("create_wip_commit", "commit_all", "commit"):
        method = getattr(git_client, name, None)
        if callable(method):
            result = await _maybe_await(method(message))
            return None if result is None else str(result)
    raise TypeError("git_client must expose create_wip_commit(), commit_all(), or commit()")


async def _create_draft_pr(github_client: Any, plan: ChildRunPlan) -> PullRequestArtifact:
    body = (
        f"Fixes #{plan.child.number}\n\n"
        "This draft PR was created by Hermes Issue Runner to preserve an auditable implementation attempt."
    )
    for name in ("create_draft_pull_request", "create_pull_request", "open_pull_request"):
        method = getattr(github_client, name, None)
        if callable(method):
            try:
                result = await _maybe_await(
                    method(
                        plan.child.owner,
                        plan.child.repo,
                        title=plan.title,
                        head=plan.branch_name,
                        base=plan.pr_base,
                        body=body,
                        draft=True,
                    )
                )
            except TypeError:
                result = await _maybe_await(method(plan.child.owner, plan.child.repo, plan.title, plan.branch_name, plan.pr_base, body, True))
            return _coerce_pr(result or {}, branch=plan.branch_name)
    raise TypeError("github_client must expose create_draft_pull_request(), create_pull_request(), or open_pull_request()")


async def ensure_draft_pr_after_first_commit(*, plan: ChildRunPlan, github_client: Any) -> ArtifactLifecycleResult:
    """Create the normal draft PR once the first implementation commit exists."""

    pr = await _create_draft_pr(github_client, plan)
    return ArtifactLifecycleResult(
        outcome="draft_pr",
        pull_request=pr,
        commit_created=False,
        message="Created draft PR after first implementation commit.",
    )


async def ensure_failure_artifact(*, plan: ChildRunPlan, git_client: Any, github_client: Any) -> ArtifactLifecycleResult:
    """Preserve changed-work failures without inventing empty artifacts."""

    if not await _has_file_changes(git_client):
        return ArtifactLifecycleResult(
            outcome="no_artifact",
            message="No file changes detected; no WIP commit or PR was created.",
        )
    await _create_wip_commit(git_client, plan)
    pr = await _create_draft_pr(github_client, plan)
    return ArtifactLifecycleResult(
        outcome="wip_draft_pr",
        pull_request=pr,
        commit_created=True,
        message="Created WIP commit and draft PR for changed-work failure.",
    )


async def mark_pr_ready_after_review(*, pull_request: PullRequestArtifact, github_client: Any) -> None:
    """Mark a PR ready only from the post-review success path."""

    if pull_request.number is None:
        raise ValueError("pull request number is required to mark ready")
    for name in ("mark_pull_request_ready", "ready_pull_request", "mark_pr_ready"):
        method = getattr(github_client, name, None)
        if callable(method):
            await _maybe_await(method(pull_request.number))
            return
    raise TypeError("github_client must expose mark_pull_request_ready(), ready_pull_request(), or mark_pr_ready()")
