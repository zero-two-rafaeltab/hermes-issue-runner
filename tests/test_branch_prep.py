from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from issue_runner.branch_prep import BranchPreparationFailure, execute_branch_preparation, plan_branch_preparation
from issue_runner.child_run import build_auditable_child_prompt, prepare_child_run
from issue_runner.selection import GitHubIssue, IssueKey


class FakeGitHub:
    def __init__(self) -> None:
        self.issues: dict[int, GitHubIssue] = {}
        self.branches: dict[int, str] = {}

    def get_issue(self, owner: str, repo: str, number: int) -> GitHubIssue:
        return self.issues.get(number, GitHubIssue(owner, repo, number, body=""))

    def branch_for_issue(self, owner: str, repo: str, number: int) -> str:
        return self.branches.get(number, f"feat/issue-{number}")


class BranchPreparationTests(unittest.TestCase):
    def _child(self, number: int, body: str) -> GitHubIssue:
        return GitHubIssue("nous", "runner", number, title=f"Issue {number}", body=body, labels=("ready-for-agent",))

    def test_no_dependencies_branches_from_latest_main_and_targets_main(self) -> None:
        child = self._child(7, "## Acceptance criteria\n\n- [ ] Build it\n")
        plan = asyncio.run(
            plan_branch_preparation(child=child, child_branch="feat/issue-7", github_client=FakeGitHub())
        )

        self.assertFalse(plan.has_dependencies)
        self.assertEqual(plan.base_branch, "main")
        self.assertEqual(plan.pr_base, "main")
        self.assertEqual(
            plan.git_commands,
            (
                ("git", "fetch", "origin"),
                ("git", "checkout", "-B", "feat/issue-7", "origin/main"),
            ),
        )

    def test_dependency_child_uses_dependency_branch_but_pr_base_stays_main(self) -> None:
        github = FakeGitHub()
        github.branches[6] = "feat/issue-6-child-run-prompt"
        child = self._child(7, "## Blocked by\n\n- #6\n")

        plan = asyncio.run(plan_branch_preparation(child=child, child_branch="feat/issue-7", github_client=github))

        self.assertTrue(plan.has_dependencies)
        self.assertEqual(plan.base_branch, "feat/issue-6-child-run-prompt")
        self.assertEqual(plan.pr_base, "main")
        self.assertEqual(plan.additional_rebase_branches, ())
        self.assertEqual(
            plan.git_commands,
            (
                ("git", "fetch", "origin"),
                ("git", "checkout", "-B", "feat/issue-7", "origin/feat/issue-6-child-run-prompt"),
            ),
        )
        self.assertNotIn(("git", "checkout", "feat/issue-6-child-run-prompt"), plan.git_commands)
        self.assertNotIn(("git", "pull", "--ff-only", "origin", "feat/issue-6-child-run-prompt"), plan.git_commands)

    def test_multiple_dependencies_choose_fewest_rebases_from_issue_graph(self) -> None:
        github = FakeGitHub()
        github.branches.update({6: "feat/issue-6", 9: "feat/issue-9"})
        github.issues[9] = self._child(9, "## Blocked by\n\n- #6\n")
        child = self._child(10, "## Blocked by\n\n- #6\n- #9\n")

        plan = asyncio.run(plan_branch_preparation(child=child, child_branch="feat/issue-10", github_client=github))

        self.assertEqual(plan.base_branch, "feat/issue-9")
        self.assertEqual(plan.additional_rebase_count, 0)

    def test_multiple_dependencies_tie_breaks_lowest_issue_number(self) -> None:
        github = FakeGitHub()
        github.branches.update({6: "feat/issue-6", 9: "feat/issue-9"})
        child = self._child(10, "## Blocked by\n\n- #9\n- #6\n")

        plan = asyncio.run(plan_branch_preparation(child=child, child_branch="feat/issue-10", github_client=github))

        self.assertEqual(plan.base_branch, "feat/issue-6")
        self.assertEqual([item.branch for item in plan.additional_rebase_branches], ["feat/issue-9"])
        self.assertIn(("git", "checkout", "-B", "feat/issue-10", "origin/feat/issue-6"), plan.git_commands)
        self.assertIn(("git", "rebase", "origin/feat/issue-9"), plan.git_commands)
        self.assertNotIn(("git", "checkout", "feat/issue-9"), plan.git_commands)
        self.assertNotIn(("git", "pull", "--ff-only", "origin", "feat/issue-9"), plan.git_commands)

    def test_git_containment_can_verify_extra_coverage(self) -> None:
        github = FakeGitHub()
        github.branches.update({6: "feat/issue-6", 9: "feat/issue-9"})
        child = self._child(10, "## Blocked by\n\n- #6\n- #9\n")

        def branch_contains(ancestor_branch: str, branch: str) -> bool:
            return ancestor_branch == "feat/issue-9" and branch == "feat/issue-6"

        plan = asyncio.run(
            plan_branch_preparation(
                child=child,
                child_branch="feat/issue-10",
                github_client=github,
                git_client=SimpleNamespace(branch_contains=branch_contains),
            )
        )

        self.assertEqual(plan.base_branch, "feat/issue-6")
        self.assertEqual(plan.additional_rebase_branches, ())

    def test_child_prompt_includes_dependency_plan_and_rebase_boundaries(self) -> None:
        github = FakeGitHub()
        github.branches[6] = "feat/issue-6-child-run-prompt"
        child = self._child(7, "## Blocked by\n\n- #6\n")
        branch_plan = asyncio.run(plan_branch_preparation(child=child, child_branch="feat/issue-7", github_client=github))
        plan = prepare_child_run(parent=IssueKey("nous", "runner", 1), child=child, branch_preparation=branch_plan)
        prompt = build_auditable_child_prompt(
            parent=plan.parent,
            child=child,
            branch_name=plan.branch_name,
            base_branch=plan.base_branch,
            pr_base=plan.pr_base,
            branch_preparation=branch_plan,
        )

        self.assertIn("Dependency base branch: `feat/issue-6-child-run-prompt`", prompt)
        self.assertIn("Pull request base remains: `main`", prompt)
        self.assertIn("Mutate only the new child branch", prompt)
        self.assertIn("do not retarget, rebase, or otherwise mutate dependency PR branches", prompt)

    def test_execute_branch_preparation_turns_rebase_failure_into_pause_failure(self) -> None:
        child = self._child(10, "## Blocked by\n\n- #6\n- #9\n")
        github = FakeGitHub()
        github.branches.update({6: "feat/issue-6", 9: "feat/issue-9"})
        plan = asyncio.run(plan_branch_preparation(child=child, child_branch="feat/issue-10", github_client=github))
        calls: list[tuple[str, ...]] = []

        def run(command: tuple[str, ...]) -> SimpleNamespace:
            calls.append(command)
            return SimpleNamespace(returncode=1 if command[:2] == ("git", "rebase") else 0)

        with self.assertRaises(BranchPreparationFailure):
            asyncio.run(execute_branch_preparation(plan, run))
        self.assertIn(("git", "rebase", "origin/feat/issue-9"), calls)


if __name__ == "__main__":
    unittest.main()
