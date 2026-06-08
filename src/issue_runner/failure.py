"""Failure pause and strict Discord recovery command handling.

The MVP failure model is intentionally small: when an implementation attempt
fails, the runner posts one actionable prompt in the failed attempt's Discord
context and waits without a timeout for an authorized single-word reply.  Only
``retry`` and ``stop`` are recovery commands; retry attempt creation is a later
lifecycle step, while this module provides the auditable pause/decision seam.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Literal

RecoveryCommand = Literal["retry", "stop"]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass(frozen=True)
class FailureRecoveryRequest:
    """Context for a failed child attempt recovery pause."""

    parent_issue: str
    child_issue: str
    failure_summary: str
    thread_url: str | None = None
    attempt: int = 1


@dataclass(frozen=True)
class FailureRecoveryDecision:
    """An accepted authorized recovery command."""

    command: RecoveryCommand
    responder_id: str | None = None
    raw_text: str = ""

    @property
    def should_retry(self) -> bool:
        return self.command == "retry"

    @property
    def should_stop(self) -> bool:
        return self.command == "stop"


def parse_recovery_command(text: str) -> RecoveryCommand | None:
    """Return a strict recovery command for exact single-word lowercase input.

    The MVP deliberately rejects natural-language variants, mixed case, and
    extra words such as ``retry please`` or ``skip`` so discussion in the failure
    thread cannot accidentally advance automation.
    """

    stripped = (text or "").strip()
    if stripped in {"retry", "stop"}:
        return stripped  # type: ignore[return-value]
    return None


def format_failure_prompt(request: FailureRecoveryRequest) -> str:
    """Build the stable actionable Discord failure prompt."""

    location = f"\nFailure thread: {request.thread_url}" if request.thread_url else ""
    return (
        "Hermes Issue Runner paused after a child attempt failure.\n"
        f"Parent: {request.parent_issue}\n"
        f"Child: {request.child_issue}\n"
        f"Attempt: {request.attempt}\n"
        f"Failure: {request.failure_summary}{location}\n"
        "Reply in this thread with exactly one word from an authorized user: `retry` or `stop`. "
        "`skip` and natural-language variants are ignored."
    )


def _event_user_id(event: Any) -> str | None:
    source = getattr(event, "source", None)
    value = getattr(source, "user_id", None)
    return None if value is None else str(value)


async def _authorized(auth_checker: Callable[[Any, Any], Any] | None, event: Any, gateway: Any) -> bool:
    if auth_checker is None:
        return False
    return bool(await _maybe_await(auth_checker(event, gateway)))


async def wait_for_recovery_decision(
    *,
    request: FailureRecoveryRequest,
    event: Any,
    gateway: Any,
    prompt_sender: Callable[[Any, Any, str], Any],
    response_waiter: Callable[..., Any],
    authorization_checker: Callable[[Any, Any], Any] | None,
) -> FailureRecoveryDecision:
    """Post the failure prompt and wait indefinitely for an authorized command.

    ``response_waiter`` is an adapter seam supplied by the Discord/Hermes layer.
    It must block until the next candidate reply in the failure context and
    return an event-like object with ``text`` and ``source.user_id``.  This
    function intentionally does not pass a timeout.
    """

    await _maybe_await(prompt_sender(event, gateway, format_failure_prompt(request)))

    while True:
        try:
            candidate = response_waiter(request=request, event=event, gateway=gateway)
        except TypeError:
            candidate = response_waiter(event, gateway)
        reply_event = await _maybe_await(candidate)
        if reply_event is None:
            continue
        if not await _authorized(authorization_checker, reply_event, gateway):
            continue
        command = parse_recovery_command(getattr(reply_event, "text", ""))
        if command is None:
            continue
        return FailureRecoveryDecision(
            command=command,
            responder_id=_event_user_id(reply_event),
            raw_text=str(getattr(reply_event, "text", "")),
        )
