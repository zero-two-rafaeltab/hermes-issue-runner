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
    failure_event: Any | None = None
    failure_source: Any | None = None
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


def _failure_context_event(request: FailureRecoveryRequest, fallback_event: Any) -> Any:
    if request.failure_event is not None:
        return request.failure_event
    if request.failure_source is not None:
        text = getattr(fallback_event, "text", "")
        return type("FailureRecoveryEvent", (), {"text": text, "source": request.failure_source})()
    return fallback_event


def _call_response_waiter(response_waiter: Callable[..., Any], **kwargs: Any) -> Any:
    """Call the recovery waiter using a documented shape, with legacy support.

    Preferred waiters accept keyword arguments including ``request``, ``event``,
    ``gateway``, ``failure_source``, and ``thread_url``. Older adapters accepted
    only positional ``(event, gateway)``; support that shape by inspecting the
    callable signature rather than swallowing arbitrary ``TypeError`` raised
    inside the waiter.
    """

    try:
        signature = inspect.signature(response_waiter)
    except (TypeError, ValueError):
        return response_waiter(**kwargs)

    parameters = list(signature.parameters.values())
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return response_waiter(**kwargs)

    keyword_arguments = {
        name: value
        for name, value in kwargs.items()
        if name in signature.parameters
        and signature.parameters[name].kind
        in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    }
    required_keyword_only = [
        parameter
        for parameter in parameters
        if parameter.default is inspect.Parameter.empty
        and parameter.kind is inspect.Parameter.KEYWORD_ONLY
        and parameter.name not in keyword_arguments
    ]
    if keyword_arguments or not required_keyword_only:
        try:
            signature.bind_partial(**keyword_arguments)
        except TypeError:
            pass
        else:
            return response_waiter(**keyword_arguments)

    positional_parameters = [
        parameter
        for parameter in parameters
        if parameter.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    if any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters) or len(positional_parameters) >= 2:
        return response_waiter(kwargs["event"], kwargs["gateway"])

    return response_waiter(**keyword_arguments)


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

    context_event = _failure_context_event(request, event)
    await _maybe_await(prompt_sender(context_event, gateway, format_failure_prompt(request)))

    while True:
        candidate = _call_response_waiter(
            response_waiter,
            request=request,
            event=context_event,
            gateway=gateway,
            failure_source=request.failure_source,
            thread_url=request.thread_url,
        )
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
