# soul-mesh Transport Extraction Design

**Date:** 2026-02-22
**Status:** Approved
**Scope:** Extract transport.py from soul-os/brain/mesh, add db.py and auth.py standalone modules

## Context

soul-mesh has 3 modules extracted (node, discovery, election) with 16 passing tests. The next module is transport.py -- WebSocket-based mesh communication with JWT auth and exponential backoff reconnection. It depends on `brain.db.store` and `brain.auth.jwt` which need standalone replacements.

## Approach: Standalone aiosqlite wrapper (Approach A)

Replace soul-os dependencies with self-contained modules rather than abstract interfaces.

## New Files

### soul_mesh/db.py

Thin async SQLite wrapper replacing `brain.db.store`. Three methods:

- `fetch_all(sql, params)` -> list[dict]
- `fetch_one(sql, params)` -> dict | None
- `execute(sql, params)` -> None

Initialized with `db_path: str`. Uses `aiosqlite.connect()` per call (matches node.py pattern). Also provides `ensure_tables()` to create mesh schema.

### soul_mesh/auth.py

Standalone JWT mesh token module replacing `brain.auth.jwt`:

- `create_mesh_token(node_id, account_id, secret, ttl=3600)` -> str
- `verify_mesh_token(token, secret)` -> dict (raises on invalid)

Uses PyJWT directly. Secret passed explicitly, never hardcoded.

### soul_mesh/transport.py

Extracted from `soul-os/brain/mesh/transport.py` with these changes:

| Original | Replacement |
|----------|-------------|
| `from brain.db import store` | `from soul_mesh.db import MeshDB` |
| `store.fetch_all(...)` | `self._db.fetch_all(...)` |
| `store.fetch_one(...)` | `self._db.fetch_one(...)` |
| `from brain.auth.jwt import create_mesh_token` | `from soul_mesh.auth import create_mesh_token` |
| Global `store` singleton | `MeshDB` instance passed to constructor |
| `logging.getLogger` | `structlog.get_logger` (consistency) |

Constructor: `MeshTransport(local_node, db, secret)` -- db is MeshDB, secret is JWT signing key.

All transport logic unchanged: reconnection with exponential backoff, message type routing, send/send_to_hub/broadcast, 1 MiB message limit, 30s heartbeat.

### Tests

- `tests/test_db.py` -- MeshDB CRUD operations, schema creation
- `tests/test_auth.py` -- token creation, verification, expiry, invalid tokens
- `tests/test_transport.py` -- message routing, send/broadcast, connection management, reconnect logic

### Dependencies

Add to pyproject.toml:
- `websockets>=12.0`
- `PyJWT>=2.8`
- `aiosqlite>=0.19` (already used by node.py)

## Verification Criteria

1. `grep -r "from brain" soul_mesh/` returns ZERO results
2. All existing 16 tests still pass
3. New transport/db/auth tests pass
4. `pip install -e .` works
