from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import issue_runner.start
from plugins import issue_runner as plugin


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, object]:
        self.calls.append((owner, repo, number))
        return {"owner": owner, "repo": repo, "number": number, "title": "Injected title"}


class IssueRunnerPluginTests(unittest.TestCase):
    def _event(self, text: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=text,
            source=SimpleNamespace(platform=SimpleNamespace(value="discord"), chat_id="c1", thread_id=None),
        )

    def test_plugin_imports_public_issue_runner_package(self) -> None:
        self.assertIs(plugin.StartCommandHandler, issue_runner.start.StartCommandHandler)

    def test_register_uses_injected_github_and_auth_seams(self) -> None:
        github = FakeGitHub()
        hooks: list[tuple[str, object]] = []
        replies: list[str] = []

        async def send(chat_id, content, metadata=None):
            replies.append(content)

        ctx = SimpleNamespace(
            github_client=github,
            authorization_checker=lambda event, gateway: True,
            register_hook=lambda name, hook: hooks.append((name, hook)),
        )
        gateway = SimpleNamespace(adapters={"discord": SimpleNamespace(send=send)})

        plugin.register(ctx)
        self.assertEqual(hooks, [("pre_gateway_dispatch", plugin.pre_gateway_dispatch)])

        result = asyncio.run(plugin.pre_gateway_dispatch(self._event("/issue-runner start nous/hermes-issue-runner#3"), gateway))
        self.assertEqual(result, {"action": "skip", "reason": "issue-runner start resolved"})
        self.assertEqual(github.calls, [("nous", "hermes-issue-runner", 3)])
        self.assertIn("Title: Injected title", replies[0])

    def test_unconfigured_github_client_returns_actionable_error(self) -> None:
        replies: list[str] = []

        async def send(chat_id, content, metadata=None):
            replies.append(content)

        ctx = SimpleNamespace(
            authorization_checker=lambda event, gateway: True,
            register_hook=lambda name, hook: None,
        )
        gateway = SimpleNamespace(adapters={"discord": SimpleNamespace(send=send)})

        plugin.register(ctx)
        result = asyncio.run(plugin.pre_gateway_dispatch(self._event("/issue-runner start nous/hermes-issue-runner#3"), gateway))
        self.assertEqual(result, {"action": "skip", "reason": "github issue lookup failed"})
        self.assertEqual(len(replies), 1)
        self.assertIn("Unable to resolve GitHub issue nous/hermes-issue-runner#3", replies[0])
        self.assertIn("GitHub client is not configured", replies[0])


if __name__ == "__main__":
    unittest.main()
