from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import unittest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from issue_runner.retry import (
    branch_attempt_number,
    close_superseded_failed_prs,
    link_attempt_threads_message,
    plan_retry_attempt,
    retry_branch_name,
)
from issue_runner.selection import GitHubIssue


class FakeRetryGitHub:
    def __init__(self) -> None:
        self.prs = []
        self.comments: list[tuple[str, str, int, str]] = []
        self.closed: list[tuple[str, str, int]] = []

    def list_pull_requests_for_issue(self, owner: str, repo: str, number: int):
        return self.prs

    def comment_pull_request(self, owner: str, repo: str, number: int, body: str) -> None:
        self.comments.append((owner, repo, number, body))

    def close_pull_request(self, owner: str, repo: str, number: int) -> None:
        self.closed.append((owner, repo, number))


class RetryAttemptTests(unittest.TestCase):
    def child(self) -> GitHubIssue:
        return GitHubIssue(
            owner="nous",
            repo="hermes-issue-runner",
            number=10,
            title="Implement retry attempts, retry naming, and failed PR closure",
            labels=("ready-for-agent",),
        )

    def test_retry_branch_names_keep_first_attempt_clean(self) -> None:
        base = "feat/issue-10-implement-retry-attempts"
        self.assertEqual(retry_branch_name(base, 1), base)
        self.assertEqual(retry_branch_name(base, 2), f"{base}-retry")
        self.assertEqual(retry_branch_name(base, 3), f"{base}-retry-2")

    def test_branch_attempt_number_round_trips_retry_suffixes(self) -> None:
        base = "feat/issue-10-implement-retry-attempts"
        self.assertEqual(branch_attempt_number(base, base), 1)
        self.assertEqual(branch_attempt_number(base, f"{base}-retry"), 2)
        self.assertEqual(branch_attempt_number(base, f"{base}-retry-2"), 3)
        self.assertIsNone(branch_attempt_number(base, f"{base}-other"))

    def test_retry_numbering_is_derived_from_linked_pr_head_branches(self) -> None:
        github = FakeRetryGitHub()
        child = self.child()
        base = "feat/issue-10-implement-retry-attempts-retry-naming-and-failed"
        github.prs = [
            {"number": 30, "headRefName": base, "state": "closed"},
            {"number": 31, "headRefName": f"{base}-retry", "state": "open"},
        ]

        plan = asyncio.run(plan_retry_attempt(child=child, github_client=github))

        self.assertEqual(plan.attempt, 3)
        self.assertEqual(plan.branch_name, f"{base}-retry-2")
        self.assertEqual([pr.number for pr in plan.prior_attempt_prs], [30, 31])

    def test_retry_defaults_to_first_retry_when_no_linked_prs_exist(self) -> None:
        github = FakeRetryGitHub()
        plan = asyncio.run(plan_retry_attempt(child=self.child(), github_client=github))

        self.assertEqual(plan.attempt, 2)
        self.assertTrue(plan.branch_name.endswith("-retry"))

    def test_close_superseded_failed_prs_comments_and_closes_open_prior_attempts(self) -> None:
        github = FakeRetryGitHub()
        child = self.child()
        base = "feat/issue-10-implement-retry-attempts-retry-naming-and-failed"
        github.prs = [
            {"number": 30, "headRefName": base, "state": "open"},
            {"number": 31, "headRefName": f"{base}-retry", "state": "closed"},
        ]
        plan = asyncio.run(plan_retry_attempt(child=child, github_client=github))

        asyncio.run(close_superseded_failed_prs(plan, github))

        self.assertEqual(github.closed, [("nous", "hermes-issue-runner", 30)])
        self.assertEqual(github.comments[0][:3], ("nous", "hermes-issue-runner", 30))
        self.assertIn("supersedes this failed attempt", github.comments[0][3])
        self.assertIn(plan.branch_name, github.comments[0][3])

    def test_thread_link_messages_are_reciprocal_when_both_urls_are_known(self) -> None:
        messages = link_attempt_threads_message(
            failed_thread_url="https://discord/failed",
            retry_thread_url="https://discord/retry",
            attempt=2,
        )
        self.assertEqual(
            messages,
            (
                "Retry attempt #2 started in https://discord/retry.",
                "This retry replaces failed attempt thread https://discord/failed.",
            ),
        )
        self.assertIsNone(link_attempt_threads_message(failed_thread_url=None, retry_thread_url="x", attempt=2))


if __name__ == "__main__":
    unittest.main()
