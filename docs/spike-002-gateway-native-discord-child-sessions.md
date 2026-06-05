# Spike 002 — gateway-native Discord child sessions

Issue: [#2](https://github.com/zero-two-rafaeltab/hermes-issue-runner/issues/2)

## Goal

Prove the smallest vertical slice for an authorized Discord trigger that:

1. creates a Discord thread in the invoking channel,
2. starts a Hermes gateway-origin child agent session routed to that thread, and
3. preserves the normal Discord live progress/tool-call streaming UX.

This spike does **not** claim live Discord proof. It is a source-backed seam investigation plus a minimal plugin prototype/harness that Rafael can use once the stable core seam exists.

## Source inspection summary

Hermes source inspected locally at `~/.hermes/hermes-agent`.

Relevant current code paths:

- `gateway/hooks.py`
  - User hooks can subscribe to gateway lifecycle events.
  - `command:<name>` hooks only fire for recognized Hermes slash commands.
- `hermes_cli/plugins.py`
  - A plugin can register `pre_gateway_dispatch`.
  - This hook receives `event`, `gateway`, and `session_store` before authorization/pairing and can return `{"action": "skip"}` after handling a message.
- `plugins/platforms/discord/adapter.py`
  - Discord is implemented as a bundled platform plugin.
  - `_handle_thread_create_slash()` already creates a thread and dispatches a Hermes session when a starter message is supplied.
  - `_create_thread()` encapsulates Discord thread creation for a native interaction.
  - `_dispatch_thread_session()` constructs a thread-targeted `MessageEvent` and calls `self.handle_message(event)`.
  - `create_handoff_thread()` implements the existing handoff thread helper for Discord.
  - `send(chat_id, content, metadata={"thread_id": ...})` routes responses to a Discord thread.
- `gateway/platforms/base.py`
  - `BasePlatformAdapter.create_handoff_thread()` defines the adapter hook for handoff destinations.
- `gateway/run.py`
  - `_handle_message()` owns authorization, session lookup/creation, and dispatch.
  - `_process_handoff()` consumes existing handoff rows and asks adapters to create handoff threads.
  - `_run_agent()` creates the normal streamed gateway turn.
  - Streaming uses `GatewayStreamConsumer` and platform metadata to progressively send/edit Discord messages.
- `gateway/stream_consumer.py`
  - `GatewayStreamConsumer` bridges synchronous agent streaming callbacks into asynchronous platform delivery.
  - It supports streamed assistant deltas, segment breaks, commentary, and tool/progress placement.

## What is already proven from code

### Thread routing exists

The Discord adapter can send into a thread when metadata contains `thread_id`. Gateway session source also models `chat_type="thread"`, `thread_id`, and `parent_chat_id`.

### A gateway-origin Discord thread session exists, but only behind private adapter methods

The bundled Discord `/thread` flow is the strongest evidence:

```text
_handle_thread_create_slash(interaction, name, message)
  -> _create_thread(...)
  -> _dispatch_thread_session(interaction, thread_id, thread_name, starter)
  -> self.handle_message(MessageEvent(... chat_type="thread" ...))
```

That means Hermes core/adapter code already knows how to create a thread and launch a normal gateway-handled session in it.

### Existing handoff machinery exists, but is not the needed plugin seam

Source inspection also finds `BasePlatformAdapter.create_handoff_thread()`, Discord
`create_handoff_thread()`, and `GatewayRunner._process_handoff()`. This is
important existing machinery, but it is designed for queued CLI/home-channel
handoff workflows. It is not currently a stable plugin API for: take this
invoking Discord event, create a child destination under the invoking channel,
authorize the invoking user through the normal gateway policy, start a streamed
child session there, and return identifiers/ack data to the plugin. The proposed
issue-runner seam should reuse this machinery where appropriate rather than
ignore it, but the machinery alone does not satisfy issue #2's plugin-facing API
requirement.

### Normal live streaming should be preserved when dispatch goes through gateway `handle_message`

If the child session is started by constructing a `MessageEvent` and entering the same gateway handling path as an inbound Discord message, it reaches the same `GatewayStreamConsumer` used by ordinary Discord turns. That is the required route for live progress/tool-call output. A standalone script or direct `AIAgent.chat()` call would **not** prove the required UX.

## Current blocker

A pure plugin is **not sufficient as a stable product seam** today.

A plugin can catch a text trigger through `pre_gateway_dispatch`, but there is no public plugin API that says:

> create a platform-native child destination under this invoking Discord channel, then start a gateway-origin Hermes session there using normal streaming and authorization/session lifecycle.

A plugin can reach private internals (`event.raw_message.create_thread`, `gateway._handle_message`, Discord adapter private methods), but relying on those internals would be brittle and would bypass/duplicate core authorization/session semantics unless very carefully mirrored.

The already-existing adapter-private `/thread` path and handoff helpers demonstrate the product is feasible, but they are not yet exposed as a stable invoking-channel plugin/child-session surface.

## Minimal Hermes core extension required

Expose a gateway service method with this shape (names illustrative; the prototype may need adaptation once the final core seam lands):

```python
@dataclass(frozen=True)
class GatewayChildSessionRequest:
    parent_event: MessageEvent
    title: str
    prompt: str
    platform: str = "discord"
    thread_kind: str = "discord_public_thread"
    idempotency_key: str | None = None
    auto_archive_duration: int = 1440
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class GatewayChildSessionResult:
    platform: str
    parent_chat_id: str
    child_chat_id: str
    thread_id: str
    thread_name: str
    session_key: str | None
    session_id: str | None
    starter_message_id: str | None
    streaming_started: bool

class GatewayRunner:
    async def start_child_session(
        self,
        request: GatewayChildSessionRequest,
    ) -> GatewayChildSessionResult: ...
```

Required behavior:

1. **Authorization boundary**
   - The call must require an already-authorized `parent_event` or explicitly run the same gateway/Discord authorization gates used for normal messages and native slash commands.
   - Plugins must not be allowed to mint child sessions for arbitrary Discord users/channels without that check.
2. **Thread creation**
   - For Discord, create a thread under `parent_event.source.chat_id` or under the parent of `parent_event.source.thread_id` when invoked from an existing thread.
   - Reuse the existing adapter logic currently inside `_create_thread()` where possible.
3. **Session construction**
   - Build a `MessageEvent` whose `SessionSource` points at the new thread:
     - `platform=Platform.DISCORD`
     - `chat_id=<thread_id>`
     - `chat_type="thread"`
     - `thread_id=<thread_id>`
     - `parent_chat_id=<invoking channel id>`
     - `guild_id`, `user_id`, `user_name`, channel prompt/skills inherited from the parent as appropriate.
4. **Lifecycle/routing**
   - Enter the normal `handle_message` path, not a direct `AIAgent` call.
   - Return after the child turn is scheduled/started, with enough identifiers for audit logs and GitHub issue comments.
   - Preserve existing session locking, `/stop`, `/status`, stream consumer, tool-progress display, and transcript persistence behavior.
5. **Idempotency**
   - Accept an idempotency key so the future issue runner can avoid duplicate threads for the same GitHub sub-issue attempt after Discord retries or gateway restarts.

### How the issue-runner plugin would call it

```python
async def pre_gateway_dispatch(event, gateway, session_store):
    if not is_issue_runner_trigger(event):
        return None

    if not gateway.is_authorized_for_child_session(event):  # illustrative helper
        # pre_gateway_dispatch runs before normal authorization; do not consume
        # unauthorized messages or bypass pairing/allowlist behavior.
        return None

    result = await gateway.start_child_session(
        GatewayChildSessionRequest(
            parent_event=event,
            title="issue-42 attempt 1",
            prompt="Implement GitHub issue #42. Stream progress here.",
            idempotency_key="github:owner/repo/issues/42:attempt:1",
        )
    )

    await post_parent_ack(event, result)
    return {"action": "skip", "reason": "issue-runner child session started"}
```

## Minimal prototype in this repo

`plugins/issue_runner_spike/__init__.py` registers `pre_gateway_dispatch` and recognizes a text trigger:

```text
/issue-runner-spike <prompt>
```

It intentionally behaves conservatively:

- If a future `gateway.start_child_session()` seam exists, it calls that.
- It checks `gateway._is_user_authorized(event.source)` when that private helper is available before calling the future seam, so unauthorized triggers are not consumed by the pre-dispatch hook. The future public seam must still enforce authorization itself.
- Otherwise it sends a visible Discord diagnostic through the platform adapter when available, explaining that the stable core seam is missing; if no adapter send path is available, it returns `None` rather than silently dropping the trigger.
- For non-Discord triggers, it returns `None` and avoids invisible diagnostics.
- It does not claim success or fabricate Discord evidence.

This provides a durable plugin-shaped artifact without depending on private Hermes internals for production behavior.

## Acceptance criteria mapping

| Acceptance criterion | Spike outcome |
| --- | --- |
| Authorized Discord slash/equivalent test trigger can start spike flow | Prototype uses `pre_gateway_dispatch` text trigger; native slash registration still needs core/plugin command registration if desired. |
| Flow creates Discord thread or demonstrates exact missing API seam | Existing private Discord adapter thread creation found; public plugin seam missing. |
| Flow starts, or identifies minimal extension needed to start, gateway-origin child session | Existing private `_dispatch_thread_session()` found; proposed public `GatewayRunner.start_child_session()` extension specified. |
| Child streams live progress/tool-call output through Discord | Source route identified: child must enter gateway `handle_message` and `GatewayStreamConsumer`. Not live-proven. |
| Short technical note on pure plugin vs core extension | This document: pure plugin is not sufficient as stable product seam; minimal core service required. |
| Rafael can perform final manual Discord UX check | Checklist below. |

## Manual HITL checklist for Rafael

Run this only after either the proposed `gateway.start_child_session()` seam exists or the prototype is temporarily wired to the private Discord adapter path for a throwaway test server.

1. Start Hermes gateway with Discord enabled and normal streaming/tool progress enabled.
2. In an authorized Discord channel, invoke the issue-runner spike trigger with a prompt that forces at least one tool call, for example:
   - `/issue-runner-spike inspect the current repository, run a harmless command, and summarize`
3. Confirm a new Discord thread appears under the invoking channel.
4. Confirm the child session posts/edits live output in that thread before the final answer appears.
5. Confirm tool-call/progress bubbles are visible in the thread, not only a final summary.
6. Confirm `/status` in the child thread reports the child run while it is active.
7. Confirm `/stop` in the child thread stops only that child session and does not break the parent channel.
8. Confirm the parent channel receives a short acknowledgement with the child thread mention/link.
9. Capture the thread URL and note whether the UX matches normal Hermes Discord live streaming.

## Non-goals for this spike

- No GitHub issue mutation.
- No durable attempt scheduler.
- No AFK queue orchestration.
- No claim of live Discord proof without Rafael’s manual check.
