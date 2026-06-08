"""Hermes plugin entrypoint for the minimal Issue Runner start command."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_REPO_SRC = Path(__file__).resolve().parents[2] / "src"
if _REPO_SRC.exists() and str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from issue_runner.start import StartCommandHandler


class UnconfiguredGitHubClient:
    """Placeholder used until Hermes injects a real GitHub adapter/client."""

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, object]:
        raise RuntimeError(
            "Issue Runner GitHub client is not configured; inject a client with get_issue(owner, repo, number)."
        )


def _build_handler(ctx: Any | None = None) -> StartCommandHandler:
    github_client = getattr(ctx, "github_client", None) if ctx is not None else None
    if github_client is None:
        github_client = getattr(ctx, "issue_runner_github_client", None) if ctx is not None else None
    if github_client is None:
        github_client = UnconfiguredGitHubClient()

    auth_checker = getattr(ctx, "authorization_checker", None) if ctx is not None else None
    if auth_checker is None:
        auth_checker = getattr(ctx, "issue_runner_authorization_checker", None) if ctx is not None else None

    git_client = getattr(ctx, "git_client", None) if ctx is not None else None
    if git_client is None:
        git_client = getattr(ctx, "issue_runner_git_client", None) if ctx is not None else None

    return StartCommandHandler(github_client=github_client, authorization_checker=auth_checker, git_client=git_client)


_handler = _build_handler()


def register(ctx: Any) -> None:
    """Register the gateway pre-dispatch hook with Hermes."""
    global _handler
    _handler = _build_handler(ctx)
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)


async def pre_gateway_dispatch(event: Any, gateway: Any, session_store: Any = None) -> dict[str, str] | None:
    """Handle `/issue-runner start ...` and mention-based start requests."""
    return await _handler.handle(event, gateway)
