from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import unittest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from issue_runner.artifacts import (
    ensure_draft_pr_after_first_commit,
    ensure_failure_artifact,
    mark_pr_ready_after_review,
)
from issue_runner.child_run import prepare_child_run
from issue_runner.selection import GitHubIssue, IssueKey


class FakeGit:
    def __init__(self, dirty: bool) -> None:
        self.dirty = dirty
        self.commits: list[str] = []

    def has_file_changes(self) -> bool:
        return self.dirty

    def create_wip_commit(self, message: str) -> str:
        self.commits.append(message)
        self.dirty = False
        return "deadbeef"


class FakeGitHub:
    def __init__(self) -> None:
        self.created_prs: list[dict[str, object]] = []
        self.ready: list[int] = []
        self.comments: list[str] = []

    def create_draft_pull_request(self, owner: str, repo: str, **kwargs):
        self.created_prs.append({"owner": owner, "repo": repo, **kwargs})
        return {"number": 42 + len(self.created_prs), "url": f"https://github/pr/{42 + len(self.created_prs)}", "headRefName": kwargs["head"], "isDraft": True}

    def mark_pull_request_ready(self, number: int) -> None:
        self.ready.append(number)

    def comment_pull_request(self, *args, **kwargs) -> None:  # pragma: no cover - should not be called routinely
        self.comments.append(str(args or kwargs))


class ArtifactLifecycleTests(unittest.TestCase):
    def plan(self):
        child = GitHubIssue(
            owner="nous",
            repo="hermes-issue-runner",
            number=11,
            title="Harden GitHub artifact edge cases and PR lifecycle behavior",
            body="",
            labels=("ready-for-agent",),
        )
        return prepare_child_run(parent=IssueKey("nous", "hermes-issue-runner", 1), child=child)

    def test_normal_first_commit_creates_draft_pr_without_routine_comments(self) -> None:
        github = FakeGitHub()
        result = asyncio.run(ensure_draft_pr_after_first_commit(plan=self.plan(), github_client=github))

        self.assertEqual(result.outcome, "draft_pr")
        self.assertFalse(result.commit_created)
        self.assertEqual(len(github.created_prs), 1)
        self.assertTrue(github.created_prs[0]["draft"])
        self.assertEqual(github.comments, [])

    def test_changed_work_failure_creates_wip_commit_and_draft_pr(self) -> None:
        git = FakeGit(dirty=True)
        github = FakeGitHub()
        result = asyncio.run(ensure_failure_artifact(plan=self.plan(), git_client=git, github_client=github))

        self.assertEqual(result.outcome, "wip_draft_pr")
        self.assertTrue(result.commit_created)
        self.assertEqual(git.commits, ["wip: preserve failed attempt for issue 11"])
        self.assertEqual(len(github.created_prs), 1)
        self.assertTrue(github.created_prs[0]["draft"])
        self.assertEqual(github.comments, [])

    def test_no_change_failure_does_not_invent_commit_or_pr(self) -> None:
        git = FakeGit(dirty=False)
        github = FakeGitHub()
        result = asyncio.run(ensure_failure_artifact(plan=self.plan(), git_client=git, github_client=github))

        self.assertEqual(result.outcome, "no_artifact")
        self.assertFalse(result.commit_created)
        self.assertEqual(git.commits, [])
        self.assertEqual(github.created_prs, [])

    def test_pr_is_marked_ready_only_after_review_success_path(self) -> None:
        github = FakeGitHub()
        result = asyncio.run(ensure_draft_pr_after_first_commit(plan=self.plan(), github_client=github))
        assert result.pull_request is not None

        self.assertEqual(github.ready, [])
        asyncio.run(mark_pr_ready_after_review(pull_request=result.pull_request, github_client=github))
        self.assertEqual(github.ready, [43])


if __name__ == "__main__":
    unittest.main()
