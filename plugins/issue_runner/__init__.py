"""Hermes plugin entrypoint for the Issue Runner command."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_SRC = Path(__file__).resolve().parents[2] / "src"
if _REPO_SRC.exists() and str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from issue_runner.start import StartCommandHandler, detect_start_intent

logger = logging.getLogger(__name__)


class GhCliGitHubClient:
    """Small live GitHub adapter backed by the authenticated ``gh`` CLI.

    The issue-runner core intentionally talks to a narrow injected GitHub seam.
    In production Hermes we satisfy that seam with ``gh`` so we can reuse the
    machine's existing GitHub auth without storing plugin-specific secrets.
    """

    def __init__(self, *, timeout: int = 45) -> None:
        self.timeout = timeout

    def _run(self, args: list[str]) -> str:
        completed = subprocess.run(
            ["gh", *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "gh command failed").strip()
            raise RuntimeError(detail)
        return completed.stdout

    @staticmethod
    def _label_names(labels: Any) -> list[str]:
        if isinstance(labels, dict):
            labels = labels.get("nodes", [])
        names: list[str] = []
        for label in labels or []:
            if isinstance(label, str):
                names.append(label)
            elif isinstance(label, dict) and label.get("name"):
                names.append(str(label["name"]))
            elif not isinstance(label, dict):
                label_name = getattr(label, "name", None)
                if label_name:
                    names.append(str(label_name))
        return names

    @classmethod
    def _coerce_issue(cls, owner: str, repo: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "owner": owner,
            "repo": repo,
            "number": int(payload["number"]),
            "title": payload.get("title") or "",
            "body": payload.get("body") or "",
            "state": str(payload.get("state") or "open").lower(),
            "labels": cls._label_names(payload.get("labels", [])),
        }

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        raw = self._run(
            [
                "issue",
                "view",
                str(number),
                "--repo",
                f"{owner}/{repo}",
                "--json",
                "number,title,body,state,labels",
            ]
        )
        return self._coerce_issue(owner, repo, json.loads(raw))

    def list_child_issues(self, owner: str, repo: str, parent_number: int) -> list[dict[str, Any]]:
        query = """
        query($owner:String!,$repo:String!,$number:Int!) {
          repository(owner:$owner, name:$repo) {
            issue(number:$number) {
              subIssues(first:100) {
                nodes {
                  number
                  title
                  body
                  state
                  labels(first:50) { nodes { name } }
                }
              }
            }
          }
        }
        """
        raw = self._run(
            [
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                f"owner={owner}",
                "-f",
                f"repo={repo}",
                "-F",
                f"number={parent_number}",
            ]
        )
        payload = json.loads(raw)
        issue = payload.get("data", {}).get("repository", {}).get("issue")
        if not issue:
            return []
        nodes = issue.get("subIssues", {}).get("nodes", []) or []
        return [self._coerce_issue(owner, repo, node) for node in nodes]

    def ensure_label(self, owner: str, repo: str, label: str) -> None:
        completed = subprocess.run(
            [
                "gh",
                "label",
                "create",
                label,
                "--repo",
                f"{owner}/{repo}",
                "--color",
                "5319E7",
                "--description",
                "Hermes Issue Runner operational state",
                "--force",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "gh label create failed").strip()
            raise RuntimeError(detail)

    def add_issue_label(self, owner: str, repo: str, number: int, label: str) -> None:
        self._run(["issue", "edit", str(number), "--repo", f"{owner}/{repo}", "--add-label", label])

    def remove_issue_label(self, owner: str, repo: str, number: int, label: str) -> None:
        self._run(["issue", "edit", str(number), "--repo", f"{owner}/{repo}", "--remove-label", label])

    def pull_request_for_issue(self, owner: str, repo: str, number: int) -> dict[str, Any] | None:
        prs = self._pull_requests_for_issue(owner, repo, number)
        return prs[0] if prs else None

    def linked_pull_requests_for_issue(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return self._pull_requests_for_issue(owner, repo, number)

    def _pull_requests_for_issue(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        raw = self._run(
            [
                "pr",
                "list",
                "--repo",
                f"{owner}/{repo}",
                "--state",
                "all",
                "--search",
                f"repo:{owner}/{repo} type:pr #{number}",
                "--json",
                "number,headRefName,state,title,url",
                "--limit",
                "30",
            ]
        )
        issue_prefixes = (f"issue-{number}-", f"issue-{number}_", f"issue-{number}")
        result: list[dict[str, Any]] = []
        for pr in json.loads(raw):
            branch = str(pr.get("headRefName") or pr.get("branch") or "")
            title = str(pr.get("title") or "")
            if any(prefix in branch for prefix in issue_prefixes) or f"#{number}" in title:
                result.append({**pr, "branch": branch, "headRefName": branch})
        return result


class UnconfiguredGitHubClient:
    """Fallback used when the live ``gh`` adapter cannot be constructed."""

    def __init__(self, reason: str = "GitHub client is not configured") -> None:
        self.reason = reason

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, object]:
        raise RuntimeError(f"{self.reason}; install/authenticate the gh CLI or inject a GitHub client.")


def _default_github_client() -> Any:
    try:
        subprocess.run(
            ["gh", "auth", "status"],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
        )
        return GhCliGitHubClient()
    except Exception as exc:
        return UnconfiguredGitHubClient(str(exc))


def _issue_labels(payload: Any) -> set[str]:
    labels = payload.get("labels", []) if isinstance(payload, dict) else getattr(payload, "labels", [])
    return set(GhCliGitHubClient._label_names(labels))


async def _wait_for_child_done_label(**kwargs: Any) -> bool:
    """Poll GitHub durable state until the child session marks itself done.

    The Hermes child-session seam currently acknowledges scheduling; the
    platform adapter then runs the actual agent in its own background task. The
    issue runner therefore has to use GitHub labels as the completion signal
    before selecting the next child.
    """

    github_client = kwargs["github_client"]
    child = kwargs["child"]
    interval = float(os.getenv("ISSUE_RUNNER_CONTINUATION_POLL_SECONDS", "30"))
    interval = max(5.0, interval)
    while True:
        payload = await asyncio.to_thread(
            github_client.get_issue,
            child.key.owner,
            child.key.repo,
            child.key.number,
        )
        labels = _issue_labels(payload)
        if "agent:done" in labels:
            return True
        if "agent:in-progress" not in labels:
            logger.warning(
                "issue-runner child %s is no longer in progress and is not done; pausing parent continuation",
                child.key.ref,
            )
            return False
        await asyncio.sleep(interval)


def _build_handler(ctx: Any | None = None) -> StartCommandHandler:
    github_client = getattr(ctx, "github_client", None) if ctx is not None else None
    if github_client is None:
        github_client = getattr(ctx, "issue_runner_github_client", None) if ctx is not None else None
    if github_client is None:
        github_client = _default_github_client()

    auth_checker = getattr(ctx, "authorization_checker", None) if ctx is not None else None
    if auth_checker is None:
        auth_checker = getattr(ctx, "issue_runner_authorization_checker", None) if ctx is not None else None

    git_client = getattr(ctx, "git_client", None) if ctx is not None else None
    if git_client is None:
        git_client = getattr(ctx, "issue_runner_git_client", None) if ctx is not None else None

    recovery_response_waiter = getattr(ctx, "issue_runner_recovery_response_waiter", None) if ctx is not None else None
    if recovery_response_waiter is None:
        recovery_response_waiter = getattr(ctx, "recovery_response_waiter", None) if ctx is not None else None

    child_completion_waiter = getattr(ctx, "issue_runner_child_completion_waiter", None) if ctx is not None else None
    if child_completion_waiter is None:
        child_completion_waiter = getattr(ctx, "child_completion_waiter", None) if ctx is not None else None
    if child_completion_waiter is None and isinstance(github_client, GhCliGitHubClient):
        child_completion_waiter = _wait_for_child_done_label

    return StartCommandHandler(
        github_client=github_client,
        authorization_checker=auth_checker,
        git_client=git_client,
        recovery_response_waiter=recovery_response_waiter,
        child_completion_waiter=child_completion_waiter,
    )


_handler = _build_handler()


def _issue_runner_command(args: str = "") -> str | None:
    """Fallback text handler for non-gateway plugin command dispatch.

    Gateway messages are handled by ``pre_gateway_dispatch`` before Hermes'
    generic plugin-command dispatcher runs, because the real runner needs the
    gateway event and child-session seam. Returning ``None`` lets that normal
    gateway flow continue if this function is ever reached there.
    """

    if args.strip():
        return None
    return "Usage: `/issue-runner start owner/repo#123` or `/issue-runner start https://github.com/owner/repo/issues/123`."


def register(ctx: Any) -> None:
    """Register the Discord-visible command and gateway pre-dispatch hook."""
    global _handler
    _handler = _build_handler(ctx)
    ctx.register_command(
        "issue-runner",
        _issue_runner_command,
        description="Run GitHub parent issues through Hermes Issue Runner",
        args_hint="start owner/repo#123",
    )
    # Hermes' pre_gateway_dispatch hook runner is synchronous. Register a sync
    # adapter that schedules the async handler on the live gateway event loop;
    # registering ``pre_gateway_dispatch`` directly creates an un-awaited
    # coroutine, which makes Discord slash invocations show then delete only the
    # ephemeral "thinking" response without actually running Issue Runner.
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch_hook)


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except Exception:
        logger.exception("issue-runner pre_gateway_dispatch task failed")


def pre_gateway_dispatch_hook(event: Any, gateway: Any, session_store: Any = None) -> dict[str, str] | None:
    """Synchronous Hermes hook adapter for async Issue Runner handling."""
    if detect_start_intent(getattr(event, "text", "")) is None:
        return None

    coroutine = pre_gateway_dispatch(event, gateway, session_store)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    task = loop.create_task(coroutine)
    task.add_done_callback(_log_task_exception)
    return {"action": "skip", "reason": "issue-runner dispatch scheduled"}


async def pre_gateway_dispatch(event: Any, gateway: Any, session_store: Any = None) -> dict[str, str] | None:
    """Handle `/issue-runner start ...` and mention-based start requests."""
    return await _handler.handle(event, gateway)
