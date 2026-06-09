from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import unittest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from issue_runner.selection import IssueKey, parse_blockers, select_next_child


class FakeGitHub:
    def __init__(self, children, issues=None) -> None:
        self.children = children
        self.issues = issues or {}
        self.child_calls: list[tuple[str, str, int]] = []
        self.issue_calls: list[tuple[str, str, int]] = []

    def list_child_issues(self, owner: str, repo: str, parent_number: int):
        self.child_calls.append((owner, repo, parent_number))
        return self.children

    def get_issue(self, owner: str, repo: str, number: int):
        self.issue_calls.append((owner, repo, number))
        return self.issues[(owner, repo, number)]


def child(number: int, *, labels=None, body="", state="open", title=None):
    return {
        "owner": "nous",
        "repo": "runner",
        "number": number,
        "title": title or f"Child {number}",
        "body": body,
        "state": state,
        "labels": labels or [],
    }


def issue(owner: str, repo: str, number: int, *, labels=None, state="open"):
    return {"owner": owner, "repo": repo, "number": number, "title": f"Issue {number}", "state": state, "labels": labels or []}


class ChildSelectionTests(unittest.TestCase):
    def test_parse_blocked_by_section_numbers_and_links_only_until_next_h2(self) -> None:
        body = """## Context
Mentions #99 here do not count.

## Blocked by
- #3
- https://github.com/other/project/issues/7?from=body
- duplicate #3

## Notes
- #8 does not count
"""
        self.assertEqual(
            parse_blockers(body, "nous", "runner"),
            (IssueKey("nous", "runner", 3), IssueKey("other", "project", 7)),
        )

    def test_parse_blockers_rejects_malformed_github_issue_url_paths(self) -> None:
        body = """## Blocked by
- https://github.com/other/project/issues/7/extra
- https://github.com/other/project/issues/8abc
- https://github.com/other/project/issues/9
- https://github.com/other/project/issues/10?from=body
- https://github.com/other/project/issues/11#note
"""
        self.assertEqual(
            parse_blockers(body, "nous", "runner"),
            (
                IssueKey("other", "project", 9),
                IssueKey("other", "project", 10),
                IssueKey("other", "project", 11),
            ),
        )

    def test_selects_first_ready_unblocked_child_by_issue_number(self) -> None:
        github = FakeGitHub(
            [
                child(12, labels=["ready-for-agent"]),
                child(10, labels=["needs-human"]),
                child(11, labels=[{"name": "ready-for-agent"}]),
            ]
        )
        selection = asyncio.run(select_next_child(IssueKey("nous", "runner", 1), github))
        self.assertEqual(selection.status, "runnable")
        self.assertIsNotNone(selection.selected)
        assert selection.selected is not None
        self.assertEqual(selection.selected.number, 11)
        self.assertEqual(github.child_calls, [("nous", "runner", 1)])
        self.assertEqual(github.issue_calls, [])

    def test_blockers_are_satisfied_by_closed_or_agent_done(self) -> None:
        body = """## Blocked by
- #3
- https://github.com/ext/repo/issues/4
"""
        github = FakeGitHub(
            [child(5, labels=["ready-for-agent"], body=body)],
            {
                ("nous", "runner", 3): issue("nous", "runner", 3, state="closed"),
                ("ext", "repo", 4): issue("ext", "repo", 4, labels=["agent:done"]),
            },
        )
        selection = asyncio.run(select_next_child(IssueKey("nous", "runner", 1), github))
        self.assertEqual(selection.status, "runnable")
        self.assertEqual(selection.selected.number, 5)  # type: ignore[union-attr]
        self.assertEqual(github.issue_calls, [("nous", "runner", 3), ("ext", "repo", 4)])

    def test_waits_on_unsatisfied_blockers_before_later_ready_child(self) -> None:
        github = FakeGitHub(
            [
                child(2, labels=["ready-for-agent"], body="## Blocked by\n- #9\n"),
                child(3, labels=["ready-for-agent"]),
            ],
            {("nous", "runner", 9): issue("nous", "runner", 9)},
        )
        selection = asyncio.run(select_next_child(IssueKey("nous", "runner", 1), github))
        self.assertEqual(selection.status, "runnable")
        self.assertEqual(selection.selected.number, 3)  # type: ignore[union-attr]
        self.assertEqual(selection.blocked[0].child.number, 2)
        self.assertEqual(selection.blocked[0].unsatisfied, (IssueKey("nous", "runner", 9),))

    def test_reports_waiting_on_blockers_when_no_ready_child_is_unblocked(self) -> None:
        github = FakeGitHub(
            [child(2, labels=["ready-for-agent"], body="## Blocked by\n- #9\n")],
            {("nous", "runner", 9): issue("nous", "runner", 9)},
        )
        selection = asyncio.run(select_next_child(IssueKey("nous", "runner", 1), github))
        self.assertEqual(selection.status, "waiting_on_blockers")
        self.assertIsNone(selection.selected)
        self.assertIn("waiting on blockers", selection.message)
        self.assertIn("#2 waits on nous/runner#9", selection.message)

    def test_reports_complete_when_ready_children_are_done(self) -> None:
        github = FakeGitHub(
            [
                child(2, labels=["ready-for-agent"], state="closed"),
                child(3, labels=["ready-for-agent", "agent:done"]),
            ]
        )
        selection = asyncio.run(select_next_child(IssueKey("nous", "runner", 1), github))
        self.assertEqual(selection.status, "complete")
        self.assertIn("complete", selection.message)

    def test_async_child_listing_and_blocker_lookup_are_awaited(self) -> None:
        body = "## Blocked by\n- #4\n"

        class AsyncGitHub(FakeGitHub):
            async def list_child_issues(self, owner: str, repo: str, parent_number: int):  # type: ignore[override]
                self.child_calls.append((owner, repo, parent_number))
                return self.children

            async def get_issue(self, owner: str, repo: str, number: int):  # type: ignore[override]
                self.issue_calls.append((owner, repo, number))
                return self.issues[(owner, repo, number)]

        github = AsyncGitHub(
            [child(2, labels=["ready-for-agent"], body=body)],
            {("nous", "runner", 4): issue("nous", "runner", 4, labels=["agent:done"])},
        )
        selection = asyncio.run(select_next_child(IssueKey("nous", "runner", 1), github))
        self.assertEqual(selection.status, "runnable")
        self.assertEqual(selection.selected.number, 2)  # type: ignore[union-attr]
        self.assertEqual(github.child_calls, [("nous", "runner", 1)])
        self.assertEqual(github.issue_calls, [("nous", "runner", 4)])

    def test_reports_waiting_for_triage_when_children_are_not_ready(self) -> None:
        github = FakeGitHub([child(2, labels=["needs-human"]), child(3, labels=[])])
        selection = asyncio.run(select_next_child(IssueKey("nous", "runner", 1), github))
        self.assertEqual(selection.status, "waiting_for_triage")
        self.assertIn("ready-for-agent", selection.message)


if __name__ == "__main__":
    unittest.main()
