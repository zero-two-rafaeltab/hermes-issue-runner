"""Prototype Hermes plugin for issue-runner Discord child-session spike.

Install location for manual testing:
    ~/.hermes/plugins/issue_runner_spike/

The plugin deliberately avoids private Hermes internals for production behavior.
It catches a text trigger through pre_gateway_dispatch and calls the proposed
future gateway.start_child_session() seam when present. If the seam is absent it
returns a diagnostic instead of pretending the Discord live-streaming flow was
proved.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

TRIGGER = "/issue-runner-spike"


@dataclass(frozen=True)
class SpikeRequest:
    title: str
    prompt: str
    idempotency_key: str | None = None


def register(ctx: Any) -> None:
    """Register the gateway pre-dispatch hook with Hermes."""
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)


def _platform_value(event: Any) -> str:
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", "")
    return str(getattr(platform, "value", platform) or "").lower()


def _parse_trigger(text: str) -> SpikeRequest | None:
    raw = (text or "").strip()
    if not raw.startswith(TRIGGER):
        return None
    rest = raw[len(TRIGGER):].strip()
    if not rest:
        rest = "Run the gateway-native child-session spike smoke task and stream progress here."
    title = "issue-runner spike child session"
    return SpikeRequest(title=title, prompt=rest, idempotency_key=f"spike:{hash(rest)}")


async def pre_gateway_dispatch(event: Any, gateway: Any, session_store: Any = None) -> dict[str, str] | None:
    """Handle `/issue-runner-spike` as an equivalent Discord test trigger.

    Expected future gateway API:
        await gateway.start_child_session(request)

    Current behavior without that API:
        Return a clear message explaining the missing seam.
    """
    request = _parse_trigger(getattr(event, "text", ""))
    if request is None:
        return None

    if _platform_value(event) != "discord":
        return {
            "action": "skip",
            "reason": "issue-runner-spike non-discord trigger",
            "message": "`/issue-runner-spike` must be run from Discord for this spike.",
        }

    start_child_session = getattr(gateway, "start_child_session", None)
    if callable(start_child_session):
        # Shape intentionally mirrors the technical note while avoiding an import
        # dependency on a not-yet-existing Hermes core dataclass.
        maybe_result = start_child_session(
            {
                "parent_event": event,
                "title": request.title,
                "prompt": request.prompt,
                "platform": "discord",
                "thread_kind": "discord_public_thread",
                "idempotency_key": request.idempotency_key,
                "auto_archive_duration": 1440,
            }
        )
        result = await maybe_result if inspect.isawaitable(maybe_result) else maybe_result
        thread_id = None
        if isinstance(result, dict):
            thread_id = result.get("thread_id")
        else:
            thread_id = getattr(result, "thread_id", None)
        link = f" <#{thread_id}>" if thread_id else ""
        return {
            "action": "skip",
            "reason": "issue-runner-spike child session started",
            "message": f"Started issue-runner spike child session{link}.",
        }

    return {
        "action": "skip",
        "reason": "missing gateway.start_child_session seam",
        "message": (
            "Issue-runner spike reached the plugin trigger, but this Hermes "
            "build does not expose `gateway.start_child_session(...)`. The "
            "Discord adapter has private thread/session helpers; issue #2's "
            "technical note documents the minimal core extension needed before "
            "a pure plugin can safely prove live child-session streaming."
        ),
    }
