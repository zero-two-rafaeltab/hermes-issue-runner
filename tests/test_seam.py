from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from plugins.issue_runner_spike import TRIGGER, pre_gateway_dispatch
from src.issue_runner_spike.seam import inspect_hermes_source


class SeamInspectionTests(unittest.TestCase):
    def test_local_hermes_source_has_private_thread_flow(self) -> None:
        probe = inspect_hermes_source("/home/rafaeltab/.hermes/hermes-agent")
        self.assertTrue(probe.discord_adapter_found)
        self.assertTrue(probe.gateway_runner_found)
        self.assertTrue(probe.stream_consumer_found)
        self.assertTrue(probe.private_discord_thread_flow_found)
        # This spike documents the current blocker. If this assertion starts
        # failing, Hermes likely gained the public seam and the technical note
        # should be updated to exercise it.
        self.assertFalse(probe.public_start_child_session_found)


class PrototypePluginTests(unittest.TestCase):
    def _event(self, text: str, platform: str = "discord") -> SimpleNamespace:
        return SimpleNamespace(
            text=text,
            source=SimpleNamespace(platform=SimpleNamespace(value=platform)),
        )

    def test_ignores_non_trigger(self) -> None:
        result = asyncio.run(pre_gateway_dispatch(self._event("hello"), SimpleNamespace()))
        self.assertIsNone(result)

    def test_reports_missing_public_seam(self) -> None:
        result = asyncio.run(pre_gateway_dispatch(self._event(f"{TRIGGER} do work"), SimpleNamespace()))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "skip")
        self.assertIn("start_child_session", result["message"])

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
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("<#{0}>".format("123"), result["message"])


if __name__ == "__main__":
    unittest.main()
