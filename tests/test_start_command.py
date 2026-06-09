from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from issue_runner.child_run import prepare_child_run
from issue_runner.start import (
    IssueReferenceError,
    StartCommandHandler,
    parse_issue_reference,
    parse_start_command,
)


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.child_calls: list[tuple[str, str, int]] = []
        self.ensure_calls: list[tuple[str, str, str]] = []
        self.add_label_calls: list[tuple[str, str, int, str]] = []
        self.remove_label_calls: list[tuple[str, str, int, str]] = []
        self.children: list[dict[str, object]] = [
            {
                "owner": "nous",
                "repo": "hermes-issue-runner",
                "number": 9,
                "title": "Runnable child",
                "body": "",
                "state": "open",
                "labels": ["ready-for-agent"],
            }
        ]

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, object]:
        self.calls.append((owner, repo, number))
        for child in self.children:
            if child.get("number") == number:
                return child
        return {"owner": owner, "repo": repo, "number": number, "title": "PRD: Hermes Issue Runner MVP"}

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
                labels = [existing for existing in (raw_labels if isinstance(raw_labels, list) else []) if existing != label]
                child["labels"] = labels


class StartCommandTests(unittest.TestCase):
    def _event(self, text: str, user_id: str = "u1", platform: str = "discord") -> SimpleNamespace:
        return SimpleNamespace(
            text=text,
            source=SimpleNamespace(
                platform=SimpleNamespace(value=platform),
                chat_id="c1",
                thread_id=None,
                user_id=user_id,
            ),
        )

    def _handler(self, allowed: bool = True) -> tuple[StartCommandHandler, FakeGitHub, list[str]]:
        github = FakeGitHub()
        replies: list[str] = []

        async def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        def auth_checker(event, gateway) -> bool:
            return allowed

        async def child_session_starter(**kwargs):
            return SimpleNamespace(plan=prepare_child_run(parent=kwargs["parent"], child=kwargs["child"]))

        return (
            StartCommandHandler(
                github,
                authorization_checker=auth_checker,
                reply_sender=reply_sender,
                child_session_starter=child_session_starter,
            ),
            github,
            replies,
        )

    def test_parse_owner_repo_issue_reference(self) -> None:
        ref = parse_issue_reference("please use nous/hermes-issue-runner#123")
        self.assertEqual(ref.owner, "nous")
        self.assertEqual(ref.repo, "hermes-issue-runner")
        self.assertEqual(ref.number, 123)
        self.assertEqual(ref.repository, "nous/hermes-issue-runner")

    def test_parse_github_issue_url_reference(self) -> None:
        ref = parse_issue_reference("https://github.com/nous/hermes-issue-runner/issues/1?foo=bar")
        self.assertEqual((ref.owner, ref.repo, ref.number), ("nous", "hermes-issue-runner", 1))

    def test_parse_github_issue_url_rejects_extra_path_segments(self) -> None:
        with self.assertRaisesRegex(IssueReferenceError, "GitHub issue URL"):
            parse_issue_reference("https://github.com/nous/hermes-issue-runner/issues/1/extra")

    def test_slash_command_parsing_accepts_parent_reference(self) -> None:
        command = parse_start_command("/issue-runner start nous/hermes-issue-runner#1")
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command.mode, "slash")
        self.assertEqual(command.action, "start")
        self.assertEqual(command.reference.repository, "nous/hermes-issue-runner")
        self.assertEqual(command.reference.number, 1)

    def test_resume_and_continue_are_handler_commands_not_start_commands(self) -> None:
        self.assertIsNone(parse_start_command("/issue-runner resume nous/hermes-issue-runner#1"))
        self.assertIsNone(parse_start_command("/issue-runner continue nous/hermes-issue-runner#1"))

    def test_natural_language_mention_parsing_matches_slash_behavior(self) -> None:
        command = parse_start_command("<@1234> please start issue runner for https://github.com/nous/hermes-issue-runner/issues/1")
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command.mode, "mention")
        self.assertEqual(command.reference.repository, "nous/hermes-issue-runner")
        self.assertEqual(command.reference.number, 1)

    def test_non_start_slash_commands_are_ignored(self) -> None:
        self.assertIsNone(parse_start_command("/issue-runner status nous/hermes-issue-runner#1"))
        self.assertIsNone(parse_start_command("/issue-runner stop nous/hermes-issue-runner#1"))
        self.assertIsNone(parse_start_command("/issue-runner nous/hermes-issue-runner#1"))
        self.assertIsNone(parse_start_command("/issue_runner status https://github.com/nous/hermes-issue-runner/issues/1"))

    def test_non_command_text_is_ignored(self) -> None:
        self.assertIsNone(parse_start_command("we should discuss nous/hermes-issue-runner#1 later"))

    def test_invalid_command_reference_raises_actionable_error(self) -> None:
        with self.assertRaisesRegex(IssueReferenceError, "Expected a parent issue reference"):
            parse_start_command("/issue-runner start not-a-reference")
        with self.assertRaisesRegex(IssueReferenceError, "GitHub issue URL"):
            parse_start_command("/issue-runner start https://github.com/nous/hermes-issue-runner/pull/1")

    def test_authorized_slash_command_resolves_and_replies(self) -> None:
        handler, github, replies = self._handler(allowed=True)
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))
        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "9"})
        self.assertEqual(github.calls, [("nous", "hermes-issue-runner", 1)])
        self.assertEqual(
            github.ensure_calls,
            [("nous", "hermes-issue-runner", "agent:in-progress"), ("nous", "hermes-issue-runner", "agent:done")],
        )
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 9, "agent:in-progress")])
        self.assertEqual(len(replies), 1)
        self.assertIn("Repository: nous/hermes-issue-runner", replies[0])
        self.assertIn("Parent issue: #1", replies[0])
        self.assertIn("Title: PRD: Hermes Issue Runner MVP", replies[0])

    def test_git_client_is_passed_to_branch_preparer(self) -> None:
        github = FakeGitHub()
        git_client = SimpleNamespace(name="git-seam")
        seen: list[object] = []
        replies: list[str] = []

        async def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        async def branch_preparer(**kwargs):
            seen.append(kwargs.get("git_client"))
            return SimpleNamespace(base_branch="main", pr_base="main")

        async def child_session_starter(**kwargs):
            return SimpleNamespace(plan=prepare_child_run(parent=kwargs["parent"], child=kwargs["child"]))

        handler = StartCommandHandler(
            github,
            authorization_checker=lambda event, gateway: True,
            reply_sender=reply_sender,
            child_session_starter=child_session_starter,
            branch_preparer=branch_preparer,
            git_client=git_client,
        )
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))

        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "9"})
        self.assertEqual(seen, [git_client])
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 9, "agent:in-progress")])

    def test_parent_run_loop_reselects_after_completed_child_and_starts_next(self) -> None:
        github = FakeGitHub()
        github.children = [
            {
                "owner": "nous",
                "repo": "hermes-issue-runner",
                "number": 8,
                "title": "First child",
                "body": "",
                "state": "open",
                "labels": ["ready-for-agent"],
            },
            {
                "owner": "nous",
                "repo": "hermes-issue-runner",
                "number": 9,
                "title": "Second child",
                "body": "## Blocked by\n\n- #8\n",
                "state": "open",
                "labels": ["ready-for-agent"],
            },
        ]
        replies: list[str] = []
        started_children: list[int] = []

        async def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        async def child_session_starter(**kwargs):
            started_children.append(kwargs["child"].number)
            plan = prepare_child_run(parent=kwargs["parent"], child=kwargs["child"])
            if kwargs["child"].number == 8:
                return SimpleNamespace(plan=plan, completed_child_issue=True)
            return SimpleNamespace(plan=plan)

        handler = StartCommandHandler(
            github,
            authorization_checker=lambda event, gateway: True,
            reply_sender=reply_sender,
            child_session_starter=child_session_starter,
        )
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))

        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "9"})
        self.assertEqual(started_children, [8, 9])
        self.assertEqual(
            github.add_label_calls,
            [
                ("nous", "hermes-issue-runner", 8, "agent:in-progress"),
                ("nous", "hermes-issue-runner", 8, "agent:done"),
                ("nous", "hermes-issue-runner", 9, "agent:in-progress"),
            ],
        )
        self.assertEqual(github.remove_label_calls, [("nous", "hermes-issue-runner", 8, "agent:in-progress")])
        self.assertGreaterEqual(github.child_calls.count(("nous", "hermes-issue-runner", 1)), 3)
        self.assertIn("Completed child runs: #8.", replies[0])
        self.assertIn("started gateway child session for #9", replies[0])

    def test_gateway_success_ack_does_not_advance_parent_loop(self) -> None:
        github = FakeGitHub()
        github.children = [
            {"owner": "nous", "repo": "hermes-issue-runner", "number": 8, "title": "First", "body": "", "state": "open", "labels": ["ready-for-agent"]},
            {"owner": "nous", "repo": "hermes-issue-runner", "number": 9, "title": "Second", "body": "", "state": "open", "labels": ["ready-for-agent"]},
        ]
        replies: list[str] = []
        started_children: list[int] = []

        async def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        async def child_session_starter(**kwargs):
            started_children.append(kwargs["child"].number)
            return {"success": True, "plan": prepare_child_run(parent=kwargs["parent"], child=kwargs["child"])}

        handler = StartCommandHandler(
            github,
            authorization_checker=lambda event, gateway: True,
            reply_sender=reply_sender,
            child_session_starter=child_session_starter,
        )
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))

        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "8"})
        self.assertEqual(started_children, [8])
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 8, "agent:in-progress")])
        self.assertNotIn("Completed child runs", replies[0])

    def test_resume_parent_re_fetches_github_state_and_starts_next_child(self) -> None:
        github = FakeGitHub()
        github.children = [
            {"owner": "nous", "repo": "hermes-issue-runner", "number": 8, "title": "Done child", "body": "", "state": "open", "labels": ["ready-for-agent", "agent:done"]},
            {"owner": "nous", "repo": "hermes-issue-runner", "number": 9, "title": "Second child", "body": "## Blocked by\n\n- #8\n", "state": "open", "labels": ["ready-for-agent"]},
        ]
        started_children: list[int] = []

        async def child_session_starter(**kwargs):
            started_children.append(kwargs["child"].number)
            return SimpleNamespace(plan=prepare_child_run(parent=kwargs["parent"], child=kwargs["child"]))

        handler = StartCommandHandler(
            github,
            authorization_checker=lambda event, gateway: True,
            child_session_starter=child_session_starter,
        )
        result = asyncio.run(
            handler.resume_parent(
                event=self._event("continuation"),
                gateway=SimpleNamespace(),
                parent=parse_issue_reference("nous/hermes-issue-runner#1"),
            )
        )

        self.assertEqual(started_children, [9])
        self.assertEqual(result.pending_run.plan.child.number, 9)
        self.assertEqual(github.calls, [("nous", "hermes-issue-runner", 1), ("nous", "hermes-issue-runner", 8), ("nous", "hermes-issue-runner", 8)])
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 9, "agent:in-progress")])

    def test_slash_resume_dispatch_re_fetches_state_replies_and_starts_next_child(self) -> None:
        github = FakeGitHub()
        github.children = [
            {"owner": "nous", "repo": "hermes-issue-runner", "number": 8, "title": "Done child", "body": "", "state": "open", "labels": ["ready-for-agent", "agent:done"]},
            {"owner": "nous", "repo": "hermes-issue-runner", "number": 9, "title": "Second child", "body": "## Blocked by\n\n- #8\n", "state": "open", "labels": ["ready-for-agent"]},
        ]
        replies: list[str] = []
        started_children: list[int] = []

        async def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        async def child_session_starter(**kwargs):
            started_children.append(kwargs["child"].number)
            return SimpleNamespace(plan=prepare_child_run(parent=kwargs["parent"], child=kwargs["child"]))

        handler = StartCommandHandler(
            github,
            authorization_checker=lambda event, gateway: True,
            reply_sender=reply_sender,
            child_session_starter=child_session_starter,
        )
        result = asyncio.run(handler.handle(self._event("/issue-runner resume nous/hermes-issue-runner#1"), SimpleNamespace()))

        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "9"})
        self.assertEqual(started_children, [9])
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 9, "agent:in-progress")])
        self.assertIn("Repository: nous/hermes-issue-runner", replies[0])
        self.assertIn("started gateway child session for #9", replies[0])

    def test_slash_continue_dispatch_matches_resume(self) -> None:
        handler, github, replies = self._handler(allowed=True)
        result = asyncio.run(handler.handle(self._event("/issue-runner continue nous/hermes-issue-runner#1"), SimpleNamespace()))

        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "9"})
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 9, "agent:in-progress")])
        self.assertIn("Repository: nous/hermes-issue-runner", replies[0])

    def test_natural_resume_mention_dispatches_continuation(self) -> None:
        handler, github, replies = self._handler(allowed=True)
        event = self._event("@Hermes please continue issue runner for nous/hermes-issue-runner#1")
        result = asyncio.run(handler.handle(event, SimpleNamespace()))

        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "9"})
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 9, "agent:in-progress")])
        self.assertIn("Repository: nous/hermes-issue-runner", replies[0])

    def test_parent_run_loop_stops_cleanly_when_all_children_complete(self) -> None:
        github = FakeGitHub()
        github.children = [
            {
                "owner": "nous",
                "repo": "hermes-issue-runner",
                "number": 8,
                "title": "Only child",
                "body": "",
                "state": "open",
                "labels": ["ready-for-agent"],
            }
        ]
        replies: list[str] = []

        async def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        async def child_session_starter(**kwargs):
            return SimpleNamespace(plan=prepare_child_run(parent=kwargs["parent"], child=kwargs["child"]), status="agent:done")

        handler = StartCommandHandler(
            github,
            authorization_checker=lambda event, gateway: True,
            reply_sender=reply_sender,
            child_session_starter=child_session_starter,
        )
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))

        self.assertEqual(result, {"action": "skip", "reason": "complete"})
        self.assertEqual(github.add_label_calls[-1], ("nous", "hermes-issue-runner", 8, "agent:done"))
        self.assertIn("Parent nous/hermes-issue-runner#1 is complete", replies[0])
        self.assertIn("Completed child runs: #8.", replies[0])

    def test_branch_preparation_failure_cleans_up_reserved_child_label(self) -> None:
        github = FakeGitHub()
        replies: list[str] = []

        async def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        async def branch_preparer(**kwargs):
            raise RuntimeError("branch prep failed")

        handler = StartCommandHandler(
            github,
            authorization_checker=lambda event, gateway: True,
            reply_sender=reply_sender,
            branch_preparer=branch_preparer,
        )
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))

        self.assertEqual(result, {"action": "skip", "reason": "failure pause unavailable"})
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 9, "agent:in-progress")])
        self.assertEqual(github.remove_label_calls, [])
        self.assertEqual(len(replies), 1)
        self.assertIn("in-progress label is preserved", replies[0])
        self.assertIn("branch prep failed", replies[0])
        self.assertIn("recovery waiting is not configured", replies[0])
        self.assertIn("rerun manually or configure", replies[0])

    def test_child_session_failure_cleans_up_reserved_child_label(self) -> None:
        github = FakeGitHub()
        replies: list[str] = []

        async def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        async def child_session_starter(**kwargs):
            raise RuntimeError("session failed")

        handler = StartCommandHandler(
            github,
            authorization_checker=lambda event, gateway: True,
            reply_sender=reply_sender,
            child_session_starter=child_session_starter,
        )
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))

        self.assertEqual(result, {"action": "skip", "reason": "failure pause unavailable"})
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 9, "agent:in-progress")])
        self.assertEqual(github.remove_label_calls, [])
        self.assertEqual(len(replies), 1)
        self.assertIn("session failed", replies[0])
        self.assertIn("recovery waiting is not configured", replies[0])

    def test_failure_recovery_waits_for_strict_authorized_stop_without_mutating_state(self) -> None:
        github = FakeGitHub()
        replies: list[str] = []
        response_events = iter(
            [
                self._event("retry", user_id="intruder"),
                self._event("skip", user_id="u1"),
                self._event("stop", user_id="u1"),
            ]
        )

        async def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        async def child_session_starter(**kwargs):
            raise RuntimeError("review failed")

        async def recovery_response_waiter(**kwargs):
            return next(response_events)

        handler = StartCommandHandler(
            github,
            authorization_checker=lambda event, gateway: event.source.user_id == "u1",
            reply_sender=reply_sender,
            child_session_starter=child_session_starter,
            recovery_response_waiter=recovery_response_waiter,
        )
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))

        self.assertEqual(result, {"action": "skip", "reason": "failure recovery: stop"})
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 9, "agent:in-progress")])
        self.assertEqual(github.remove_label_calls, [])
        self.assertEqual(len(replies), 1)
        self.assertIn("review failed", replies[0])
        self.assertIn("exactly one word", replies[0])

    def test_branch_preparation_failure_retries_once_and_succeeds(self) -> None:
        github = FakeGitHub()
        replies: list[str] = []
        branch_attempts = 0

        async def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        async def branch_preparer(**kwargs):
            nonlocal branch_attempts
            branch_attempts += 1
            if branch_attempts == 1:
                raise RuntimeError("branch prep failed once")
            return SimpleNamespace(
                base_branch="main",
                pr_base="main",
                child_branch=kwargs["child_branch"],
                git_commands=(),
                has_dependencies=False,
                dependency_branches=(),
                additional_rebase_branches=(),
            )

        async def child_session_starter(**kwargs):
            return SimpleNamespace(plan=prepare_child_run(parent=kwargs["parent"], child=kwargs["child"]))

        async def recovery_response_waiter(**kwargs):
            return self._event("retry", user_id="u1")

        handler = StartCommandHandler(
            github,
            authorization_checker=lambda event, gateway: event.source.user_id == "u1",
            reply_sender=reply_sender,
            branch_preparer=branch_preparer,
            child_session_starter=child_session_starter,
            recovery_response_waiter=recovery_response_waiter,
        )
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))

        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "9"})
        self.assertEqual(branch_attempts, 2)
        self.assertEqual(github.remove_label_calls, [])
        self.assertIn("branch prep failed once", replies[0])
        self.assertIn("started gateway child session", replies[1])

    def test_child_session_startup_failure_retries_once_and_succeeds(self) -> None:
        github = FakeGitHub()
        replies: list[str] = []
        startup_attempts = 0

        async def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        async def child_session_starter(**kwargs):
            nonlocal startup_attempts
            startup_attempts += 1
            if startup_attempts == 1:
                raise RuntimeError("session failed once")
            return SimpleNamespace(plan=prepare_child_run(parent=kwargs["parent"], child=kwargs["child"]))

        async def recovery_response_waiter(**kwargs):
            return self._event("retry", user_id="u1")

        handler = StartCommandHandler(
            github,
            authorization_checker=lambda event, gateway: event.source.user_id == "u1",
            reply_sender=reply_sender,
            child_session_starter=child_session_starter,
            recovery_response_waiter=recovery_response_waiter,
        )
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))

        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "9"})
        self.assertEqual(startup_attempts, 2)
        self.assertEqual(github.remove_label_calls, [])
        self.assertIn("session failed once", replies[0])
        self.assertIn("started gateway child session", replies[1])

    def test_explicit_failed_child_session_status_stops_without_advancing(self) -> None:
        github = FakeGitHub()
        replies: list[str] = []

        async def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        async def child_session_starter(**kwargs):
            return {"status": "failed"}

        async def recovery_response_waiter(**kwargs):
            return self._event("stop", user_id="u1")

        handler = StartCommandHandler(
            github,
            authorization_checker=lambda event, gateway: event.source.user_id == "u1",
            reply_sender=reply_sender,
            child_session_starter=child_session_starter,
            recovery_response_waiter=recovery_response_waiter,
        )
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))

        self.assertEqual(result, {"action": "skip", "reason": "failure recovery: stop"})
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 9, "agent:in-progress")])
        self.assertEqual(github.remove_label_calls, [])
        self.assertNotIn("started gateway child session", "\n".join(replies))
        self.assertIn("reported failure status", replies[0])

    def test_explicit_failed_child_session_status_retries_startup(self) -> None:
        github = FakeGitHub()
        replies: list[str] = []
        startup_attempts = 0

        async def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        async def child_session_starter(**kwargs):
            nonlocal startup_attempts
            startup_attempts += 1
            if startup_attempts == 1:
                return SimpleNamespace(status="failed")
            return SimpleNamespace(plan=prepare_child_run(parent=kwargs["parent"], child=kwargs["child"]))

        async def recovery_response_waiter(**kwargs):
            return self._event("retry", user_id="u1")

        handler = StartCommandHandler(
            github,
            authorization_checker=lambda event, gateway: event.source.user_id == "u1",
            reply_sender=reply_sender,
            child_session_starter=child_session_starter,
            recovery_response_waiter=recovery_response_waiter,
        )
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))

        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "9"})
        self.assertEqual(startup_attempts, 2)
        self.assertIn("reported failure status", replies[0])
        self.assertIn("started gateway child session", replies[1])

    def test_authorized_natural_mention_resolves_same_behavior(self) -> None:
        handler, github, replies = self._handler(allowed=True)
        event = self._event("@Hermes start issue runner for nous/hermes-issue-runner#1")
        result = asyncio.run(handler.handle(event, SimpleNamespace()))
        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "9"})
        self.assertEqual(github.calls, [("nous", "hermes-issue-runner", 1)])
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 9, "agent:in-progress")])
        self.assertIn("Repository: nous/hermes-issue-runner", replies[0])

    def test_start_refuses_duplicate_run_when_child_in_progress(self) -> None:
        handler, github, replies = self._handler(allowed=True)
        github.children = [
            {
                "owner": "nous",
                "repo": "hermes-issue-runner",
                "number": 8,
                "title": "Active child",
                "body": "",
                "state": "open",
                "labels": ["ready-for-agent", "agent:in-progress"],
            },
            {
                "owner": "nous",
                "repo": "hermes-issue-runner",
                "number": 9,
                "title": "Runnable child",
                "body": "",
                "state": "open",
                "labels": ["ready-for-agent"],
            },
        ]
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))
        self.assertEqual(result, {"action": "skip", "reason": "duplicate run in progress"})
        self.assertEqual(github.add_label_calls, [])
        self.assertEqual(github.child_calls, [("nous", "hermes-issue-runner", 1)])
        self.assertIn("Refusing duplicate start", replies[0])
        self.assertIn("#8 Active child", replies[0])

    def test_async_start_handler_awaits_github_child_listing_and_selection(self) -> None:
        class AsyncGitHub(FakeGitHub):
            async def get_issue(self, owner: str, repo: str, number: int) -> dict[str, object]:  # type: ignore[override]
                self.calls.append((owner, repo, number))
                return {"owner": owner, "repo": repo, "number": number, "title": "Async Parent"}

            async def list_child_issues(self, owner: str, repo: str, parent_number: int) -> list[dict[str, object]]:  # type: ignore[override]
                self.child_calls.append((owner, repo, parent_number))
                return self.children

            async def ensure_label(self, owner: str, repo: str, label: str) -> None:  # type: ignore[override]
                self.ensure_calls.append((owner, repo, label))

            async def add_issue_label(self, owner: str, repo: str, number: int, label: str) -> None:  # type: ignore[override]
                self.add_label_calls.append((owner, repo, number, label))

        github = AsyncGitHub()
        replies: list[str] = []

        async def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        async def child_session_starter(**kwargs):
            return SimpleNamespace(plan=prepare_child_run(parent=kwargs["parent"], child=kwargs["child"]))

        handler = StartCommandHandler(
            github,
            authorization_checker=lambda event, gateway: True,
            reply_sender=reply_sender,
            child_session_starter=child_session_starter,
        )
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))
        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "9"})
        self.assertEqual(github.calls, [("nous", "hermes-issue-runner", 1)])
        self.assertEqual(github.child_calls, [("nous", "hermes-issue-runner", 1), ("nous", "hermes-issue-runner", 1)])
        self.assertEqual(github.add_label_calls, [("nous", "hermes-issue-runner", 9, "agent:in-progress")])
        self.assertIn("Title: Async Parent", replies[0])

    def test_unauthorized_user_is_rejected_without_github_work(self) -> None:
        handler, github, replies = self._handler(allowed=False)
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))
        self.assertEqual(result, {"action": "skip", "reason": "unauthorized"})
        self.assertEqual(github.calls, [])
        self.assertIn("not authorized", replies[0])

    def test_non_start_slash_handler_does_not_authorize_or_call_github(self) -> None:
        handler, github, replies = self._handler(allowed=True)
        result = asyncio.run(handler.handle(self._event("/issue-runner status nous/hermes-issue-runner#1"), SimpleNamespace()))
        self.assertIsNone(result)
        self.assertEqual(github.calls, [])
        self.assertEqual(replies, [])

    def test_unauthorized_malformed_command_does_not_leak_reference_guidance(self) -> None:
        handler, github, replies = self._handler(allowed=False)
        result = asyncio.run(handler.handle(self._event("/issue-runner start not-a-reference"), SimpleNamespace()))
        self.assertEqual(result, {"action": "skip", "reason": "unauthorized"})
        self.assertEqual(github.calls, [])
        self.assertEqual(len(replies), 1)
        self.assertIn("not authorized", replies[0])
        self.assertNotIn("Invalid parent issue reference", replies[0])
        self.assertNotIn("owner/repo#1", replies[0])

    def test_missing_default_authorization_rejects_without_github_work(self) -> None:
        github = FakeGitHub()
        replies: list[str] = []

        def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        handler = StartCommandHandler(github, reply_sender=reply_sender)
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))
        self.assertEqual(result, {"action": "skip", "reason": "unauthorized"})
        self.assertEqual(github.calls, [])
        self.assertIn("not authorized", replies[0])

    def test_invalid_reference_replies_without_starting_github_work(self) -> None:
        handler, github, replies = self._handler(allowed=True)
        result = asyncio.run(handler.handle(self._event("/issue-runner start nope"), SimpleNamespace()))
        self.assertEqual(result, {"action": "skip", "reason": "invalid parent issue reference"})
        self.assertEqual(github.calls, [])
        self.assertIn("Invalid parent issue reference", replies[0])
        self.assertIn("owner/repo#1", replies[0])

    def test_default_authorization_reuses_gateway_allowed_user_behavior(self) -> None:
        github = FakeGitHub()
        replies: list[str] = []

        def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        async def start_child_session(request):
            return {"scheduled": True, "request": request}

        gateway = SimpleNamespace(_is_user_authorized=lambda source: source.user_id == "u1", start_child_session=start_child_session)
        handler = StartCommandHandler(github, reply_sender=reply_sender)
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1", user_id="u1"), gateway))
        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "9"})
        self.assertEqual(github.calls, [("nous", "hermes-issue-runner", 1)])

        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#2", user_id="u2"), gateway))
        self.assertEqual(result, {"action": "skip", "reason": "unauthorized"})
        self.assertEqual(github.calls, [("nous", "hermes-issue-runner", 1)])

    def test_default_authorization_awaits_async_gateway_denial_before_bool_coercion(self) -> None:
        github = FakeGitHub()
        replies: list[str] = []

        def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        async def is_user_authorized(source) -> bool:
            return False

        gateway = SimpleNamespace(_is_user_authorized=is_user_authorized)
        handler = StartCommandHandler(github, reply_sender=reply_sender)
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), gateway))
        self.assertEqual(result, {"action": "skip", "reason": "unauthorized"})
        self.assertEqual(github.calls, [])
        self.assertIn("not authorized", replies[0])

    def test_default_authorization_accepts_async_gateway_allow(self) -> None:
        github = FakeGitHub()
        replies: list[str] = []

        def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        async def is_user_authorized(source) -> bool:
            return True

        async def start_child_session(request):
            return {"scheduled": True, "request": request}

        gateway = SimpleNamespace(is_user_authorized=is_user_authorized, start_child_session=start_child_session)
        handler = StartCommandHandler(github, reply_sender=reply_sender)
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), gateway))
        self.assertEqual(result, {"action": "skip", "reason": "issue-runner child run started", "child": "9"})
        self.assertEqual(github.calls, [("nous", "hermes-issue-runner", 1)])
        self.assertIn("Title: PRD: Hermes Issue Runner MVP", replies[0])

    def test_malformed_github_issue_payload_replies_actionably(self) -> None:
        class MalformedGitHub(FakeGitHub):
            def get_issue(self, owner: str, repo: str, number: int) -> dict[str, object]:
                self.calls.append((owner, repo, number))
                return {"owner": owner, "repo": repo, "number": number}

        github = MalformedGitHub()
        replies: list[str] = []

        def reply_sender(event, gateway, message: str) -> bool:
            replies.append(message)
            return True

        handler = StartCommandHandler(github, authorization_checker=lambda event, gateway: True, reply_sender=reply_sender)
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1"), SimpleNamespace()))
        self.assertEqual(result, {"action": "skip", "reason": "github issue lookup failed"})
        self.assertEqual(github.calls, [("nous", "hermes-issue-runner", 1)])
        self.assertEqual(len(replies), 1)
        self.assertIn("Unable to resolve GitHub issue nous/hermes-issue-runner#1", replies[0])
        self.assertIn("did not include a title", replies[0])


if __name__ == "__main__":
    unittest.main()
