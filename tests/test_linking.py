"""Tests for device linking -- pair devices to the same mesh account."""

from __future__ import annotations

import pytest

from soul_mesh.db import MeshDB
from soul_mesh.linking import (
    MAX_GLOBAL_LINK_ATTEMPTS_PER_15MIN,
    MAX_LINK_ATTEMPTS_PER_15MIN,
    generate_link_code,
    get_or_create_account_id,
    redeem_link_code,
)


@pytest.fixture
async def db(tmp_path):
    """Create a MeshDB with all tables."""
    db_path = str(tmp_path / "test.db")
    db = MeshDB(db_path)
    await db.ensure_tables()
    return db


class TestConstants:
    def test_max_link_attempts(self):
        assert MAX_LINK_ATTEMPTS_PER_15MIN == 5

    def test_max_global_attempts(self):
        assert MAX_GLOBAL_LINK_ATTEMPTS_PER_15MIN == 50


class TestGenerateLinkCode:
    async def test_returns_string(self, db):
        code = await generate_link_code(db, "acct-1")
        assert isinstance(code, str)
        assert len(code) > 0

    async def test_code_stored_in_db(self, db):
        code = await generate_link_code(db, "acct-1")
        row = await db.fetch_one(
            "SELECT account_id FROM link_codes WHERE code = ?", (code,)
        )
        assert row is not None
        assert row["account_id"] == "acct-1"

    async def test_codes_are_unique(self, db):
        codes = set()
        for _ in range(10):
            code = await generate_link_code(db, "acct-1")
            codes.add(code)
        assert len(codes) == 10

    async def test_expired_codes_cleaned(self, db):
        # Insert an already-expired code
        await db.execute(
            "INSERT INTO link_codes (code, account_id, expires_at) "
            "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 minutes'))",
            ("old-code", "acct-old"),
        )
        await generate_link_code(db, "acct-1")
        row = await db.fetch_one(
            "SELECT code FROM link_codes WHERE code = ?", ("old-code",)
        )
        assert row is None


class TestRedeemLinkCode:
    async def test_valid_code_returns_account_id(self, db):
        await db.insert("nodes", {"id": "node-1", "name": "test", "status": "online"})
        code = await generate_link_code(db, "acct-1")
        result = await redeem_link_code(db, code, "node-1", "127.0.0.1")
        assert result == "acct-1"

    async def test_code_deleted_after_redemption(self, db):
        await db.insert("nodes", {"id": "node-1", "name": "test", "status": "online"})
        code = await generate_link_code(db, "acct-1")
        await redeem_link_code(db, code, "node-1", "127.0.0.1")
        row = await db.fetch_one("SELECT code FROM link_codes WHERE code = ?", (code,))
        assert row is None

    async def test_invalid_code_returns_none(self, db):
        result = await redeem_link_code(db, "bad-code", "node-1", "127.0.0.1")
        assert result is None

    async def test_expired_code_returns_none(self, db):
        await db.execute(
            "INSERT INTO link_codes (code, account_id, expires_at) "
            "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 minutes'))",
            ("expired", "acct-1"),
        )
        result = await redeem_link_code(db, "expired", "node-1", "127.0.0.1")
        assert result is None

    async def test_sets_account_on_node(self, db):
        await db.insert("nodes", {"id": "node-1", "name": "test", "status": "online"})
        code = await generate_link_code(db, "acct-1")
        await redeem_link_code(db, code, "node-1", "127.0.0.1")
        row = await db.fetch_one(
            "SELECT account_id FROM nodes WHERE id = ?", ("node-1",)
        )
        assert row["account_id"] == "acct-1"

    async def test_saves_account_to_settings(self, db):
        await db.insert("nodes", {"id": "node-1", "name": "test", "status": "online"})
        code = await generate_link_code(db, "acct-1")
        await redeem_link_code(db, code, "node-1", "127.0.0.1")
        row = await db.fetch_one(
            "SELECT value FROM settings WHERE key = 'mesh_account_id'"
        )
        assert row["value"] == "acct-1"

    async def test_updates_existing_settings(self, db):
        await db.insert("settings", {"key": "mesh_account_id", "value": "old-acct"})
        await db.insert("nodes", {"id": "node-1", "name": "test", "status": "online"})
        code = await generate_link_code(db, "new-acct")
        await redeem_link_code(db, code, "node-1", "127.0.0.1")
        row = await db.fetch_one(
            "SELECT value FROM settings WHERE key = 'mesh_account_id'"
        )
        assert row["value"] == "new-acct"


class TestRateLimiting:
    async def test_per_ip_rate_limit(self, db):
        await db.insert("nodes", {"id": "node-1", "name": "test", "status": "online"})
        for i in range(MAX_LINK_ATTEMPTS_PER_15MIN):
            code = await generate_link_code(db, "acct-1")
            await redeem_link_code(db, code, "node-1", "1.2.3.4")
        # Next attempt should be rate limited
        code = await generate_link_code(db, "acct-1")
        result = await redeem_link_code(db, code, "node-1", "1.2.3.4")
        assert result is None

    async def test_different_ip_not_limited(self, db):
        await db.insert("nodes", {"id": "node-1", "name": "test", "status": "online"})
        for i in range(MAX_LINK_ATTEMPTS_PER_15MIN):
            code = await generate_link_code(db, "acct-1")
            await redeem_link_code(db, code, "node-1", "1.2.3.4")
        # Different IP should still work
        code = await generate_link_code(db, "acct-1")
        result = await redeem_link_code(db, code, "node-1", "5.6.7.8")
        assert result == "acct-1"


class TestGetOrCreateAccountId:
    async def test_creates_new_account(self, db):
        account_id = await get_or_create_account_id(db)
        assert isinstance(account_id, str)
        assert len(account_id) == 32  # hex(16) = 32 chars

    async def test_returns_existing_account(self, db):
        await db.insert("settings", {"key": "mesh_account_id", "value": "existing-acct"})
        account_id = await get_or_create_account_id(db)
        assert account_id == "existing-acct"

    async def test_idempotent(self, db):
        id1 = await get_or_create_account_id(db)
        id2 = await get_or_create_account_id(db)
        assert id1 == id2
