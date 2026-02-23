"""Tests for the CommandRelay -- routes commands from REST to agents via WS."""

from __future__ import annotations

import asyncio

import pytest

from soul_mesh.executor import CommandRelay


class FakeWS:
    """Minimal stand-in for a FastAPI WebSocket (only ``send_json``)."""

    def __init__(self, relay: CommandRelay | None = None):
        self.sent: list[dict] = []
        self._relay = relay

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)
        # Simulate the agent immediately returning a result when the relay
        # is provided (used by the send_command test).
        if self._relay is not None:
            cmd_id = data.get("cmd_id")
            if cmd_id:
                self._relay.deliver_result(cmd_id, {
                    "cmd_id": cmd_id,
                    "stdout": "hello\n",
                    "stderr": "",
                    "exit_code": 0,
                })


class TestConnectionManagement:
    """register / unregister / get."""

    async def test_stores_and_retrieves_connection(self):
        relay = CommandRelay()
        ws = FakeWS()
        relay.register("node-1", ws)
        assert relay.get("node-1") is ws

    async def test_unregister_removes_connection(self):
        relay = CommandRelay()
        ws = FakeWS()
        relay.register("node-1", ws)
        relay.unregister("node-1")
        assert relay.get("node-1") is None

    async def test_get_returns_none_for_unknown(self):
        relay = CommandRelay()
        assert relay.get("missing") is None

    async def test_unregister_idempotent(self):
        relay = CommandRelay()
        relay.unregister("never-registered")  # should not raise


class TestSendCommand:
    """send_command -- sends to agent and awaits result via future."""

    async def test_send_command_to_connected_node(self):
        relay = CommandRelay()
        ws = FakeWS(relay=relay)
        relay.register("node-1", ws)

        result = await asyncio.wait_for(
            relay.send_command("node-1", "echo hello"),
            timeout=2.0,
        )
        assert result["stdout"] == "hello\n"
        assert result["exit_code"] == 0

    async def test_send_command_sends_correct_message(self):
        relay = CommandRelay()
        ws = FakeWS(relay=relay)
        relay.register("node-1", ws)

        await relay.send_command("node-1", "uptime")

        assert len(ws.sent) == 1
        msg = ws.sent[0]
        assert msg["type"] == "run_command"
        assert msg["command"] == "uptime"
        assert "cmd_id" in msg

    async def test_command_to_unknown_node_raises(self):
        relay = CommandRelay()
        with pytest.raises(ValueError, match="not connected"):
            await relay.send_command("ghost-node", "ls")

    async def test_command_timeout(self):
        relay = CommandRelay()
        ws = FakeWS()  # No relay -- won't deliver a result
        relay.register("node-1", ws)

        with pytest.raises(asyncio.TimeoutError):
            await relay.send_command("node-1", "sleep 999", timeout=0.05)

    async def test_pending_cleaned_up_after_success(self):
        relay = CommandRelay()
        ws = FakeWS(relay=relay)
        relay.register("node-1", ws)

        await relay.send_command("node-1", "echo ok")
        assert len(relay._pending) == 0

    async def test_pending_cleaned_up_after_timeout(self):
        relay = CommandRelay()
        ws = FakeWS()
        relay.register("node-1", ws)

        with pytest.raises(asyncio.TimeoutError):
            await relay.send_command("node-1", "hang", timeout=0.05)
        assert len(relay._pending) == 0


class TestDeliverResult:
    """deliver_result -- resolves pending futures."""

    async def test_deliver_resolves_future(self):
        relay = CommandRelay()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        relay._pending["cmd-123"] = future

        relay.deliver_result("cmd-123", {"stdout": "ok"})

        assert future.done()
        assert future.result() == {"stdout": "ok"}

    async def test_deliver_unknown_cmd_id_is_noop(self):
        relay = CommandRelay()
        # Should not raise
        relay.deliver_result("unknown-cmd", {"stdout": ""})

    async def test_deliver_already_done_future_is_noop(self):
        relay = CommandRelay()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        future.set_result({"stdout": "first"})
        relay._pending["cmd-456"] = future

        # Delivering again should not raise
        relay.deliver_result("cmd-456", {"stdout": "second"})
        assert future.result() == {"stdout": "first"}
