"""Prototype Hermes plugin for issue-runner Discord child-session spike.

Install location for manual testing:
    ~/.hermes/plugins/issue_runner_spike/

The plugin deliberately avoids private Hermes internals for production behavior.
It catches a text trigger through pre_gateway_dispatch and calls the proposed
future gateway.start_child_session() seam when present. If the seam is absent it
sends a visible Discord diagnostic through the adapter when available instead of
pretending the Discord live-streaming flow was proved.
"""

from __future__ import annotations

import inspect
import hashlib
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
    digest = hashlib.sha256(rest.encode("utf-8")).hexdigest()[:16]
    return SpikeRequest(title=title, prompt=rest, idempotency_key=f"spike:{digest}")


def _resolve_adapter(event: Any, gateway: Any) -> Any | None:
    adapters = getattr(gateway, "adapters", None)
    if not isinstance(adapters, dict):
        return None
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    candidates = [platform, getattr(platform, "value", None), str(getattr(platform, "value", platform) or "")]
    for candidate in candidates:
        try:
            if candidate in adapters:
                return adapters[candidate]
        except TypeError:
            continue
    lowered = str(getattr(platform, "value", platform) or "").lower()
    return adapters.get(lowered)


async def _send_adapter_message(event: Any, gateway: Any, message: str) -> bool:
    """Best-effort visible Discord diagnostic/ack for the spike prototype."""
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

    # Adapter signatures vary across Hermes versions. Prefer the documented
    # metadata form, but fall back to a plain send for this spike artifact.
    try:
        result = send(chat_id, message, metadata=metadata)
    except TypeError:
        result = send(chat_id, message)
    if inspect.isawaitable(result):
        await result
    return True


def _is_authorized(event: Any, gateway: Any) -> bool | None:
    auth = getattr(gateway, "_is_user_authorized", None)
    if not callable(auth):
        return None
    return bool(auth(getattr(event, "source", None)))


async def pre_gateway_dispatch(event: Any, gateway: Any, session_store: Any = None) -> dict[str, str] | None:
    """Handle `/issue-runner-spike` as an equivalent Discord test trigger.

    Expected future gateway API:
        await gateway.start_child_session(request)

    Current behavior without that API:
        Send a visible Discord diagnostic when an adapter is available, then skip.
    """
    request = _parse_trigger(getattr(event, "text", ""))
    if request is None:
        return None

    if _platform_value(event) != "discord":
        return None

    authorized = _is_authorized(event, gateway)
    if authorized is False:
        # pre_gateway_dispatch runs before normal gateway authorization/pairing.
        # Do not consume unauthorized triggers; let Hermes core enforce its
        # standard auth/pairing policy and messaging.
        return None

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
        await _send_adapter_message(event, gateway, f"Started issue-runner spike child session{link}.")
        return {
            "action": "skip",
            "reason": "issue-runner-spike child session started",
        }

    sent = await _send_adapter_message(
        event,
        gateway,
        (
            "Issue-runner spike reached the plugin trigger, but this Hermes "
            "build does not expose `gateway.start_child_session(...)`. The "
            "Discord adapter has private thread/session helpers; issue #2's "
            "technical note documents the minimal core extension needed before "
            "a pure plugin can safely prove live child-session streaming."
        ),
    )
    if not sent:
        # Avoid consuming the trigger as an invisible drop if this Hermes build
        # does not expose an adapter send path to surface the diagnostic.
        return None
    return {
        "action": "skip",
        "reason": "missing gateway.start_child_session seam",
    }
