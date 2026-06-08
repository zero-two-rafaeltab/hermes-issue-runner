"""Operational GitHub label state for Hermes Issue Runner.

The MVP state model intentionally uses only two automation labels:

- ``agent:in-progress`` marks a child issue currently owned by a runner attempt.
- ``agent:done`` marks a child issue that completed successfully.

Human triage labels such as ``ready-for-agent`` are never removed here, and the
runner does not create or depend on ``agent:blocked``.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Iterable, cast

from .selection import DONE_LABEL, GitHubIssue, IssueKey, coerce_github_issue

IN_PROGRESS_LABEL = "agent:in-progress"
OPERATIONAL_LABELS = (IN_PROGRESS_LABEL, DONE_LABEL)


@dataclass(frozen=True)
class DuplicateRunGuard:
    """Result of checking whether a parent already has active runner work."""

    parent: IssueKey
    in_progress: tuple[GitHubIssue, ...]

    @property
    def has_incomplete_state(self) -> bool:
        return bool(self.in_progress)

    @property
    def message(self) -> str:
        if not self.in_progress:
            return f"Parent {self.parent.ref} has no {IN_PROGRESS_LABEL} children."
        children = ", ".join(f"#{child.number} {child.title}".rstrip() for child in self.in_progress)
        return f"Refusing duplicate start; {IN_PROGRESS_LABEL} already present on: {children}."


async def _list_child_payloads(github_client: Any, parent: IssueKey) -> Iterable[Any]:
    for name in ("list_child_issues", "list_sub_issues", "get_child_issues"):
        method = getattr(github_client, name, None)
        if callable(method):
            return cast(Iterable[Any], await _maybe_await(method(parent.owner, parent.repo, parent.number)))
    raise TypeError(
        "github_client must expose list_child_issues(owner, repo, parent_number) "
        "or list_sub_issues/get_child_issues for duplicate-run checks"
    )


def _issue_has_label(issue: GitHubIssue, label: str) -> bool:
    return issue.has_label(label)


async def in_progress_children(parent: IssueKey, github_client: Any) -> tuple[GitHubIssue, ...]:
    """Return child issues under ``parent`` currently labeled in progress."""

    children = (
        coerce_github_issue(parent.owner, parent.repo, payload)
        for payload in await _list_child_payloads(github_client, parent)
    )
    return tuple(sorted((child for child in children if _issue_has_label(child, IN_PROGRESS_LABEL)), key=lambda child: child.number))


async def check_duplicate_run(parent: IssueKey, github_client: Any) -> DuplicateRunGuard:
    """Refuse a new start when any child already has incomplete runner state."""

    return DuplicateRunGuard(parent=parent, in_progress=await in_progress_children(parent, github_client))


def _call_first(github_client: Any, names: tuple[str, ...], *args: Any) -> Any:
    for name in names:
        method = getattr(github_client, name, None)
        if callable(method):
            return method(*args)
    raise TypeError(f"github_client must expose one of: {', '.join(names)}")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def ensure_operational_labels(owner: str, repo: str, github_client: Any) -> None:
    """Create or verify the two MVP automation labels when the adapter supports it."""

    ensure = getattr(github_client, "ensure_label", None)
    if callable(ensure):
        for label in OPERATIONAL_LABELS:
            await _maybe_await(ensure(owner, repo, label))
        return

    ensure_many = getattr(github_client, "ensure_labels", None)
    if callable(ensure_many):
        await _maybe_await(ensure_many(owner, repo, OPERATIONAL_LABELS))
        return

    # Read-only/test adapters may not manage labels. Selection still remains
    # side-effect free until a caller asks to mark an issue.
    return


def add_issue_label(issue: IssueKey, label: str, github_client: Any) -> Any:
    """Add a label to an issue through common GitHub adapter method names."""

    return _call_first(
        github_client,
        ("add_issue_label", "add_label_to_issue", "add_label", "label_issue"),
        issue.owner,
        issue.repo,
        issue.number,
        label,
    )


def remove_issue_label(issue: IssueKey, label: str, github_client: Any) -> Any:
    """Remove a label from an issue through common GitHub adapter method names."""

    return _call_first(
        github_client,
        ("remove_issue_label", "remove_label_from_issue", "remove_label", "unlabel_issue"),
        issue.owner,
        issue.repo,
        issue.number,
        label,
    )


async def mark_child_started(child: GitHubIssue | IssueKey, github_client: Any) -> None:
    """Mark a selected child in progress immediately without touching triage labels."""

    key = child.key if isinstance(child, GitHubIssue) else child
    await _maybe_await(add_issue_label(key, IN_PROGRESS_LABEL, github_client))


def _missing_label_error(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return "not found" in text or "404" in text or "does not exist" in text


async def clear_child_started(child: GitHubIssue | IssueKey, github_client: Any) -> None:
    """Best-effort cleanup for an in-progress marker that may already be absent."""

    key = child.key if isinstance(child, GitHubIssue) else child
    try:
        await _maybe_await(remove_issue_label(key, IN_PROGRESS_LABEL, github_client))
    except Exception as exc:
        if not _missing_label_error(exc):
            raise


async def mark_child_done(child: GitHubIssue | IssueKey, github_client: Any) -> None:
    """Transition a successfully completed child from in-progress to done.

    The transition is idempotent with respect to ``agent:in-progress`` so a
    child that already removed or never had that label can still be marked done.
    """

    key = child.key if isinstance(child, GitHubIssue) else child
    await clear_child_started(child, github_client)
    await _maybe_await(add_issue_label(key, DONE_LABEL, github_client))
