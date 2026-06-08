# Hermes Issue Runner

A Hermes gateway plugin for running GitHub sub-issues through auditable agent implementation workflows from Discord.

This repository currently contains the issue #2 technical spike artifact plus the first minimal issue-runner start command surface for Discord.

## Spike #2 result

See [`docs/spike-002-gateway-native-discord-child-sessions.md`](docs/spike-002-gateway-native-discord-child-sessions.md).

Short version:

- Hermes already has gateway-native Discord streaming for normal sessions via `GatewayStreamConsumer`.
- Hermes already has a Discord adapter-private `/thread` flow that can create a Discord thread and dispatch a Hermes session into it.
- A repository/user plugin can catch a test trigger through `pre_gateway_dispatch`, but it does **not** have a stable public gateway API for “create thread, then start a gateway-origin streamed child session in that thread”.
- The smallest core extension should expose that existing adapter/gateway behavior as a supported service method while keeping the user-facing artifact a plugin.

## Prototype contents

- `plugins/issue_runner/` — minimal Discord start command plugin entrypoint.
- `src/issue_runner/start.py` — parser/handler for slash and mention start commands with injected auth/GitHub seams.
- `plugins/issue_runner_spike/` — minimal plugin prototype for a `/issue-runner-spike` text trigger.
- `src/issue_runner_spike/seam.py` — source-inspection/seam model and CLI smoke-check helper.
- `tests/test_start_command.py` — unit tests for issue #3 start command behavior.
- `tests/test_seam.py` — lightweight semantic checks for the proposed seam.

## Minimal start command

The issue-runner plugin recognizes slash-style commands such as:

```text
/issue-runner start owner/repo#1
/issue-runner start https://github.com/owner/repo/issues/1
```

It also recognizes mention-oriented natural language, for example:

```text
@Hermes start issue runner for owner/repo#1
```

The handler reuses Hermes Discord authorization through the injected gateway
authorization predicate (for example `_is_user_authorized`) and requires an
injected GitHub client with `get_issue(owner, repo, number)` for live use. The
plugin imports the handler as `issue_runner.start`; for direct local imports from
a checkout, put `src/` on `PYTHONPATH` (the plugin does this for its repository
layout during registration/tests).

## Local smoke checks

```bash
python3 -m src.issue_runner_spike.seam --hermes-source "${HERMES_SOURCE:-$HOME/.hermes/hermes-agent}" --json
python3 -m unittest discover -s tests
```

`tests/test_seam.py` uses `HERMES_SOURCE` when set, defaults to
`~/.hermes/hermes-agent`, and skips the local source-inspection assertion when
that checkout is absent.

## Manual prototype enablement for Rafael

The plugin API shape in this spike is illustrative: the prototype may need small
adaptations once Hermes core lands a supported child-session seam.

For a throwaway local check, copy or symlink `plugins/issue_runner_spike/` into
the active Hermes profile plugin directory, enable it in the profile config, and
restart the gateway with Discord configured. Then run this from an authorized
Discord channel:

```text
/issue-runner-spike inspect the current repository, run a harmless command, and summarize
```

Until `gateway.start_child_session(...)` exists, the prototype only consumes the
trigger when it can send a visible Discord diagnostic/ack through the Discord
adapter; it does not claim live child-session proof or emit invisible diagnostics
for other platforms.
