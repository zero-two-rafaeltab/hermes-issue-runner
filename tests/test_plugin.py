from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import shutil
import sys
import tempfile
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
        self.child_calls: list[tuple[str, str, int]] = []
        self.ensure_calls: list[tuple[str, str, str]] = []
        self.add_label_calls: list[tuple[str, str, int, str]] = []

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, object]:
        self.calls.append((owner, repo, number))
        return {"owner": owner, "repo": repo, "number": number, "title": "Injected title"}

    def list_child_issues(self, owner: str, repo: str, parent_number: int) -> list[dict[str, object]]:
        self.child_calls.append((owner, repo, parent_number))
        return [
            {"owner": owner, "repo": repo, "number": 4, "title": "Child", "labels": ["ready-for-agent"]}
        ]

    def ensure_label(self, owner: str, repo: str, label: str) -> None:
        self.ensure_calls.append((owner, repo, label))

    def add_issue_label(self, owner: str, repo: str, number: int, label: str) -> None:
        self.add_label_calls.append((owner, repo, number, label))


class IssueRunnerPluginTests(unittest.TestCase):
    def _event(self, text: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=text,
            source=SimpleNamespace(platform=SimpleNamespace(value="discord"), chat_id="c1", thread_id=None),
        )

    def test_plugin_imports_public_issue_runner_package(self) -> None:
        self.assertIs(plugin.StartCommandHandler, issue_runner.start.StartCommandHandler)

    def test_plugin_imports_when_copied_without_repo_src(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        repo_plugin_dir = repo_root / "plugins" / "issue_runner"
        original_path = list(sys.path)
        original_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "issue_runner" or name.startswith("issue_runner.")
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_parent = Path(temp_dir)
            shutil.copytree(repo_plugin_dir, temp_parent / "issue_runner")
            for name in list(sys.modules):
                if name == "issue_runner" or name.startswith("issue_runner."):
                    sys.modules.pop(name)
            sys.path[:] = [str(temp_parent)] + [
                entry
                for entry in original_path
                if entry and not Path(entry).resolve().is_relative_to(repo_root)
            ]
            try:
                standalone_plugin = importlib.import_module("issue_runner")
                standalone_start = importlib.import_module("issue_runner.start")
                standalone_selection = importlib.import_module("issue_runner.selection")
            finally:
                for name in list(sys.modules):
                    if name == "issue_runner" or name.startswith("issue_runner."):
                        sys.modules.pop(name)
                sys.modules.update(original_modules)
                sys.path[:] = original_path

        self.assertIs(standalone_plugin.StartCommandHandler, standalone_start.StartCommandHandler)
        self.assertTrue(callable(standalone_selection.select_next_child))
        self.assertTrue(callable(standalone_plugin.register))

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
        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child selected", "child": "4"})
        self.assertEqual(github.calls, [("nous", "hermes-issue-runner", 3)])
        self.assertEqual(github.child_calls, [("nous", "hermes-issue-runner", 3), ("nous", "hermes-issue-runner", 3)])
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 4, "agent:in-progress")])
        self.assertIn("Title: Injected title", replies[0])
        self.assertIn("Next runnable child is #4", replies[0])

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
