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

from .child_run import start_child_issue_session
from .selection import IssueKey, select_next_child
from .state import check_duplicate_run, ensure_operational_labels, mark_child_done, mark_child_started

SLASH_COMMANDS = ("/issue-runner", "/issue_runner")
START_WORD_RE = re.compile(r"\b(?:start|run|begin)\b", re.IGNORECASE)
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


@dataclass(frozen=True)
class StartIntent:
    mode: str


@dataclass(frozen=True)
class ParentIssue:
    owner: str
    repo: str
    number: int
    title: str

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"


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


def _is_slash_start(raw: str) -> bool:
    parts = _slash_command_parts(raw)
    if parts is None:
        return False
    _command, remainder = parts
    first_word = remainder.split(maxsplit=1)[0].lower() if remainder else ""
    return first_word == "start"


def _looks_like_natural_start(raw: str) -> bool:
    if not START_WORD_RE.search(raw):
        return False
    if not re.search(r"\bissue[- ]?runner\b", raw, re.IGNORECASE):
        return False
    # Natural-language support is deliberately mention-oriented so ordinary chat
    # about the issue runner does not get consumed as a command.
    return bool(re.search(r"<@!?\d+>|@\w+", raw))


def detect_start_intent(text: str) -> StartIntent | None:
    """Detect whether text is an issue-runner start request without parsing refs."""
    raw = (text or "").strip()
    if not raw:
        return None
    if _is_slash_start(raw):
        return StartIntent(mode="slash")
    if _looks_like_natural_start(raw):
        return StartIntent(mode="mention")
    return None


def parse_start_command(text: str) -> StartCommand | None:
    """Return a start command for slash-command or natural mention text.

    Non-command text returns ``None``. Command-shaped text with a bad/missing
    parent issue raises ``IssueReferenceError`` so callers can reply with an
    actionable Discord error.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    intent = detect_start_intent(raw)
    if intent and intent.mode == "slash":
        return StartCommand(reference=parse_issue_reference(raw), mode="slash")

    if intent and intent.mode == "mention":
        return StartCommand(reference=parse_issue_reference(raw), mode="mention")

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
    ) -> None:
        self.github_client = github_client
        self.authorization_checker = authorization_checker or default_authorization_checker
        self.reply_sender = reply_sender or send_discord_reply
        self.child_session_starter = child_session_starter or start_child_issue_session

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
            command = parse_start_command(text)
        except IssueReferenceError as exc:
            await _maybe_await(self.reply_sender(event, gateway, f"Invalid parent issue reference: {exc}"))
            return {"action": "skip", "reason": "invalid parent issue reference"}

        if command is None:
            return None

        child_run = None
        try:
            issue_payload = await _maybe_await(self._get_issue(command.reference))
            parent = _coerce_parent_issue(command.reference, issue_payload)
            parent_key = IssueKey(parent.owner, parent.repo, parent.number)
            await _maybe_await(ensure_operational_labels(parent.owner, parent.repo, self.github_client))
            duplicate_guard = await check_duplicate_run(parent_key, self.github_client)
            if duplicate_guard.has_incomplete_state:
                await _maybe_await(self.reply_sender(event, gateway, duplicate_guard.message))
                return {"action": "skip", "reason": "duplicate run in progress"}
            selection = await self._select_next_child(parent)
            if selection.has_runnable_child:
                assert selection.selected is not None
                await _maybe_await(mark_child_started(selection.selected, self.github_client))
                child_run = await _maybe_await(
                    self.child_session_starter(
                        gateway=gateway,
                        event=event,
                        parent=parent_key,
                        child=selection.selected,
                        base_branch="main",
                        pr_base="main",
                    )
                )
        except Exception as exc:
            await _maybe_await(
                self.reply_sender(
                    event,
                    gateway,
                    f"Unable to resolve GitHub issue {command.reference.repository}#{command.reference.number}: {exc}",
                )
            )
            return {"action": "skip", "reason": "github issue lookup failed"}
        await _maybe_await(
            self.reply_sender(
                event,
                gateway,
                (
                    "Resolved Hermes Issue Runner parent issue:\n"
                    f"Repository: {parent.repository}\n"
                    f"Parent issue: #{parent.number}\n"
                    f"Title: {parent.title}\n"
                    f"Child selection: {selection.message}"
                    + (
                        "\nChild run: started gateway child session "
                        f"for #{selection.selected.number} on branch {child_run.plan.branch_name}."
                        if selection.has_runnable_child and child_run is not None
                        else ""
                    )
                ),
            )
        )
        if selection.has_runnable_child:
            assert selection.selected is not None
            return {
                "action": "skip",
                "reason": "issue-runner child run started",
                "child": str(selection.selected.number),
            }
        return {"action": "skip", "reason": selection.status}

    def _get_issue(self, reference: IssueReference) -> Any:
        getter = getattr(self.github_client, "get_issue", None)
        if callable(getter):
            return getter(reference.owner, reference.repo, reference.number)
        if callable(self.github_client):
            return self.github_client(reference.owner, reference.repo, reference.number)
        raise TypeError("github_client must be callable or expose get_issue(owner, repo, number)")

    async def _select_next_child(self, parent: ParentIssue) -> Any:
        return await select_next_child(IssueKey(parent.owner, parent.repo, parent.number), self.github_client)

    async def complete_child(self, child: IssueKey) -> None:
        """Mark a successfully completed child done through the operational state model."""

        await _maybe_await(mark_child_done(child, self.github_client))
