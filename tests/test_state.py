from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import unittest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from issue_runner.selection import IssueKey
from issue_runner.state import (
    IN_PROGRESS_LABEL,
    OPERATIONAL_LABELS,
    check_duplicate_run,
    ensure_operational_labels,
    mark_child_done,
    mark_child_started,
)


class FakeGitHub:
    def __init__(self, children=None) -> None:
        self.children = children or []
        self.child_calls: list[tuple[str, str, int]] = []
        self.ensure_calls: list[tuple[str, str, str]] = []
        self.add_calls: list[tuple[str, str, int, str]] = []
        self.remove_calls: list[tuple[str, str, int, str]] = []

    def list_child_issues(self, owner: str, repo: str, parent_number: int):
        self.child_calls.append((owner, repo, parent_number))
        return self.children

    def ensure_label(self, owner: str, repo: str, label: str) -> None:
        self.ensure_calls.append((owner, repo, label))

    def add_issue_label(self, owner: str, repo: str, number: int, label: str) -> None:
        self.add_calls.append((owner, repo, number, label))

    def remove_issue_label(self, owner: str, repo: str, number: int, label: str) -> None:
        self.remove_calls.append((owner, repo, number, label))


def child(number: int, *, labels=None, title=None):
    return {
        "owner": "nous",
        "repo": "runner",
        "number": number,
        "title": title or f"Child {number}",
        "state": "open",
        "labels": labels or [],
    }


class OperationalStateTests(unittest.TestCase):
    def test_ensure_operational_labels_uses_only_in_progress_and_done(self) -> None:
        github = FakeGitHub()
        asyncio.run(ensure_operational_labels("nous", "runner", github))
        self.assertEqual(OPERATIONAL_LABELS, ("agent:in-progress", "agent:done"))
        self.assertEqual(github.ensure_calls, [("nous", "runner", "agent:in-progress"), ("nous", "runner", "agent:done")])
        self.assertNotIn("agent:blocked", [label for *_repo, label in github.ensure_calls])

    def test_duplicate_guard_finds_any_child_in_progress(self) -> None:
        github = FakeGitHub(
            [
                child(3, labels=["ready-for-agent"]),
                child(2, labels=["agent:in-progress"], title="Earlier active child"),
            ]
        )
        guard = asyncio.run(check_duplicate_run(IssueKey("nous", "runner", 1), github))
        self.assertTrue(guard.has_incomplete_state)
        self.assertEqual([issue.number for issue in guard.in_progress], [2])
        self.assertIn("Refusing duplicate start", guard.message)
        self.assertIn("#2 Earlier active child", guard.message)

    def test_duplicate_guard_allows_start_when_no_child_in_progress(self) -> None:
        github = FakeGitHub([child(2, labels=["ready-for-agent"]), child(3, labels=["agent:done"])])
        guard = asyncio.run(check_duplicate_run(IssueKey("nous", "runner", 1), github))
        self.assertFalse(guard.has_incomplete_state)
        self.assertIn(f"no {IN_PROGRESS_LABEL} children", guard.message)

    def test_duplicate_guard_awaits_async_child_listing(self) -> None:
        class AsyncGitHub(FakeGitHub):
            async def list_child_issues(self, owner: str, repo: str, parent_number: int):  # type: ignore[override]
                self.child_calls.append((owner, repo, parent_number))
                return self.children

        github = AsyncGitHub([child(4, labels=["agent:in-progress"], title="Async active child")])
        guard = asyncio.run(check_duplicate_run(IssueKey("nous", "runner", 1), github))
        self.assertTrue(guard.has_incomplete_state)
        self.assertEqual([issue.number for issue in guard.in_progress], [4])
        self.assertEqual(github.child_calls, [("nous", "runner", 1)])

    def test_mark_child_started_adds_in_progress_without_touching_ready_label(self) -> None:
        github = FakeGitHub()
        asyncio.run(mark_child_started(IssueKey("nous", "runner", 5), github))
        self.assertEqual(github.add_calls, [("nous", "runner", 5, "agent:in-progress")])
        self.assertEqual(github.remove_calls, [])

    def test_mark_child_done_removes_in_progress_and_adds_done(self) -> None:
        github = FakeGitHub()
        asyncio.run(mark_child_done(IssueKey("nous", "runner", 5), github))
        self.assertEqual(github.remove_calls, [("nous", "runner", 5, "agent:in-progress")])
        self.assertEqual(github.add_calls, [("nous", "runner", 5, "agent:done")])

    def test_mark_child_done_is_harmless_when_in_progress_label_absent(self) -> None:
        class MissingLabelGitHub(FakeGitHub):
            def remove_issue_label(self, owner: str, repo: str, number: int, label: str) -> None:  # type: ignore[override]
                self.remove_calls.append((owner, repo, number, label))
                raise RuntimeError("label not found")

        github = MissingLabelGitHub()
        asyncio.run(mark_child_done(IssueKey("nous", "runner", 5), github))
        self.assertEqual(github.remove_calls, [("nous", "runner", 5, "agent:in-progress")])
        self.assertEqual(github.add_calls, [("nous", "runner", 5, "agent:done")])

    def test_async_label_adapter_methods_are_awaited(self) -> None:
        class AsyncGitHub(FakeGitHub):
            async def ensure_label(self, owner: str, repo: str, label: str) -> None:  # type: ignore[override]
                self.ensure_calls.append((owner, repo, label))

            async def add_issue_label(self, owner: str, repo: str, number: int, label: str) -> None:  # type: ignore[override]
                self.add_calls.append((owner, repo, number, label))

            async def remove_issue_label(self, owner: str, repo: str, number: int, label: str) -> None:  # type: ignore[override]
                self.remove_calls.append((owner, repo, number, label))

        github = AsyncGitHub()
        asyncio.run(ensure_operational_labels("nous", "runner", github))
        asyncio.run(mark_child_started(IssueKey("nous", "runner", 5), github))
        asyncio.run(mark_child_done(IssueKey("nous", "runner", 5), github))
        self.assertEqual(github.ensure_calls, [("nous", "runner", "agent:in-progress"), ("nous", "runner", "agent:done")])
        self.assertEqual(github.add_calls, [("nous", "runner", 5, "agent:in-progress"), ("nous", "runner", 5, "agent:done")])
        self.assertEqual(github.remove_calls, [("nous", "runner", 5, "agent:in-progress")])


if __name__ == "__main__":
    unittest.main()
