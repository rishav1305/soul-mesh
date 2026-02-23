# tests/test_transport.py
"""Tests for mesh WebSocket transport."""

from __future__ import annotations

import asyncio
import json

import pytest

from soul_mesh.transport import (
    HEARTBEAT_INTERVAL,
    MAX_MESSAGE_SIZE,
    MAX_RECONNECT_DELAY,
    MeshTransport,
)


class MockNode:
    def __init__(self, node_id="local-1", account_id="acct-1"):
        self.id = node_id
        self.account_id = account_id
        self.port = 8340


class MockDB:
    """Mock MeshDB for testing transport without real SQLite."""

    def __init__(self):
        self._nodes: list[dict] = []

    async def fetch_all(self, sql, params=()):
        return [n for n in self._nodes if n["id"] != params[0]] if params else self._nodes

    async def fetch_one(self, sql, params=()):
        for n in self._nodes:
            if n.get("role") == "hub" and n.get("status") == "online":
                return n
        return None

    def add_node(self, node_id, host="127.0.0.1", port=8340, role="agent"):
        self._nodes.append({
            "id": node_id, "host": host, "port": port,
            "role": role, "status": "online",
        })


class MockWebSocket:
    """Mock WebSocket for testing send/receive."""

    def __init__(self):
        self.sent: list[str] = []
        self.closed = False

    async def send_text(self, data: str):
        self.sent.append(data)

    async def send(self, data: str):
        self.sent.append(data)

    async def close(self):
        self.closed = True


class TestConstants:
    def test_max_message_size(self):
        assert MAX_MESSAGE_SIZE == 1_048_576

    def test_heartbeat_interval(self):
        assert HEARTBEAT_INTERVAL == 30

    def test_max_reconnect_delay(self):
        assert MAX_RECONNECT_DELAY == 300


class TestMeshTransportInit:
    def test_constructor(self):
        node = MockNode()
        db = MockDB()
        transport = MeshTransport(node, db, "secret")
        assert transport._running is False
        assert transport._handlers == {}
        assert transport._connections == {}


class TestMessageRouting:
    def test_register_handler(self):
        node = MockNode()
        transport = MeshTransport(node, MockDB(), "secret")

        async def handler(payload, peer_id):
            pass

        transport.on("test_type", handler)
        assert "test_type" in transport._handlers

    async def test_handle_message_dispatches(self):
        node = MockNode()
        transport = MeshTransport(node, MockDB(), "secret")
        received = []

        async def handler(payload, peer_id):
            received.append((payload, peer_id))
            return {"ok": True}

        transport.on("test_msg", handler)
        result = await transport.handle_message(
            {"type": "test_msg", "payload": {"data": 1}}, "peer-1"
        )
        assert result == {"ok": True}
        assert len(received) == 1
        assert received[0] == ({"data": 1}, "peer-1")

    async def test_handle_unknown_type_returns_none(self):
        node = MockNode()
        transport = MeshTransport(node, MockDB(), "secret")
        result = await transport.handle_message(
            {"type": "unknown", "payload": {}}, "peer-1"
        )
        assert result is None

    async def test_handle_message_catches_handler_error(self):
        node = MockNode()
        transport = MeshTransport(node, MockDB(), "secret")

        async def bad_handler(payload, peer_id):
            raise RuntimeError("oops")

        transport.on("bad", bad_handler)
        result = await transport.handle_message(
            {"type": "bad", "payload": {}}, "peer-1"
        )
        assert result is None


class TestSend:
    async def test_send_to_connected_peer(self):
        node = MockNode()
        transport = MeshTransport(node, MockDB(), "secret")
        ws = MockWebSocket()
        transport._connections["peer-1"] = ws

        await transport.send("peer-1", "hello", {"text": "hi"})
        assert len(ws.sent) == 1
        msg = json.loads(ws.sent[0])
        assert msg["type"] == "hello"
        assert msg["payload"] == {"text": "hi"}
        assert msg["from"] == "local-1"

    async def test_send_to_missing_peer_raises(self):
        node = MockNode()
        transport = MeshTransport(node, MockDB(), "secret")
        with pytest.raises(ConnectionError):
            await transport.send("nobody", "hello", {})

    async def test_send_oversized_raises(self):
        node = MockNode()
        transport = MeshTransport(node, MockDB(), "secret")
        ws = MockWebSocket()
        transport._connections["peer-1"] = ws

        big_payload = "x" * (MAX_MESSAGE_SIZE + 1)
        with pytest.raises(ValueError, match="too large"):
            await transport.send("peer-1", "big", big_payload)

    async def test_broadcast_sends_to_all(self):
        node = MockNode()
        transport = MeshTransport(node, MockDB(), "secret")
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()
        transport._connections["p1"] = ws1
        transport._connections["p2"] = ws2

        await transport.broadcast("ping", {"ts": 123})
        assert len(ws1.sent) == 1
        assert len(ws2.sent) == 1

    async def test_broadcast_skips_failed_peer(self):
        node = MockNode()
        transport = MeshTransport(node, MockDB(), "secret")

        class FailWS(MockWebSocket):
            async def send_text(self, data):
                raise ConnectionError("gone")

        transport._connections["good"] = MockWebSocket()
        transport._connections["bad"] = FailWS()

        await transport.broadcast("ping", {})
        # Should not raise -- bad peer is skipped
        good_ws = transport._connections["good"]
        assert len(good_ws.sent) == 1

    async def test_send_to_hub(self):
        node = MockNode()
        db = MockDB()
        db.add_node("hub-1", role="hub")
        transport = MeshTransport(node, db, "secret")
        ws = MockWebSocket()
        transport._connections["hub-1"] = ws

        await transport.send_to_hub("sync", {"data": 1})
        assert len(ws.sent) == 1

    async def test_send_to_hub_no_hub_raises(self):
        node = MockNode()
        transport = MeshTransport(node, MockDB(), "secret")
        with pytest.raises(ConnectionError, match="No hub"):
            await transport.send_to_hub("sync", {})


class TestConnectionManagement:
    async def test_register_inbound(self):
        node = MockNode()
        transport = MeshTransport(node, MockDB(), "secret")
        ws = MockWebSocket()
        await transport.register_inbound("peer-1", ws)
        assert "peer-1" in transport._connections

    async def test_unregister(self):
        node = MockNode()
        transport = MeshTransport(node, MockDB(), "secret")
        ws = MockWebSocket()
        transport._connections["peer-1"] = ws
        await transport.unregister("peer-1")
        assert "peer-1" not in transport._connections

    async def test_stop_closes_all(self):
        node = MockNode()
        transport = MeshTransport(node, MockDB(), "secret")
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()
        transport._connections["p1"] = ws1
        transport._connections["p2"] = ws2
        transport._running = True

        await transport.stop()
        assert transport._running is False
        assert ws1.closed
        assert ws2.closed
        assert transport._connections == {}


class TestSendUsesCorrectWSMethod:
    async def test_uses_send_text_when_available(self):
        node = MockNode()
        transport = MeshTransport(node, MockDB(), "secret")

        class FastAPIWS:
            def __init__(self):
                self.sent_text = []
            async def send_text(self, data):
                self.sent_text.append(data)

        ws = FastAPIWS()
        transport._connections["p1"] = ws
        await transport.send("p1", "test", {})
        assert len(ws.sent_text) == 1

    async def test_uses_send_when_no_send_text(self):
        node = MockNode()
        transport = MeshTransport(node, MockDB(), "secret")

        class PlainWS:
            def __init__(self):
                self.sent = []
            async def send(self, data):
                self.sent.append(data)

        ws = PlainWS()
        transport._connections["p1"] = ws
        await transport.send("p1", "test", {})
        assert len(ws.sent) == 1
