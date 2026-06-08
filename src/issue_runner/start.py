"""Minimal start-command implementation for Hermes Issue Runner.

The module is intentionally stdlib-only and adapter-friendly: Discord/Hermes and
GitHub behavior enter through small injected callables/objects so unit tests do
not require live services.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from .branch_prep import plan_branch_preparation
from .child_run import prepare_child_run, start_child_issue_session
from .failure import FailureRecoveryRequest, format_failure_prompt, wait_for_recovery_decision
from .selection import IssueKey, select_next_child
from .state import check_duplicate_run, ensure_operational_labels, mark_child_done, mark_child_started

SLASH_COMMANDS = ("/issue-runner", "/issue_runner")
START_WORD_RE = re.compile(r"\b(?:start|run|begin)\b", re.IGNORECASE)
RESUME_WORD_RE = re.compile(r"\b(?:resume|continue)\b", re.IGNORECASE)
ISSUE_REF_RE = re.compile(
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)"
)
GITHUB_ISSUE_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/issues/(?P<number>[1-9][0-9]*)(?:[#?][^\s>]*)?(?=$|[\s>])",
    re.IGNORECASE,
)


class IssueReferenceError(ValueError):
    """Raised when a parent issue reference cannot be parsed."""


@dataclass(frozen=True)
class IssueReference:
    owner: str
    repo: str
    number: int

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class StartCommand:
    reference: IssueReference
    mode: str
    action: str = "start"


@dataclass(frozen=True)
class StartIntent:
    mode: str
    action: str = "start"


@dataclass(frozen=True)
class ParentIssue:
    owner: str
    repo: str
    number: int
    title: str

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class ParentRunResult:
    """Outcome of advancing a parent issue run through runnable children."""

    parent: IssueKey
    final_selection: Any
    started_runs: tuple[Any, ...]
    completed_children: tuple[IssueKey, ...]

    @property
    def first_started_run(self) -> Any | None:
        return self.started_runs[0] if self.started_runs else None

    @property
    def pending_run(self) -> Any | None:
        if not self.started_runs:
            return None
        last_run = self.started_runs[-1]
        return None if _child_run_completed(last_run) else last_run


def _truthy_status(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"approved", "complete", "completed", "done", "agent:done"}
    return bool(value)


def _value_from_result(result: Any, name: str) -> Any:
    if isinstance(result, dict):
        return result.get(name)
    return getattr(result, name, None)


def _child_run_plan(child_run: Any) -> Any:
    return _value_from_result(child_run, "plan")


def _child_run_completed(child_run: Any) -> bool:
    """Return whether a child session result means the child reached agent:done.

    The live gateway seam may only acknowledge scheduling, in which case the
    parent loop stops after the fresh child session starts. Tests and future
    adapters can return an explicit completion signal after a child session has
    finished so the loop can re-read GitHub state and advance without relying on
    one long agent context.
    """

    for candidate in (child_run, _value_from_result(child_run, "result")):
        if candidate is None:
            continue
        for name in ("completed_child_issue", "agent_done"):
            value = _value_from_result(candidate, name)
            if value is not None:
                return _truthy_status(value)
        status = _value_from_result(candidate, "status")
        if status is not None:
            return str(status).strip().casefold() in {"agent:done", "child_completed"}
    return False


class ChildStartupError(RuntimeError):
    """Raised when branch prep or child session startup fails after selection."""

    def __init__(self, reason: str, message: str, child: Any | None = None, parent: IssueKey | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.child = child
        self.parent = parent


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def parse_issue_reference(text: str) -> IssueReference:
    """Parse ``owner/repo#N`` or a GitHub issue URL from arbitrary text."""
    raw = (text or "").strip()
    url_match = GITHUB_ISSUE_URL_RE.search(raw)
    if url_match:
        return IssueReference(
            owner=url_match.group("owner"),
            repo=url_match.group("repo"),
            number=int(url_match.group("number")),
        )

    ref_match = ISSUE_REF_RE.search(raw)
    if ref_match:
        return IssueReference(
            owner=ref_match.group("owner"),
            repo=ref_match.group("repo"),
            number=int(ref_match.group("number")),
        )

    # Give a more specific error for GitHub URLs that are not issue URLs.
    parsed = urlparse(raw.split()[0] if raw.split() else raw)
    if parsed.netloc.lower() == "github.com":
        raise IssueReferenceError(
            "Expected a GitHub issue URL like https://github.com/owner/repo/issues/1."
        )
    raise IssueReferenceError("Expected a parent issue reference like owner/repo#1 or a GitHub issue URL.")


def _slash_command_parts(raw: str) -> tuple[str, str] | None:
    lowered = raw.lower()
    for command in SLASH_COMMANDS:
        if lowered == command:
            return command, ""
        if lowered.startswith(f"{command} "):
            return command, raw[len(command) :].strip()
    return None


def _slash_action(raw: str) -> str | None:
    parts = _slash_command_parts(raw)
    if parts is None:
        return None
    _command, remainder = parts
    first_word = remainder.split(maxsplit=1)[0].lower() if remainder else ""
    if first_word in {"start", "resume", "continue"}:
        return first_word
    return None


def _looks_like_natural_start(raw: str) -> bool:
    if not START_WORD_RE.search(raw):
        return False
    if not re.search(r"\bissue[- ]?runner\b", raw, re.IGNORECASE):
        return False
    # Natural-language support is deliberately mention-oriented so ordinary chat
    # about the issue runner does not get consumed as a command.
    return bool(re.search(r"<@!?\d+>|@\w+", raw))


def _looks_like_natural_resume(raw: str) -> bool:
    if not RESUME_WORD_RE.search(raw):
        return False
    if not re.search(r"\bissue[- ]?runner\b", raw, re.IGNORECASE):
        return False
    return bool(re.search(r"<@!?\d+>|@\w+", raw))


def detect_start_intent(text: str) -> StartIntent | None:
    """Detect whether text is an issue-runner command without parsing refs."""
    raw = (text or "").strip()
    if not raw:
        return None
    slash_action = _slash_action(raw)
    if slash_action is not None:
        return StartIntent(mode="slash", action=slash_action)
    if _looks_like_natural_start(raw):
        return StartIntent(mode="mention", action="start")
    if _looks_like_natural_resume(raw):
        return StartIntent(mode="mention", action="resume")
    return None


def parse_issue_runner_command(text: str) -> StartCommand | None:
    """Return a start/resume command for slash-command or natural mention text.

    Non-command text returns ``None``. Command-shaped text with a bad/missing
    parent issue raises ``IssueReferenceError`` so callers can reply with an
    actionable Discord error.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    intent = detect_start_intent(raw)
    if intent is None:
        return None
    return StartCommand(reference=parse_issue_reference(raw), mode=intent.mode, action=intent.action)


def parse_start_command(text: str) -> StartCommand | None:
    """Return only start commands, preserving the historical parser API."""
    command = parse_issue_runner_command(text)
    if command is not None and command.action == "start":
        return command
    return None


def default_authorization_checker(event: Any, gateway: Any) -> Any | None:
    """Reuse Hermes gateway authorization when available.

    Returning ``None`` means no injected/core checker was discoverable; callers
    may decide whether to defer to Hermes core or reject. The handler rejects for
    safety because this command would otherwise start GitHub work.
    """
    for name in ("_is_user_authorized", "is_user_authorized"):
        checker = getattr(gateway, name, None)
        if callable(checker):
            return checker(getattr(event, "source", None))
    return None


def _platform_value(event: Any) -> str:
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", "")
    return str(getattr(platform, "value", platform) or "").lower()


def _resolve_adapter(event: Any, gateway: Any) -> Any | None:
    adapters = getattr(gateway, "adapters", None)
    if not isinstance(adapters, dict):
        return None
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    candidates = (platform, getattr(platform, "value", None), str(getattr(platform, "value", platform) or ""))
    for candidate in candidates:
        try:
            if candidate in adapters:
                return adapters[candidate]
        except TypeError:
            continue
    return adapters.get(str(getattr(platform, "value", platform) or "").lower())


async def send_discord_reply(event: Any, gateway: Any, message: str) -> bool:
    """Best-effort Discord reply through the Hermes adapter send seam."""
    adapter = _resolve_adapter(event, gateway)
    send = getattr(adapter, "send", None)
    if not callable(send):
        return False
    source = getattr(event, "source", None)
    chat_id = getattr(source, "chat_id", None)
    if not chat_id:
        return False
    metadata = None
    thread_id = getattr(source, "thread_id", None)
    if thread_id:
        metadata = {"thread_id": thread_id}
    try:
        result = send(chat_id, message, metadata=metadata)
    except TypeError:
        result = send(chat_id, message)
    await _maybe_await(result)
    return True


def _coerce_parent_issue(reference: IssueReference, payload: Any) -> ParentIssue:
    if isinstance(payload, ParentIssue):
        return payload
    if isinstance(payload, dict):
        title = payload.get("title")
        owner = payload.get("owner", reference.owner)
        repo = payload.get("repo", reference.repo)
        number = payload.get("number", reference.number)
    else:
        title = getattr(payload, "title", None)
        owner = getattr(payload, "owner", reference.owner)
        repo = getattr(payload, "repo", reference.repo)
        number = getattr(payload, "number", reference.number)
    if not title:
        raise ValueError(f"GitHub issue {reference.repository}#{reference.number} did not include a title.")
    return ParentIssue(owner=str(owner), repo=str(repo), number=int(number), title=str(title))


def _format_parent_run_reply(parent: ParentIssue, run_result: ParentRunResult) -> str:
    selection = run_result.final_selection
    child_run = run_result.pending_run or run_result.first_started_run
    completed_summary = ""
    if run_result.completed_children:
        completed_summary = "\nCompleted child runs: " + ", ".join(f"#{child.number}" for child in run_result.completed_children) + "."
    return (
        "Resolved Hermes Issue Runner parent issue:\n"
        f"Repository: {parent.repository}\n"
        f"Parent issue: #{parent.number}\n"
        f"Title: {parent.title}\n"
        f"Child selection: {selection.message}"
        + completed_summary
        + (
            "\nChild run: started gateway child session "
            f"for #{selection.selected.number} on branch {_child_run_plan(child_run).branch_name}."
            if selection.has_runnable_child and child_run is not None and run_result.pending_run is not None
            else ""
        )
    )


def _handler_result(run_result: ParentRunResult) -> dict[str, str]:
    selection = run_result.final_selection
    if run_result.pending_run is not None:
        assert selection.selected is not None
        return {
            "action": "skip",
            "reason": "issue-runner child run started",
            "child": str(selection.selected.number),
        }
    return {"action": "skip", "reason": selection.status}


class StartCommandHandler:
    """Handle the minimal issue-runner start command.

    ``github_client`` may expose ``get_issue(owner, repo, number)`` or itself be
    a callable with those arguments. ``authorization_checker`` receives
    ``(event, gateway)`` and should return a truthy value for allowed users.
    """

    def __init__(
        self,
        github_client: Any,
        authorization_checker: Callable[[Any, Any], Any] | None = None,
        reply_sender: Callable[[Any, Any, str], Any] | None = None,
        child_session_starter: Callable[..., Any] | None = None,
        branch_preparer: Callable[..., Any] | None = None,
        recovery_response_waiter: Callable[..., Any] | None = None,
        git_client: Any | None = None,
    ) -> None:
        self.github_client = github_client
        self.git_client = git_client
        self.authorization_checker = authorization_checker or default_authorization_checker
        self.reply_sender = reply_sender or send_discord_reply
        self.child_session_starter = child_session_starter or start_child_issue_session
        self.branch_preparer = branch_preparer or plan_branch_preparation
        self.recovery_response_waiter = recovery_response_waiter

    async def handle(self, event: Any, gateway: Any) -> dict[str, str] | None:
        if _platform_value(event) != "discord":
            return None

        text = getattr(event, "text", "")
        intent = detect_start_intent(text)
        if intent is None:
            return None

        authorized = await _maybe_await(self.authorization_checker(event, gateway))
        if not bool(authorized):
            await _maybe_await(
                self.reply_sender(
                    event,
                    gateway,
                    "You are not authorized to start Hermes Issue Runner from Discord.",
                )
            )
            return {"action": "skip", "reason": "unauthorized"}

        try:
            command = parse_issue_runner_command(text)
        except IssueReferenceError as exc:
            await _maybe_await(self.reply_sender(event, gateway, f"Invalid parent issue reference: {exc}"))
            return {"action": "skip", "reason": "invalid parent issue reference"}

        if command is None:
            return None

        try:
            issue_payload = await _maybe_await(self._get_issue(command.reference))
            parent = _coerce_parent_issue(command.reference, issue_payload)
        except Exception as exc:
            await _maybe_await(
                self.reply_sender(
                    event,
                    gateway,
                    f"Unable to resolve GitHub issue {command.reference.repository}#{command.reference.number}: {exc}",
                )
            )
            return {"action": "skip", "reason": "github issue lookup failed"}

        parent_key = IssueKey(parent.owner, parent.repo, parent.number)
        try:
            if command.action in {"resume", "continue"}:
                run_result = await self.resume_parent(event=event, gateway=gateway, parent=parent_key)
            else:
                await _maybe_await(ensure_operational_labels(parent.owner, parent.repo, self.github_client))
                duplicate_guard = await check_duplicate_run(parent_key, self.github_client)
                if duplicate_guard.has_incomplete_state:
                    await _maybe_await(self.reply_sender(event, gateway, duplicate_guard.message))
                    return {"action": "skip", "reason": "duplicate run in progress"}
                run_result = await self._run_parent_loop(event=event, gateway=gateway, parent=parent)
        except ChildStartupError as exc:
            if self.recovery_response_waiter is not None:
                decision = await self._pause_for_failure_recovery(event=event, gateway=gateway, failure=exc)
                return {"action": "skip", "reason": f"failure recovery: {decision.command}"}
            request = self._failure_recovery_request(parent_key, exc)
            await _maybe_await(self.reply_sender(event, gateway, str(exc)))
            await _maybe_await(self.reply_sender(event, gateway, format_failure_prompt(request)))
            return {"action": "skip", "reason": "failure pause pending"}
        except Exception as exc:
            await _maybe_await(self.reply_sender(event, gateway, f"Unable to advance parent issue {parent_key.ref}: {exc}"))
            return {"action": "skip", "reason": "parent run failed"}
        await _maybe_await(self.reply_sender(event, gateway, _format_parent_run_reply(parent, run_result)))
        return _handler_result(run_result)

    def _get_issue(self, reference: IssueReference) -> Any:
        getter = getattr(self.github_client, "get_issue", None)
        if callable(getter):
            return getter(reference.owner, reference.repo, reference.number)
        if callable(self.github_client):
            return self.github_client(reference.owner, reference.repo, reference.number)
        raise TypeError("github_client must be callable or expose get_issue(owner, repo, number)")

    def _failure_recovery_request(self, parent: IssueKey, failure: ChildStartupError) -> FailureRecoveryRequest:
        child = failure.child
        child_ref = getattr(child, "ref", None)
        if child_ref is None and child is not None:
            child_ref = f"{parent.repository}#{getattr(child, 'number', '?')}"
        return FailureRecoveryRequest(
            parent_issue=parent.ref,
            child_issue=str(child_ref or "unknown child"),
            failure_summary=str(failure),
        )

    async def _pause_for_failure_recovery(self, *, event: Any, gateway: Any, failure: ChildStartupError) -> Any:
        assert self.recovery_response_waiter is not None
        parent = failure.parent or IssueKey("unknown", "unknown", 0)
        return await wait_for_recovery_decision(
            request=self._failure_recovery_request(parent, failure),
            event=event,
            gateway=gateway,
            prompt_sender=self.reply_sender,
            response_waiter=self.recovery_response_waiter,
            authorization_checker=self.authorization_checker,
        )

    async def _select_next_child(self, parent: ParentIssue) -> Any:
        return await select_next_child(IssueKey(parent.owner, parent.repo, parent.number), self.github_client)

    async def _start_selected_child(self, *, event: Any, gateway: Any, parent_key: IssueKey, child: Any) -> Any:
        prepared_plan = prepare_child_run(parent=parent_key, child=child)
        await _maybe_await(mark_child_started(child, self.github_client))
        try:
            branch_preparation = await _maybe_await(
                self.branch_preparer(
                    child=child,
                    child_branch=prepared_plan.branch_name,
                    github_client=self.github_client,
                    git_client=self.git_client,
                    pr_base="main",
                )
            )
        except Exception as exc:
            raise ChildStartupError(
                "branch preparation failed",
                f"Unable to prepare branch for child #{child.number}; paused with in-progress label preserved: {exc}",
                child=child,
                parent=parent_key,
            ) from exc

        try:
            child_run = await _maybe_await(
                self.child_session_starter(
                    gateway=gateway,
                    event=event,
                    parent=parent_key,
                    child=child,
                    base_branch=branch_preparation.base_branch,
                    pr_base=branch_preparation.pr_base,
                    branch_preparation=branch_preparation,
                )
            )
        except Exception as exc:
            raise ChildStartupError(
                "child session startup failed",
                f"Unable to start child session for child #{child.number}; paused with in-progress label preserved: {exc}",
                child=child,
                parent=parent_key,
            ) from exc
        return child_run

    async def resume_parent(self, *, event: Any, gateway: Any, parent: IssueKey | IssueReference) -> ParentRunResult:
        """Re-enter a parent run from fresh GitHub state after a child finishes.

        This continuation path intentionally re-fetches the parent issue and
        re-runs duplicate checks instead of relying on any in-memory queue from a
        previous handler invocation.
        """

        reference = IssueReference(parent.owner, parent.repo, parent.number)
        issue_payload = await _maybe_await(self._get_issue(reference))
        parent_issue = _coerce_parent_issue(reference, issue_payload)
        parent_key = IssueKey(parent_issue.owner, parent_issue.repo, parent_issue.number)
        await _maybe_await(ensure_operational_labels(parent_issue.owner, parent_issue.repo, self.github_client))
        duplicate_guard = await check_duplicate_run(parent_key, self.github_client)
        if duplicate_guard.has_incomplete_state:
            raise ChildStartupError("duplicate run in progress", duplicate_guard.message)
        return await self._run_parent_loop(event=event, gateway=gateway, parent=parent_issue)

    async def _run_parent_loop(self, *, event: Any, gateway: Any, parent: ParentIssue) -> ParentRunResult:
        """Advance a parent run while child sessions report completion.

        Each iteration re-selects from GitHub rather than retaining a precomputed
        child list. A normal gateway scheduling acknowledgement starts exactly one
        fresh child session and leaves the loop pending; an explicit completion
        signal allows controlled/test adapters to continue to the next unblocked
        ready child.
        """

        parent_key = IssueKey(parent.owner, parent.repo, parent.number)
        started_runs: list[Any] = []
        completed_children: list[IssueKey] = []
        max_iterations = 100
        for _ in range(max_iterations):
            selection = await self._select_next_child(parent)
            if not selection.has_runnable_child:
                return ParentRunResult(parent_key, selection, tuple(started_runs), tuple(completed_children))
            assert selection.selected is not None
            child_run = await self._start_selected_child(
                event=event,
                gateway=gateway,
                parent_key=parent_key,
                child=selection.selected,
            )
            started_runs.append(child_run)
            if not _child_run_completed(child_run):
                return ParentRunResult(parent_key, selection, tuple(started_runs), tuple(completed_children))
            await _maybe_await(mark_child_done(selection.selected, self.github_client))
            completed_children.append(selection.selected.key)
        raise RuntimeError("Parent run loop exceeded 100 child iterations without reaching a stable state")

    async def complete_child(self, child: IssueKey) -> None:
        """Mark a successfully completed child done through the operational state model."""

        await _maybe_await(mark_child_done(child, self.github_client))
