# soul-mesh Transport Extraction Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extract transport.py from soul-os with standalone db.py and auth.py replacements for brain.db.store and brain.auth.jwt.

**Architecture:** Three new modules (db, auth, transport) added to soul_mesh/. MeshDB is a thin aiosqlite wrapper. auth.py uses PyJWT directly. transport.py is adapted from soul-os with dependency injection replacing global singletons.

**Tech Stack:** Python 3.11+, aiosqlite, PyJWT, websockets, structlog, pytest-asyncio

---

### Task 1: Add dependencies to pyproject.toml

**Files:**
- Modify: `pyproject.toml`

**Step 1: Add websockets and PyJWT to dependencies**

```toml
dependencies = [
    "aiosqlite>=0.20.0",
    "pydantic-settings>=2.7.0",
    "structlog>=24.0.0",
    "websockets>=12.0",
    "PyJWT>=2.8.0",
]
```

**Step 2: Install updated deps**

Run: `cd ~/soul/soul-mesh && pip install -e ".[dev]"`
Expected: Successfully installed with websockets and PyJWT

**Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "feat: add websockets and PyJWT dependencies for transport module"
```

---

### Task 2: Create soul_mesh/db.py with tests (TDD)

**Files:**
- Create: `soul_mesh/db.py`
- Create: `tests/test_db.py`

**Step 1: Write failing tests**

```python
# tests/test_db.py
"""Tests for the MeshDB async SQLite wrapper."""

from __future__ import annotations

import pytest

from soul_mesh.db import MeshDB


class TestMeshDB:
    @pytest.fixture
    async def db(self, tmp_path):
        """Create a MeshDB with a temp database."""
        db_path = str(tmp_path / "test.db")
        db = MeshDB(db_path)
        await db.ensure_tables()
        return db

    async def test_ensure_tables_creates_mesh_nodes(self, db):
        rows = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mesh_nodes'"
        )
        assert len(rows) == 1
        assert rows[0]["name"] == "mesh_nodes"

    async def test_execute_and_fetch_one(self, db):
        await db.execute(
            "INSERT INTO mesh_nodes (id, name, status) VALUES (?, ?, ?)",
            ("node-1", "test-node", "online"),
        )
        row = await db.fetch_one(
            "SELECT id, name, status FROM mesh_nodes WHERE id = ?", ("node-1",)
        )
        assert row is not None
        assert row["id"] == "node-1"
        assert row["name"] == "test-node"

    async def test_fetch_one_returns_none_when_missing(self, db):
        row = await db.fetch_one(
            "SELECT id FROM mesh_nodes WHERE id = ?", ("nonexistent",)
        )
        assert row is None

    async def test_fetch_all_returns_list_of_dicts(self, db):
        await db.execute(
            "INSERT INTO mesh_nodes (id, name, status) VALUES (?, ?, ?)",
            ("a", "alpha", "online"),
        )
        await db.execute(
            "INSERT INTO mesh_nodes (id, name, status) VALUES (?, ?, ?)",
            ("b", "bravo", "offline"),
        )
        rows = await db.fetch_all("SELECT id, name FROM mesh_nodes ORDER BY id")
        assert len(rows) == 2
        assert rows[0]["id"] == "a"
        assert rows[1]["id"] == "b"

    async def test_fetch_all_empty_returns_empty_list(self, db):
        rows = await db.fetch_all("SELECT id FROM mesh_nodes")
        assert rows == []

    async def test_execute_with_no_params(self, db):
        await db.execute(
            "INSERT INTO mesh_nodes (id, name, status) VALUES ('x', 'x', 'online')"
        )
        row = await db.fetch_one("SELECT id FROM mesh_nodes WHERE id = 'x'")
        assert row is not None

    async def test_in_memory_db(self):
        db = MeshDB(":memory:")
        await db.ensure_tables()
        await db.execute(
            "INSERT INTO mesh_nodes (id, name, status) VALUES (?, ?, ?)",
            ("m1", "mem", "online"),
        )
        row = await db.fetch_one("SELECT id FROM mesh_nodes WHERE id = ?", ("m1",))
        assert row is not None
```

**Step 2: Run tests to verify they fail**

Run: `cd ~/soul/soul-mesh && python -m pytest tests/test_db.py -v`
Expected: FAIL -- `ModuleNotFoundError: No module named 'soul_mesh.db'`

**Step 3: Implement MeshDB**

```python
# soul_mesh/db.py
"""Thin async SQLite wrapper for mesh operations.

Replaces brain.db.store with a standalone module.
Uses aiosqlite with connect-per-call (matches node.py pattern).
"""

from __future__ import annotations

import aiosqlite


class MeshDB:
    """Async SQLite wrapper for mesh node storage."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._memory_db: aiosqlite.Connection | None = None

    async def _connect(self) -> aiosqlite.Connection:
        if self._db_path == ":memory:":
            if self._memory_db is None:
                self._memory_db = await aiosqlite.connect(":memory:")
                self._memory_db.row_factory = aiosqlite.Row
            return self._memory_db
        conn = await aiosqlite.connect(self._db_path)
        conn.row_factory = aiosqlite.Row
        return conn

    async def _close(self, conn: aiosqlite.Connection) -> None:
        if self._db_path != ":memory:":
            await conn.close()

    async def fetch_all(
        self, sql: str, params: tuple = ()
    ) -> list[dict]:
        conn = await self._connect()
        try:
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            await self._close(conn)

    async def fetch_one(
        self, sql: str, params: tuple = ()
    ) -> dict | None:
        conn = await self._connect()
        try:
            cursor = await conn.execute(sql, params)
            row = await cursor.fetchone()
            return dict(row) if row else None
        finally:
            await self._close(conn)

    async def execute(self, sql: str, params: tuple = ()) -> None:
        conn = await self._connect()
        try:
            await conn.execute(sql, params)
            await conn.commit()
        finally:
            await self._close(conn)

    async def ensure_tables(self) -> None:
        """Create mesh tables if they don't exist."""
        conn = await self._connect()
        try:
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS mesh_nodes (
                    id TEXT PRIMARY KEY,
                    account_id TEXT DEFAULT '',
                    name TEXT DEFAULT '',
                    host TEXT DEFAULT '',
                    port INTEGER DEFAULT 8340,
                    platform TEXT DEFAULT '',
                    arch TEXT DEFAULT '',
                    ram_mb INTEGER DEFAULT 0,
                    storage_mb INTEGER DEFAULT 0,
                    is_hub INTEGER DEFAULT 0,
                    capability REAL DEFAULT 0.0,
                    last_seen TEXT DEFAULT '',
                    status TEXT DEFAULT 'online'
                )"""
            )
            await conn.commit()
        finally:
            await self._close(conn)
```

**Step 4: Run tests to verify they pass**

Run: `cd ~/soul/soul-mesh && python -m pytest tests/test_db.py -v`
Expected: All 7 tests PASS

**Step 5: Commit**

```bash
git add soul_mesh/db.py tests/test_db.py
git commit -m "feat: add MeshDB async SQLite wrapper replacing brain.db.store"
```

---

### Task 3: Create soul_mesh/auth.py with tests (TDD)

**Files:**
- Create: `soul_mesh/auth.py`
- Create: `tests/test_auth.py`

**Step 1: Write failing tests**

```python
# tests/test_auth.py
"""Tests for standalone mesh JWT authentication."""

from __future__ import annotations

import time

import jwt
import pytest

from soul_mesh.auth import create_mesh_token, verify_mesh_token


TEST_SECRET = "test-secret-key-for-mesh"


class TestCreateMeshToken:
    def test_creates_valid_jwt(self):
        token = create_mesh_token("node-1", "account-1", TEST_SECRET)
        assert isinstance(token, str)
        payload = jwt.decode(token, TEST_SECRET, algorithms=["HS256"])
        assert payload["node_id"] == "node-1"
        assert payload["account_id"] == "account-1"

    def test_includes_expiry(self):
        token = create_mesh_token("node-1", "account-1", TEST_SECRET, ttl=60)
        payload = jwt.decode(token, TEST_SECRET, algorithms=["HS256"])
        assert "exp" in payload
        assert payload["exp"] > time.time()

    def test_custom_ttl(self):
        token = create_mesh_token("n", "a", TEST_SECRET, ttl=300)
        payload = jwt.decode(token, TEST_SECRET, algorithms=["HS256"])
        # exp should be within 300s of now
        assert payload["exp"] <= time.time() + 301

    def test_includes_issued_at(self):
        token = create_mesh_token("n", "a", TEST_SECRET)
        payload = jwt.decode(token, TEST_SECRET, algorithms=["HS256"])
        assert "iat" in payload

    def test_type_claim_is_mesh(self):
        token = create_mesh_token("n", "a", TEST_SECRET)
        payload = jwt.decode(token, TEST_SECRET, algorithms=["HS256"])
        assert payload["type"] == "mesh"


class TestVerifyMeshToken:
    def test_verify_valid_token(self):
        token = create_mesh_token("node-1", "account-1", TEST_SECRET)
        payload = verify_mesh_token(token, TEST_SECRET)
        assert payload["node_id"] == "node-1"
        assert payload["account_id"] == "account-1"

    def test_verify_wrong_secret_raises(self):
        token = create_mesh_token("n", "a", TEST_SECRET)
        with pytest.raises(jwt.InvalidSignatureError):
            verify_mesh_token(token, "wrong-secret")

    def test_verify_expired_token_raises(self):
        token = create_mesh_token("n", "a", TEST_SECRET, ttl=-1)
        with pytest.raises(jwt.ExpiredSignatureError):
            verify_mesh_token(token, TEST_SECRET)

    def test_verify_garbage_raises(self):
        with pytest.raises(jwt.DecodeError):
            verify_mesh_token("not.a.jwt", TEST_SECRET)
```

**Step 2: Run tests to verify they fail**

Run: `cd ~/soul/soul-mesh && python -m pytest tests/test_auth.py -v`
Expected: FAIL -- `ModuleNotFoundError: No module named 'soul_mesh.auth'`

**Step 3: Implement auth module**

```python
# soul_mesh/auth.py
"""Standalone JWT mesh token creation and verification.

Replaces brain.auth.jwt with a self-contained module using PyJWT.
"""

from __future__ import annotations

import time

import jwt


def create_mesh_token(
    node_id: str,
    account_id: str,
    secret: str,
    ttl: int = 3600,
) -> str:
    """Create a signed JWT mesh token.

    Parameters
    ----------
    node_id : str
        The sending node's unique ID.
    account_id : str
        The account this node belongs to.
    secret : str
        HMAC signing key.
    ttl : int
        Token lifetime in seconds (default 1 hour).
    """
    now = int(time.time())
    payload = {
        "node_id": node_id,
        "account_id": account_id,
        "type": "mesh",
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_mesh_token(token: str, secret: str) -> dict:
    """Verify and decode a mesh JWT token.

    Raises jwt.InvalidSignatureError, jwt.ExpiredSignatureError,
    or jwt.DecodeError on failure.
    """
    return jwt.decode(token, secret, algorithms=["HS256"])
```

**Step 4: Run tests to verify they pass**

Run: `cd ~/soul/soul-mesh && python -m pytest tests/test_auth.py -v`
Expected: All 9 tests PASS

**Step 5: Commit**

```bash
git add soul_mesh/auth.py tests/test_auth.py
git commit -m "feat: add standalone mesh JWT auth replacing brain.auth.jwt"
```

---

### Task 4: Extract and adapt transport.py with tests (TDD)

**Files:**
- Create: `soul_mesh/transport.py`
- Create: `tests/test_transport.py`

**Step 1: Write failing tests**

```python
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
            if n.get("is_hub") and n.get("status") == "online":
                return n
        return None

    def add_node(self, node_id, host="127.0.0.1", port=8340, is_hub=False):
        self._nodes.append({
            "id": node_id, "host": host, "port": port,
            "is_hub": is_hub, "status": "online",
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
        db.add_node("hub-1", is_hub=True)
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
```

**Step 2: Run tests to verify they fail**

Run: `cd ~/soul/soul-mesh && python -m pytest tests/test_transport.py -v`
Expected: FAIL -- `ModuleNotFoundError: No module named 'soul_mesh.transport'`

**Step 3: Implement transport.py**

Copy from `~/soul-os/brain/mesh/transport.py` and adapt:

```python
# soul_mesh/transport.py
"""Mesh transport -- authenticated WebSocket communication.

Extracted from soul-os. Uses standalone MeshDB and auth modules
instead of brain.db.store and brain.auth.jwt.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import structlog

from soul_mesh.auth import create_mesh_token
from soul_mesh.db import MeshDB

logger = structlog.get_logger("soul-mesh.transport")

MAX_RECONNECT_DELAY = 300
MAX_MESSAGE_SIZE = 1_048_576  # 1 MiB
HEARTBEAT_INTERVAL = 30


class MeshTransport:
    """WebSocket-based mesh transport with auth and reconnection.

    Parameters
    ----------
    local_node : NodeInfo
        The local node instance.
    db : MeshDB
        Database wrapper for peer lookups.
    secret : str
        JWT signing secret for mesh tokens.
    """

    def __init__(self, local_node, db: MeshDB, secret: str) -> None:
        self._local = local_node
        self._db = db
        self._secret = secret
        self._handlers: dict[str, Callable[..., Awaitable]] = {}
        self._connections: dict[str, Any] = {}
        self._outbound_tasks: dict[str, asyncio.Task] = {}
        self._running = False

    def on(self, msg_type: str, handler: Callable[..., Awaitable]) -> None:
        """Register a handler for a mesh message type."""
        self._handlers[msg_type] = handler

    async def start(self) -> None:
        self._running = True
        peers = await self._db.fetch_all(
            "SELECT id, host, port FROM mesh_nodes "
            "WHERE id != ? AND status = 'online'",
            (self._local.id,),
        )
        for peer in peers:
            self._start_outbound(peer["id"], peer["host"], peer["port"])

    async def stop(self) -> None:
        self._running = False
        for task in self._outbound_tasks.values():
            task.cancel()
        for pid, ws in list(self._connections.items()):
            try:
                await ws.close()
            except Exception as exc:
                logger.debug("Error closing WS", peer_id=pid[:8], error=str(exc))
        self._connections.clear()
        self._outbound_tasks.clear()
        logger.info("Transport stopped")

    # -- outbound ---------------------------------------------------

    def _start_outbound(self, peer_id: str, host: str, port: int) -> None:
        if peer_id in self._outbound_tasks:
            return
        self._outbound_tasks[peer_id] = asyncio.create_task(
            self._connect_with_backoff(peer_id, host, port)
        )

    async def _connect_with_backoff(
        self, peer_id: str, host: str, port: int
    ) -> None:
        delay = 1
        while self._running:
            try:
                await self._connect_to_peer(peer_id, host, port)
                delay = 1
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning(
                    "Connection failed",
                    peer_id=peer_id[:8],
                    error=str(exc),
                    retry_delay=delay,
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)

    async def _connect_to_peer(
        self, peer_id: str, host: str, port: int
    ) -> None:
        token = create_mesh_token(
            self._local.id, self._local.account_id, self._secret
        )
        uri = f"ws://{host}:{port}/api/mesh/ws?token={token}"

        import websockets

        async with websockets.connect(
            uri,
            max_size=MAX_MESSAGE_SIZE,
            ping_interval=HEARTBEAT_INTERVAL,
            ping_timeout=HEARTBEAT_INTERVAL,
            close_timeout=5,
        ) as ws:
            self._connections[peer_id] = ws
            logger.info("Connected to peer", peer_id=peer_id[:8], host=host, port=port)
            try:
                async for raw in ws:
                    if len(raw) > MAX_MESSAGE_SIZE:
                        logger.warning(
                            "Oversized message dropped",
                            peer_id=peer_id[:8],
                            size=len(raw),
                        )
                        continue
                    data = json.loads(raw)
                    await self.handle_message(data, peer_id)
            finally:
                self._connections.pop(peer_id, None)

    # -- inbound ----------------------------------------------------

    async def register_inbound(self, peer_id: str, websocket: Any) -> None:
        self._connections[peer_id] = websocket
        logger.info("Peer connected (inbound)", peer_id=peer_id[:8])

    async def unregister(self, peer_id: str) -> None:
        self._connections.pop(peer_id, None)
        task = self._outbound_tasks.pop(peer_id, None)
        if task:
            task.cancel()
        logger.info("Peer disconnected", peer_id=peer_id[:8])

    # -- sending ----------------------------------------------------

    async def send(self, peer_id: str, msg_type: str, payload: Any) -> None:
        ws = self._connections.get(peer_id)
        if not ws:
            raise ConnectionError(f"No connection to peer {peer_id[:8]}")
        msg = json.dumps({
            "type": msg_type,
            "payload": payload,
            "from": self._local.id,
        })
        if len(msg) > MAX_MESSAGE_SIZE:
            raise ValueError(
                f"Message too large: {len(msg)} bytes (max {MAX_MESSAGE_SIZE})"
            )
        if hasattr(ws, "send_text"):
            await ws.send_text(msg)
        else:
            await ws.send(msg)

    async def send_to_hub(self, msg_type: str, payload: Any) -> None:
        hub = await self._db.fetch_one(
            "SELECT id FROM mesh_nodes WHERE is_hub = 1 AND status = 'online'"
        )
        if not hub:
            raise ConnectionError("No hub available")
        await self.send(hub["id"], msg_type, payload)

    async def broadcast(self, msg_type: str, payload: Any) -> None:
        for peer_id in list(self._connections):
            try:
                await self.send(peer_id, msg_type, payload)
            except Exception as exc:
                logger.warning("Broadcast failed", peer_id=peer_id[:8], error=str(exc))

    # -- routing ----------------------------------------------------

    async def handle_message(self, data: dict, peer_id: str) -> Any:
        msg_type = data.get("type", "")
        handler = self._handlers.get(msg_type)
        if not handler:
            logger.warning("No handler for message type", msg_type=msg_type)
            return None
        try:
            return await handler(data.get("payload"), peer_id)
        except Exception as exc:
            logger.error(
                "Handler failed", msg_type=msg_type, error=str(exc), exc_info=True
            )
            return None
```

**Step 4: Run tests to verify they pass**

Run: `cd ~/soul/soul-mesh && python -m pytest tests/test_transport.py -v`
Expected: All 19 tests PASS

**Step 5: Commit**

```bash
git add soul_mesh/transport.py tests/test_transport.py
git commit -m "feat: extract mesh transport with standalone db and auth deps"
```

---

### Task 5: Update __init__.py and run full test suite

**Files:**
- Modify: `soul_mesh/__init__.py`

**Step 1: Update exports**

```python
# soul_mesh/__init__.py
"""Soul-Mesh -- Multi-device mesh networking with hub election."""

__version__ = "0.1.0"

from soul_mesh.node import NodeInfo
from soul_mesh.election import HubElection, HYSTERESIS_MARGIN, elect_hub
from soul_mesh.db import MeshDB
from soul_mesh.auth import create_mesh_token, verify_mesh_token
from soul_mesh.transport import MeshTransport
```

**Step 2: Run full test suite**

Run: `cd ~/soul/soul-mesh && python -m pytest tests/ -v`
Expected: All tests PASS (16 existing + ~35 new = ~51 total)

**Step 3: Verify zero brain imports**

Run: `grep -r "from brain" ~/soul/soul-mesh/soul_mesh/`
Expected: No matches

**Step 4: Verify pip install works**

Run: `cd ~/soul/soul-mesh && pip install -e .`
Expected: Successfully installed

**Step 5: Commit**

```bash
git add soul_mesh/__init__.py
git commit -m "feat: export db, auth, transport from soul_mesh package"
```

---

## Batch Summary

| Batch | Tasks | What |
|-------|-------|------|
| 1 | Tasks 1-3 | Dependencies + db.py + auth.py with tests |
| 2 | Tasks 4-5 | transport.py with tests + integration verification |
