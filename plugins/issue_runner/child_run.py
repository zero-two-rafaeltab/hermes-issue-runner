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


@dataclass(frozen=True)
class ChildRunStart:
    """Result of scheduling a child issue through Hermes core."""

    plan: ChildRunPlan
    result: Any


def issue_branch_slug(title: str, *, max_words: int = 7) -> str:
    """Return a short deterministic branch slug from a GitHub issue title."""

    words = re.findall(r"[a-z0-9]+", (title or "").lower())[:max_words]
    return "-".join(words) or "issue"


def issue_branch_name(child: GitHubIssue) -> str:
    """Return the clean first-attempt branch name for a child issue."""

    return f"feat/issue-{child.number}-{issue_branch_slug(child.title)}"


def build_auditable_child_prompt(
    *,
    parent: IssueKey,
    child: GitHubIssue,
    branch_name: str | None = None,
    base_branch: str = "main",
    pr_base: str = "main",
) -> str:
    """Build the self-contained prompt given to the fresh child agent.

    The prompt intentionally carries the full issue body and all lifecycle
    requirements so the child session does not depend on controller context.
    """

    branch = branch_name or issue_branch_name(child)
    issue_url = f"https://github.com/{child.repository}/issues/{child.number}"
    parent_url = f"https://github.com/{parent.repository}/issues/{parent.number}"
    return f"""Use the `auditable-implementation-review-loop` skill to implement this GitHub child issue end-to-end.

Repository: {child.repository}
Parent issue: {parent.ref} ({parent_url})
Selected child issue: {child.ref} — {child.title}
Child issue URL: {issue_url}
Base branch to pull before starting: {base_branch}
Implementation branch to create: {branch}
Pull request base: {pr_base}

Issue body:
{child.body.strip() or "(No issue body provided.)"}

Required process:
1. Load and follow `auditable-implementation-review-loop` exactly, including supporting GitHub/subagent skills.
2. Before implementation work begins, run `git fetch origin`, check out `{base_branch}`, and pull it with `git pull --ff-only origin {base_branch}`. Do not work from a stale branch.
3. Create `{branch}` from the freshly pulled `{base_branch}`.
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
- If a blocking tool, git, GitHub, or test failure occurs, report the real output in this thread instead of inventing success.
"""


def prepare_child_run(
    *,
    parent: IssueKey,
    child: GitHubIssue,
    base_branch: str = "main",
    pr_base: str = "main",
) -> ChildRunPlan:
    """Prepare deterministic child-run metadata and prompt."""

    branch = issue_branch_name(child)
    title = f"Issue #{child.number}: {child.title}" if child.title else f"Issue #{child.number}"
    return ChildRunPlan(
        parent=parent,
        child=child,
        branch_name=branch,
        base_branch=base_branch,
        pr_base=pr_base,
        idempotency_key=f"github:{child.repository}/issues/{child.number}:attempt:1",
        title=title,
        prompt=build_auditable_child_prompt(
            parent=parent,
            child=child,
            branch_name=branch,
            base_branch=base_branch,
            pr_base=pr_base,
        ),
    )


def _request_payload(event: Any, plan: ChildRunPlan) -> dict[str, Any]:
    return {
        "parent_event": event,
        "title": plan.title,
        "prompt": plan.prompt,
        "idempotency_key": plan.idempotency_key,
        "metadata": {
            "source": "hermes-issue-runner",
            "parent_issue": plan.parent.ref,
            "child_issue": plan.child.ref,
            "branch": plan.branch_name,
            "pr_base": plan.pr_base,
        },
    }


async def start_child_issue_session(
    *,
    gateway: Any,
    event: Any,
    parent: IssueKey,
    child: GitHubIssue,
    base_branch: str = "main",
    pr_base: str = "main",
) -> ChildRunStart:
    """Start a gateway-native child session for one selected issue.

    The preferred seam is ``gateway.start_child_session(request)``. Tests or
    adapters may also expose ``gateway.child_session_request_factory`` to coerce
    the dictionary payload into Hermes core's request dataclass.
    """

    starter = getattr(gateway, "start_child_session", None)
    if not callable(starter):
        raise TypeError("gateway must expose start_child_session(request) to run a child issue")

    plan = prepare_child_run(parent=parent, child=child, base_branch=base_branch, pr_base=pr_base)
    payload = _request_payload(event, plan)
    factory = getattr(gateway, "child_session_request_factory", None)
    request = factory(**payload) if callable(factory) else payload
    result = await _maybe_await(starter(request))
    return ChildRunStart(plan=plan, result=result)
