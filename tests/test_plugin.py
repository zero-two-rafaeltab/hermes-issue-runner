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
        self.remove_label_calls: list[tuple[str, str, int, str]] = []
        self.children: list[dict[str, object]] = [
            {"owner": "nous", "repo": "hermes-issue-runner", "number": 4, "title": "Child", "labels": ["ready-for-agent"]}
        ]

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, object]:
        self.calls.append((owner, repo, number))
        for child in self.children:
            if child.get("number") == number:
                return child
        return {"owner": owner, "repo": repo, "number": number, "title": "Injected title"}

    def list_child_issues(self, owner: str, repo: str, parent_number: int) -> list[dict[str, object]]:
        self.child_calls.append((owner, repo, parent_number))
        return self.children

    def ensure_label(self, owner: str, repo: str, label: str) -> None:
        self.ensure_calls.append((owner, repo, label))

    def add_issue_label(self, owner: str, repo: str, number: int, label: str) -> None:
        self.add_label_calls.append((owner, repo, number, label))
        for child in self.children:
            if child.get("number") == number:
                raw_labels = child.get("labels", [])
                labels = list(raw_labels if isinstance(raw_labels, list) else [])
                if label not in labels:
                    labels.append(label)
                child["labels"] = labels

    def remove_issue_label(self, owner: str, repo: str, number: int, label: str) -> None:
        self.remove_label_calls.append((owner, repo, number, label))
        for child in self.children:
            if child.get("number") == number:
                raw_labels = child.get("labels", [])
                child["labels"] = [existing for existing in (raw_labels if isinstance(raw_labels, list) else []) if existing != label]


class IssueRunnerPluginTests(unittest.TestCase):
    def _event(self, text: str, user_id: str = "u1") -> SimpleNamespace:
        return SimpleNamespace(
            text=text,
            source=SimpleNamespace(platform=SimpleNamespace(value="discord"), chat_id="c1", thread_id=None, user_id=user_id),
        )

    def test_plugin_imports_public_issue_runner_package(self) -> None:
        self.assertIs(plugin.StartCommandHandler, issue_runner.start.StartCommandHandler)

    def test_plugin_package_mirrors_src_modules(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        src_package_dir = repo_root / "src" / "issue_runner"
        repo_plugin_dir = repo_root / "plugins" / "issue_runner"
        mirrored_modules = [
            "branch_prep.py",
            "child_run.py",
            "failure.py",
            "selection.py",
            "start.py",
            "state.py",
        ]

        for module_name in mirrored_modules:
            with self.subTest(module=module_name):
                self.assertEqual(
                    (repo_plugin_dir / module_name).read_bytes(),
                    (src_package_dir / module_name).read_bytes(),
                )

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
        async def start_child_session(request):
            return {"scheduled": True, "request": request}

        gateway = SimpleNamespace(adapters={"discord": SimpleNamespace(send=send)}, start_child_session=start_child_session)

        plugin.register(ctx)
        self.assertEqual(hooks, [("pre_gateway_dispatch", plugin.pre_gateway_dispatch)])

        result = asyncio.run(plugin.pre_gateway_dispatch(self._event("/issue-runner start nous/hermes-issue-runner#3"), gateway))
        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "4"})
        self.assertEqual(github.calls, [("nous", "hermes-issue-runner", 3)])
        self.assertEqual(github.child_calls, [("nous", "hermes-issue-runner", 3), ("nous", "hermes-issue-runner", 3)])
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 4, "agent:in-progress")])
        self.assertIn("Title: Injected title", replies[0])
        self.assertIn("Next runnable child is #4", replies[0])

    def test_plugin_resume_and_continue_dispatch_through_event_path(self) -> None:
        for verb in ("resume", "continue"):
            with self.subTest(verb=verb):
                github = FakeGitHub()
                github.children = [
                    {"owner": "nous", "repo": "hermes-issue-runner", "number": 3, "title": "Done", "body": "", "state": "open", "labels": ["ready-for-agent", "agent:done"]},
                    {"owner": "nous", "repo": "hermes-issue-runner", "number": 4, "title": "Next", "body": "## Blocked by\n\n- #3\n", "state": "open", "labels": ["ready-for-agent"]},
                ]
                hooks: list[tuple[str, object]] = []
                replies: list[str] = []

                async def send(chat_id, content, metadata=None):
                    replies.append(content)

                ctx = SimpleNamespace(
                    github_client=github,
                    authorization_checker=lambda event, gateway: True,
                    register_hook=lambda name, hook: hooks.append((name, hook)),
                )

                async def start_child_session(request):
                    return {"scheduled": True, "request": request}

                gateway = SimpleNamespace(adapters={"discord": SimpleNamespace(send=send)}, start_child_session=start_child_session)
                plugin.register(ctx)

                result = asyncio.run(plugin.pre_gateway_dispatch(self._event(f"/issue-runner {verb} nous/hermes-issue-runner#2"), gateway))

                self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "4"})
                self.assertEqual(hooks, [("pre_gateway_dispatch", plugin.pre_gateway_dispatch)])
                self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 4, "agent:in-progress")])
                self.assertIn("Repository: nous/hermes-issue-runner", replies[0])
                self.assertIn("started gateway child session for #4", replies[0])

    def test_build_handler_uses_injected_git_client_seam(self) -> None:
        github = FakeGitHub()
        git_client = SimpleNamespace(name="git")
        handler = plugin._build_handler(SimpleNamespace(github_client=github, git_client=git_client))

        self.assertIs(handler.github_client, github)
        self.assertIs(handler.git_client, git_client)

    def test_build_handler_uses_issue_runner_git_client_fallback(self) -> None:
        github = FakeGitHub()
        git_client = SimpleNamespace(name="fallback-git")
        handler = plugin._build_handler(
            SimpleNamespace(issue_runner_github_client=github, issue_runner_git_client=git_client)
        )

        self.assertIs(handler.github_client, github)
        self.assertIs(handler.git_client, git_client)

    def test_build_handler_uses_issue_runner_recovery_waiter_first(self) -> None:
        github = FakeGitHub()
        preferred = SimpleNamespace(name="preferred")
        fallback = SimpleNamespace(name="fallback")
        handler = plugin._build_handler(
            SimpleNamespace(
                github_client=github,
                issue_runner_recovery_response_waiter=preferred,
                recovery_response_waiter=fallback,
            )
        )

        self.assertIs(handler.recovery_response_waiter, preferred)

    def test_build_handler_uses_recovery_waiter_fallback(self) -> None:
        github = FakeGitHub()
        waiter = SimpleNamespace(name="fallback")
        handler = plugin._build_handler(SimpleNamespace(github_client=github, recovery_response_waiter=waiter))

        self.assertIs(handler.recovery_response_waiter, waiter)

    def test_plugin_recovery_waiter_retries_gateway_start_failure(self) -> None:
        github = FakeGitHub()
        hooks: list[tuple[str, object]] = []
        replies: list[str] = []
        attempts = 0

        async def send(chat_id, content, metadata=None):
            replies.append(content)

        async def recovery_response_waiter(**kwargs):
            return self._event("retry", user_id="u1")

        ctx = SimpleNamespace(
            github_client=github,
            authorization_checker=lambda event, gateway: event.source.user_id == "u1",
            issue_runner_recovery_response_waiter=recovery_response_waiter,
            register_hook=lambda name, hook: hooks.append((name, hook)),
        )

        async def start_child_session(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("gateway start failed once")
            return {"scheduled": True, "request": request}

        gateway = SimpleNamespace(adapters={"discord": SimpleNamespace(send=send)}, start_child_session=start_child_session)
        plugin.register(ctx)

        result = asyncio.run(plugin.pre_gateway_dispatch(self._event("/issue-runner start nous/hermes-issue-runner#3"), gateway))

        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "4"})
        self.assertEqual(hooks, [("pre_gateway_dispatch", plugin.pre_gateway_dispatch)])
        self.assertEqual(attempts, 2)
        self.assertIn("gateway start failed once", replies[0])
        self.assertIn("started gateway child session", replies[1])

    def test_plugin_recovery_waiter_consumes_strict_stop(self) -> None:
        github = FakeGitHub()
        replies: list[str] = []
        response_events = iter([self._event("retry please"), self._event("STOP"), self._event("stop")])

        async def send(chat_id, content, metadata=None):
            replies.append(content)

        async def recovery_response_waiter(**kwargs):
            return next(response_events)

        ctx = SimpleNamespace(
            github_client=github,
            authorization_checker=lambda event, gateway: event.source.user_id == "u1",
            recovery_response_waiter=recovery_response_waiter,
            register_hook=lambda name, hook: None,
        )

        async def start_child_session(request):
            raise RuntimeError("gateway start failed")

        gateway = SimpleNamespace(adapters={"discord": SimpleNamespace(send=send)}, start_child_session=start_child_session)
        plugin.register(ctx)

        result = asyncio.run(plugin.pre_gateway_dispatch(self._event("/issue-runner start nous/hermes-issue-runner#3"), gateway))

        self.assertEqual(result, {"action": "skip", "reason": "failure recovery: stop"})
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 4, "agent:in-progress")])
        self.assertEqual(github.remove_label_calls, [])
        self.assertEqual(len(replies), 1)
        self.assertIn("exactly one word", replies[0])


    def test_standalone_plugin_routes_failure_recovery_to_failed_attempt_thread(self) -> None:
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
                standalone_start = importlib.import_module("issue_runner.start")

                github = FakeGitHub()
                parent_event = self._event("/issue-runner start nous/hermes-issue-runner#3", user_id="u1")
                failure_source = SimpleNamespace(
                    platform=SimpleNamespace(value="discord"),
                    chat_id="failure-thread",
                    thread_id="attempt-1",
                    user_id="child-agent",
                )
                failure_event = SimpleNamespace(text="child failed", source=failure_source)
                prompt_events: list[SimpleNamespace] = []
                prompt_messages: list[str] = []
                response_events = iter([
                    self._event("retry please", user_id="u1"),
                    self._event("stop", user_id="intruder"),
                    self._event("stop", user_id="u1"),
                ])

                async def reply_sender(event, gateway, message):
                    prompt_events.append(event)
                    prompt_messages.append(message)

                async def recovery_response_waiter(**kwargs):
                    self.assertIs(kwargs["event"], failure_event)
                    self.assertIs(kwargs["failure_source"], failure_source)
                    self.assertEqual(kwargs["thread_url"], "https://discord.test/failure-thread")
                    return next(response_events)

                async def child_session_starter(**kwargs):
                    return {
                        "status": "failed",
                        "failure_event": failure_event,
                        "failure_thread_url": "https://discord.test/failure-thread",
                    }

                handler = standalone_start.StartCommandHandler(
                    github_client=github,
                    authorization_checker=lambda event, gateway: event.source.user_id == "u1",
                    reply_sender=reply_sender,
                    child_session_starter=child_session_starter,
                    recovery_response_waiter=recovery_response_waiter,
                )

                result = asyncio.run(handler.handle(parent_event, SimpleNamespace()))
            finally:
                for name in list(sys.modules):
                    if name == "issue_runner" or name.startswith("issue_runner."):
                        sys.modules.pop(name)
                sys.modules.update(original_modules)
                sys.path[:] = original_path

        self.assertEqual(result, {"action": "skip", "reason": "failure recovery: stop"})
        self.assertEqual(prompt_events, [failure_event])
        self.assertIn("exactly one word", prompt_messages[0])
        self.assertIn("https://discord.test/failure-thread", prompt_messages[0])
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 4, "agent:in-progress")])
        self.assertEqual(github.remove_label_calls, [])

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
