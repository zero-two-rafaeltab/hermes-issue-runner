from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from issue_runner.failure import (
    FailureRecoveryRequest,
    format_failure_prompt,
    parse_recovery_command,
    wait_for_recovery_decision,
)


class FailureRecoveryTests(unittest.TestCase):
    def _event(self, text: str, user_id: str = "u1") -> SimpleNamespace:
        return SimpleNamespace(text=text, source=SimpleNamespace(user_id=user_id))

    def test_recovery_command_parser_is_strict_single_word_lowercase(self) -> None:
        self.assertEqual(parse_recovery_command("retry"), "retry")
        self.assertEqual(parse_recovery_command(" stop \n"), "stop")
        for text in ("Retry", "STOP", "retry please", "please retry", "skip", "", "retry\nstop"):
            self.assertIsNone(parse_recovery_command(text), text)

    def test_failure_prompt_is_actionable_and_rejects_skip(self) -> None:
        prompt = format_failure_prompt(
            FailureRecoveryRequest(
                parent_issue="zero-two/repo#1",
                child_issue="zero-two/repo#9",
                failure_summary="tests failed",
                thread_url="https://discord.example/thread",
            )
        )
        self.assertIn("paused", prompt)
        self.assertIn("exactly one word", prompt)
        self.assertIn("`retry` or `stop`", prompt)
        self.assertIn("`skip`", prompt)
        self.assertIn("https://discord.example/thread", prompt)

    def test_wait_for_recovery_ignores_unauthorized_and_invalid_replies_until_stop(self) -> None:
        prompts: list[str] = []
        replies = iter([
            self._event("retry", user_id="intruder"),
            self._event("skip", user_id="u1"),
            self._event("retry please", user_id="u1"),
            self._event("stop", user_id="u1"),
        ])

        async def prompt_sender(event, gateway, message: str) -> None:
            prompts.append(message)

        async def response_waiter(**kwargs):
            return next(replies)

        def authorization_checker(event, gateway) -> bool:
            return event.source.user_id == "u1"

        decision = asyncio.run(
            wait_for_recovery_decision(
                request=FailureRecoveryRequest(
                    parent_issue="zero-two/repo#1",
                    child_issue="zero-two/repo#9",
                    failure_summary="boom",
                ),
                event=self._event("failure context"),
                gateway=SimpleNamespace(),
                prompt_sender=prompt_sender,
                response_waiter=response_waiter,
                authorization_checker=authorization_checker,
            )
        )

        self.assertEqual(len(prompts), 1)
        self.assertEqual(decision.command, "stop")
        self.assertTrue(decision.should_stop)
        self.assertFalse(decision.should_retry)
        self.assertEqual(decision.responder_id, "u1")


if __name__ == "__main__":
    unittest.main()
