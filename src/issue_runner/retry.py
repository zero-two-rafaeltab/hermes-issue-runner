"""Retry attempt planning for failed child issue runs.

Retry state is reconstructed from GitHub PR artifacts rather than hidden local
storage. The first attempt keeps the clean issue branch. Subsequent retry
attempts use deterministic suffixes so every failed attempt can keep its own
thread, branch, and PR audit trail.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Iterable, cast

from .child_run import issue_branch_name
from .selection import GitHubIssue


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass(frozen=True)
class LinkedPullRequest:
    """Small normalized view of a PR linked to a child issue."""

    number: int
    head_branch: str
    state: str = "open"
    url: str = ""

    @property
    def is_open(self) -> bool:
        return self.state.casefold() == "open"


@dataclass(frozen=True)
class RetryAttemptPlan:
    """Derived artifact plan for a retry attempt."""

    child: GitHubIssue
    attempt: int
    branch_name: str
    prior_attempt_prs: tuple[LinkedPullRequest, ...]
    failed_thread_url: str | None = None
    retry_thread_url: str | None = None

    @property
    def is_retry(self) -> bool:
        return self.attempt > 1

    @property
    def superseded_open_prs(self) -> tuple[LinkedPullRequest, ...]:
        return tuple(pr for pr in self.prior_attempt_prs if pr.is_open and pr.head_branch != self.branch_name)


def retry_branch_name(first_attempt_branch: str, attempt: int) -> str:
    """Return the deterministic branch name for an attempt number.

    Attempt 1 has no suffix. Attempt 2 is the first retry and uses ``-retry``;
    later retries use ``-retry-N`` where N starts at 2.
    """

    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    if attempt == 1:
        return first_attempt_branch
    if attempt == 2:
        return f"{first_attempt_branch}-retry"
    return f"{first_attempt_branch}-retry-{attempt - 1}"


def branch_attempt_number(first_attempt_branch: str, branch: str) -> int | None:
    """Infer an attempt number from a branch name for the child issue."""

    if branch == first_attempt_branch:
        return 1
    if branch == f"{first_attempt_branch}-retry":
        return 2
    prefix = f"{first_attempt_branch}-retry-"
    if branch.startswith(prefix):
        suffix = branch[len(prefix) :]
        if suffix.isdigit() and int(suffix) >= 2:
            return int(suffix) + 1
    return None


def _value(payload: Any, name: str, default: Any = None) -> Any:
    if isinstance(payload, dict):
        return payload.get(name, default)
    return getattr(payload, name, default)


def coerce_linked_pr(payload: Any) -> LinkedPullRequest | None:
    head_branch = _value(payload, "headRefName") or _value(payload, "branch")
    head = _value(payload, "head")
    if not head_branch and head is not None:
        head_branch = _value(head, "ref")
    number = _value(payload, "number")
    if number is None or not head_branch:
        return None
    return LinkedPullRequest(
        number=int(number),
        head_branch=str(head_branch),
        state=str(_value(payload, "state", "open") or "open").lower(),
        url=str(_value(payload, "url", "") or ""),
    )


async def linked_pull_requests_for_issue(github_client: Any, child: GitHubIssue) -> tuple[LinkedPullRequest, ...]:
    """Return PRs linked to a child issue through common adapter method names."""

    method_names = (
        "list_pull_requests_for_issue",
        "linked_pull_requests_for_issue",
        "pull_requests_for_issue",
        "find_pull_requests_for_issue",
    )
    payloads: Iterable[Any] = ()
    for name in method_names:
        method = getattr(github_client, name, None)
        if callable(method):
            payloads = cast(Iterable[Any], await _maybe_await(method(child.owner, child.repo, child.number)))
            break
    prs = [pr for payload in payloads if (pr := coerce_linked_pr(payload)) is not None]
    return tuple(sorted(prs, key=lambda pr: pr.number))


async def plan_retry_attempt(
    *,
    child: GitHubIssue,
    github_client: Any,
    failed_thread_url: str | None = None,
    retry_thread_url: str | None = None,
) -> RetryAttemptPlan:
    """Derive the next retry branch from PRs linked to ``child``."""

    base_branch = issue_branch_name(child)
    linked_prs = await linked_pull_requests_for_issue(github_client, child)
    attempts = [attempt for pr in linked_prs if (attempt := branch_attempt_number(base_branch, pr.head_branch))]
    next_attempt = (max(attempts) + 1) if attempts else 2
    return RetryAttemptPlan(
        child=child,
        attempt=next_attempt,
        branch_name=retry_branch_name(base_branch, next_attempt),
        prior_attempt_prs=linked_prs,
        failed_thread_url=failed_thread_url,
        retry_thread_url=retry_thread_url,
    )


async def close_superseded_failed_prs(plan: RetryAttemptPlan, github_client: Any) -> None:
    """Close open prior-attempt PRs with a short retry explanation when supported."""

    for pr in plan.superseded_open_prs:
        comment = (
            f"Closed because retry attempt #{plan.attempt} supersedes this failed attempt "
            f"for issue #{plan.child.number}. Replacement branch: `{plan.branch_name}`."
        )
        for name in ("comment_pull_request", "add_pull_request_comment", "comment_pr"):
            method = getattr(github_client, name, None)
            if callable(method):
                await _maybe_await(method(plan.child.owner, plan.child.repo, pr.number, comment))
                break
        for name in ("close_pull_request", "close_pr"):
            method = getattr(github_client, name, None)
            if callable(method):
                await _maybe_await(method(plan.child.owner, plan.child.repo, pr.number))
                break


def link_attempt_threads_message(
    *,
    failed_thread_url: str | None,
    retry_thread_url: str | None,
    attempt: int,
) -> tuple[str, str] | None:
    """Return reciprocal thread-link messages when both attempt URLs are known."""

    if not failed_thread_url or not retry_thread_url:
        return None
    failed_message = f"Retry attempt #{attempt} started in {retry_thread_url}."
    retry_message = f"This retry replaces failed attempt thread {failed_thread_url}."
    return failed_message, retry_message
