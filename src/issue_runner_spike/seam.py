"""Seam model and source-inspection helper for issue #2.

This module is intentionally stdlib-only. It does not import Hermes; it checks
for the source symbols that make the spike feasible and reports whether the
stable public plugin seam exists.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SeamProbe:
    hermes_source: str
    discord_adapter_found: bool
    gateway_runner_found: bool
    stream_consumer_found: bool
    private_discord_thread_flow_found: bool
    public_start_child_session_found: bool
    conclusion: str


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def inspect_hermes_source(root: str | Path) -> SeamProbe:
    """Inspect a Hermes source checkout for the Discord child-session seam."""
    base = Path(root).expanduser().resolve()
    discord_adapter = base / "plugins" / "platforms" / "discord" / "adapter.py"
    gateway_run = base / "gateway" / "run.py"
    stream_consumer = base / "gateway" / "stream_consumer.py"

    adapter_text = _read(discord_adapter)
    gateway_text = _read(gateway_run)
    stream_text = _read(stream_consumer)

    private_flow = all(
        needle in adapter_text
        for needle in (
            "async def _handle_thread_create_slash",
            "async def _create_thread",
            "async def _dispatch_thread_session",
            "await self.handle_message(event)",
        )
    )
    public_seam = "start_child_session" in gateway_text or "start_child_session" in adapter_text
    streaming = "class GatewayStreamConsumer" in stream_text and "stream_delta_callback" in stream_text

    if public_seam:
        conclusion = "public child-session seam appears to exist"
    elif private_flow and streaming:
        conclusion = "private Discord thread/session flow exists; public plugin seam is missing"
    else:
        conclusion = "required private flow was not found in inspected source"

    return SeamProbe(
        hermes_source=str(base),
        discord_adapter_found=discord_adapter.is_file(),
        gateway_runner_found=gateway_run.is_file(),
        stream_consumer_found=stream_consumer.is_file(),
        private_discord_thread_flow_found=private_flow,
        public_start_child_session_found=public_seam,
        conclusion=conclusion,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hermes-source",
        default="~/.hermes/hermes-agent",
        help="Path to a Hermes Agent source checkout",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args(argv)

    result = inspect_hermes_source(args.hermes_source)
    data: dict[str, Any] = asdict(result)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        for key, value in data.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
