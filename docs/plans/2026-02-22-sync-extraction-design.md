# soul-mesh Sync Extraction Design

**Date:** 2026-02-22
**Status:** Approved
**Scope:** Extract sync.py from soul-os/brain/mesh, add insert() to MeshDB, add pending_writes table

## Context

soul-mesh has 6 modules extracted (node, discovery, election, db, auth, transport) with 52 passing tests. The next module is sync.py -- hub-proxy write coordinator with offline pending queue. It depends on `brain.db.store.insert()` (which MeshDB doesn't have yet) and `brain.config.settings.mesh_sync_interval_seconds`.

## Approach

Replace soul-os dependencies with constructor parameters and extend MeshDB.

## Changes

### MeshDB Extensions (soul_mesh/db.py)

Add `insert(table, data)` method:
- Builds `INSERT INTO {table} ({cols}) VALUES ({placeholders})` from dict keys
- Uses parameterized `?` placeholders (no SQL injection)
- Returns `cursor.lastrowid`
- Table name validated against allowlist to prevent injection

Add `pending_writes` table to `ensure_tables()`:
- `id` INTEGER PRIMARY KEY AUTOINCREMENT
- `target_table` TEXT
- `payload` TEXT
- `status` TEXT DEFAULT 'pending'
- `retry_count` INTEGER DEFAULT 0

### soul_mesh/sync.py

Extracted from `soul-os/brain/mesh/sync.py` with these changes:

| Original | Replacement |
|----------|-------------|
| `from brain.config import settings` | Constructor param `sync_interval` |
| `from brain.db import store` | `MeshDB` instance via constructor |
| `store.insert(table, data)` | `self._db.insert(table, data)` |
| `store.fetch_one(...)` | `self._db.fetch_one(...)` |
| `store.fetch_all(...)` | `self._db.fetch_all(...)` |
| `store.execute(...)` | `self._db.execute(...)` |
| `settings.mesh_sync_interval_seconds` | `self._sync_interval` |
| `logging.getLogger` | `structlog.get_logger` |

Constructor: `MeshSync(local_node, transport, db, sync_interval=30)`

All sync logic unchanged: table/column allowlists, account verification, pending queue with max 10k depth, batch replay of 50, max 10 retries.

### Tests

- `tests/test_db.py` -- add tests for insert() method and pending_writes table
- `tests/test_sync.py` -- hub write, non-hub forwarding, offline queue, replay loop, table allowlist, column filtering, account mismatch

## Verification Criteria

1. `grep -r "from brain" soul_mesh/` returns ZERO results
2. All existing 52 tests still pass
3. New sync/db tests pass
4. `pip install -e .` works
