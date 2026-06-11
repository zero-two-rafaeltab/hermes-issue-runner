from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import types
from types import SimpleNamespace
import unittest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from issue_runner.child_run import build_auditable_child_prompt, prepare_child_run, start_child_issue_session
from issue_runner.selection import GitHubIssue, IssueKey


class ChildRunTests(unittest.TestCase):
    def _parent(self) -> IssueKey:
        return IssueKey("zero-two-rafaeltab", "hermes-issue-runner", 1)

    def _child(self) -> GitHubIssue:
        return GitHubIssue(
            owner="zero-two-rafaeltab",
            repo="hermes-issue-runner",
            number=6,
            title="Run one child issue end-to-end through the auditable implementation prompt",
            body="## Acceptance criteria\n\n- [ ] The child-agent prompt is self-contained.\n",
            labels=("ready-for-agent",),
        )

    def test_prompt_is_self_contained_and_requires_auditable_loop(self) -> None:
        prompt = build_auditable_child_prompt(parent=self._parent(), child=self._child())
        self.assertIn("auditable-implementation-review-loop", prompt)
        self.assertIn("git fetch origin", prompt)
        self.assertIn("git pull --ff-only origin main", prompt)
        self.assertIn("Create `feat/issue-6-run-one-child-issue-end-to-end`", prompt)
        self.assertIn("open a draft PR targeting `main`", prompt)
        self.assertIn("final verdict is `APPROVED`", prompt)
        self.assertIn("mark the draft PR ready", prompt)
        self.assertIn("agent:in-progress` to `agent:done`", prompt)
        self.assertIn("If the review loop returns `REQUEST_CHANGES`", prompt)
        self.assertIn("Do not merge PRs", prompt)
        self.assertIn("## Acceptance criteria", prompt)

    def test_prepare_child_run_uses_deterministic_metadata(self) -> None:
        plan = prepare_child_run(parent=self._parent(), child=self._child())
        self.assertEqual(plan.branch_name, "feat/issue-6-run-one-child-issue-end-to-end")
        self.assertEqual(plan.base_branch, "main")
        self.assertEqual(plan.pr_base, "main")
        self.assertEqual(plan.idempotency_key, "github:zero-two-rafaeltab/hermes-issue-runner/issues/6:attempt:1")
        self.assertEqual(plan.title, "Issue #6: Run one child issue end-to-end through the auditable implementation prompt")

    def test_start_child_issue_session_calls_gateway_child_session_seam(self) -> None:
        calls = []

        async def start_child_session(request):
            calls.append(request)
            return {"scheduled": True, "thread_id": "t1"}

        gateway = SimpleNamespace(
            start_child_session=start_child_session,
            child_session_request_factory=lambda **kwargs: kwargs,
        )
        result = asyncio.run(
            start_child_issue_session(
                gateway=gateway,
                event=SimpleNamespace(source="discord"),
                parent=self._parent(),
                child=self._child(),
            )
        )

        self.assertEqual(result.result, {"scheduled": True, "thread_id": "t1"})
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertEqual(request["child_title"], result.plan.title)
        self.assertEqual(request["idempotency_key"], result.plan.idempotency_key)
        self.assertEqual(request["metadata"]["source"], "hermes-issue-runner")
        self.assertEqual(request["metadata"]["child_issue"], "zero-two-rafaeltab/hermes-issue-runner#6")
        self.assertIn("auditable-implementation-review-loop", request["starter_prompt"])

    def test_start_child_issue_session_builds_gateway_request_when_available(self) -> None:
        calls = []

        class GatewayChildSessionRequest:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        gateway_module = types.ModuleType("gateway")
        child_session_module = types.ModuleType("gateway.child_session")
        child_session_module.GatewayChildSessionRequest = GatewayChildSessionRequest
        old_gateway = sys.modules.get("gateway")
        old_child_session = sys.modules.get("gateway.child_session")
        sys.modules["gateway"] = gateway_module
        sys.modules["gateway.child_session"] = child_session_module

        def restore_modules() -> None:
            if old_gateway is None:
                sys.modules.pop("gateway", None)
            else:
                sys.modules["gateway"] = old_gateway
            if old_child_session is None:
                sys.modules.pop("gateway.child_session", None)
            else:
                sys.modules["gateway.child_session"] = old_child_session

        self.addCleanup(restore_modules)

        async def start_child_session(request):
            calls.append(request)
            return {"scheduled": True, "thread_id": "t1"}

        gateway = SimpleNamespace(start_child_session=start_child_session)
        result = asyncio.run(
            start_child_issue_session(
                gateway=gateway,
                event=SimpleNamespace(source="discord"),
                parent=self._parent(),
                child=self._child(),
            )
        )

        self.assertEqual(result.result, {"scheduled": True, "thread_id": "t1"})
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertIsInstance(request, GatewayChildSessionRequest)
        self.assertEqual(request.child_title, result.plan.title)
        self.assertEqual(request.idempotency_key, result.plan.idempotency_key)
        self.assertEqual(request.metadata["source"], "hermes-issue-runner")
        self.assertEqual(request.metadata["child_issue"], "zero-two-rafaeltab/hermes-issue-runner#6")
        self.assertIn("auditable-implementation-review-loop", request.starter_prompt)

    def test_start_child_issue_session_requires_gateway_seam(self) -> None:
        with self.assertRaisesRegex(TypeError, "start_child_session"):
            asyncio.run(
                start_child_issue_session(
                    gateway=SimpleNamespace(),
                    event=SimpleNamespace(),
                    parent=self._parent(),
                    child=self._child(),
                )
            )


if __name__ == "__main__":
    unittest.main()
