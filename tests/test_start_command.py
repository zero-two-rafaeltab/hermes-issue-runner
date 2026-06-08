from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from issue_runner.start import (
    IssueReferenceError,
    StartCommandHandler,
    parse_issue_reference,
    parse_start_command,
)


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, object]:
        self.calls.append((owner, repo, number))
        return {"owner": owner, "repo": repo, "number": number, "title": "PRD: Hermes Issue Runner MVP"}


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

        return StartCommandHandler(github, authorization_checker=auth_checker, reply_sender=reply_sender), github, replies

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
        self.assertEqual(command.reference.repository, "nous/hermes-issue-runner")
        self.assertEqual(command.reference.number, 1)

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
        self.assertEqual(result, {"action": "skip", "reason": "issue-runner start resolved"})
        self.assertEqual(github.calls, [("nous", "hermes-issue-runner", 1)])
        self.assertEqual(len(replies), 1)
        self.assertIn("Repository: nous/hermes-issue-runner", replies[0])
        self.assertIn("Parent issue: #1", replies[0])
        self.assertIn("Title: PRD: Hermes Issue Runner MVP", replies[0])

    def test_authorized_natural_mention_resolves_same_behavior(self) -> None:
        handler, github, replies = self._handler(allowed=True)
        event = self._event("@Hermes start issue runner for nous/hermes-issue-runner#1")
        result = asyncio.run(handler.handle(event, SimpleNamespace()))
        self.assertEqual(result, {"action": "skip", "reason": "issue-runner start resolved"})
        self.assertEqual(github.calls, [("nous", "hermes-issue-runner", 1)])
        self.assertIn("Repository: nous/hermes-issue-runner", replies[0])

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

        gateway = SimpleNamespace(_is_user_authorized=lambda source: source.user_id == "u1")
        handler = StartCommandHandler(github, reply_sender=reply_sender)
        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#1", user_id="u1"), gateway))
        self.assertEqual(result, {"action": "skip", "reason": "issue-runner start resolved"})
        self.assertEqual(github.calls, [("nous", "hermes-issue-runner", 1)])

        result = asyncio.run(handler.handle(self._event("/issue-runner start nous/hermes-issue-runner#2", user_id="u2"), gateway))
        self.assertEqual(result, {"action": "skip", "reason": "unauthorized"})
        self.assertEqual(github.calls, [("nous", "hermes-issue-runner", 1)])


if __name__ == "__main__":
    unittest.main()
