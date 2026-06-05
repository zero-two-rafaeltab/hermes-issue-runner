# Hermes Issue Runner

A Hermes gateway plugin for running GitHub sub-issues through auditable agent implementation workflows from Discord.

This repository currently contains the issue #2 technical spike artifact: a minimal gateway plugin prototype plus a technical note proving the Discord child-session seam as far as possible from local source inspection.

## Spike #2 result

See [`docs/spike-002-gateway-native-discord-child-sessions.md`](docs/spike-002-gateway-native-discord-child-sessions.md).

Short version:

- Hermes already has gateway-native Discord streaming for normal sessions via `GatewayStreamConsumer`.
- Hermes already has a Discord adapter-private `/thread` flow that can create a Discord thread and dispatch a Hermes session into it.
- A repository/user plugin can catch a test trigger through `pre_gateway_dispatch`, but it does **not** have a stable public gateway API for “create thread, then start a gateway-origin streamed child session in that thread”.
- The smallest core extension should expose that existing adapter/gateway behavior as a supported service method while keeping the user-facing artifact a plugin.

## Prototype contents

- `plugins/issue_runner_spike/` — minimal plugin prototype for a `/issue-runner-spike` text trigger.
- `src/issue_runner_spike/seam.py` — source-inspection/seam model and CLI smoke-check helper.
- `tests/test_seam.py` — lightweight semantic checks for the proposed seam.

## Local smoke checks

```bash
python -m src.issue_runner_spike.seam --hermes-source ~/.hermes/hermes-agent --json
python -m unittest discover -s tests
```
