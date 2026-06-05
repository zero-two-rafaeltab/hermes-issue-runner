from __future__ import annotations

import asyncio
import os
from pathlib import Path
import unittest
from types import SimpleNamespace

from plugins.issue_runner_spike import TRIGGER, pre_gateway_dispatch
from src.issue_runner_spike.seam import inspect_hermes_source


class SeamInspectionTests(unittest.TestCase):
    def test_local_hermes_source_has_private_thread_flow(self) -> None:
        hermes_source = Path(os.environ.get("HERMES_SOURCE", "~/.hermes/hermes-agent")).expanduser()
        if not hermes_source.exists():
            self.skipTest(f"Hermes source checkout not found: {hermes_source}")
        probe = inspect_hermes_source(hermes_source)
        self.assertTrue(probe.discord_adapter_found)
        self.assertTrue(probe.base_platform_adapter_found)
        self.assertTrue(probe.gateway_runner_found)
        self.assertTrue(probe.stream_consumer_found)
        self.assertTrue(probe.private_discord_thread_flow_found)
        self.assertTrue(probe.base_create_handoff_thread_found)
        self.assertTrue(probe.discord_create_handoff_thread_found)
        self.assertTrue(probe.gateway_process_handoff_found)
        # This spike documents the current blocker. If this assertion starts
        # failing, Hermes likely gained the public seam and the technical note
        # should be updated to exercise it.
        self.assertFalse(probe.public_start_child_session_found)


class PrototypePluginTests(unittest.TestCase):
    def _event(self, text: str, platform: str = "discord") -> SimpleNamespace:
        return SimpleNamespace(
            text=text,
            source=SimpleNamespace(platform=SimpleNamespace(value=platform), chat_id="c1", thread_id=None),
        )

    def test_ignores_non_trigger(self) -> None:
        result = asyncio.run(pre_gateway_dispatch(self._event("hello"), SimpleNamespace()))
        self.assertIsNone(result)

    def test_reports_missing_public_seam(self) -> None:
        sent = []

        async def send(chat_id, content, metadata=None):
            sent.append((chat_id, content, metadata))

        gateway = SimpleNamespace(adapters={"discord": SimpleNamespace(send=send)})
        result = asyncio.run(pre_gateway_dispatch(self._event(f"{TRIGGER} do work"), gateway))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "skip")
        self.assertNotIn("message", result)
        self.assertEqual(len(sent), 1)
        self.assertIn("start_child_session", sent[0][1])

    def test_missing_seam_without_adapter_is_not_invisible_drop(self) -> None:
        result = asyncio.run(pre_gateway_dispatch(self._event(f"{TRIGGER} do work"), SimpleNamespace()))
        self.assertIsNone(result)

    def test_unauthorized_trigger_is_not_consumed(self) -> None:
        gateway = SimpleNamespace(_is_user_authorized=lambda source: False)
        result = asyncio.run(pre_gateway_dispatch(self._event(f"{TRIGGER} do work"), gateway))
        self.assertIsNone(result)

    def test_uses_future_public_seam_when_available(self) -> None:
        calls = []

        async def start_child_session(payload):
            calls.append(payload)
            return {"thread_id": "123"}

        gateway = SimpleNamespace(start_child_session=start_child_session)
        result = asyncio.run(pre_gateway_dispatch(self._event(f"{TRIGGER} do work"), gateway))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["platform"], "discord")
        self.assertEqual(calls[0]["prompt"], "do work")
        self.assertEqual(calls[0]["idempotency_key"], "spike:64d6f071c16a0984")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertNotIn("message", result)


if __name__ == "__main__":
    unittest.main()
