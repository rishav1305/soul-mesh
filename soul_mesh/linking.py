"""Device linking -- pair devices to the same mesh account.

Link-code redemption is rate-limited to prevent brute-force attacks.
"""

from __future__ import annotations

import secrets

import structlog

from soul_mesh.db import MeshDB

logger = structlog.get_logger("soul-mesh.linking")

MAX_LINK_ATTEMPTS_PER_15MIN = 5
MAX_GLOBAL_LINK_ATTEMPTS_PER_15MIN = 50


async def generate_link_code(db: MeshDB, account_id: str) -> str:
    """Generate a 16-char url-safe link code valid for 10 minutes."""
    code = secrets.token_urlsafe(12)
    await db.execute(
        "INSERT INTO link_codes (code, account_id, expires_at) "
        "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+10 minutes'))",
        (code, account_id),
    )
    await db.execute(
        "DELETE FROM link_codes "
        "WHERE expires_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
    )
    return code


async def redeem_link_code(
    db: MeshDB, code: str, node_id: str, ip_address: str
) -> str | None:
    """Validate a link code. Returns account_id or None.

    Rate limit check + attempt insert wrapped in a transaction.
    """
    try:
        async with db.transaction() as cursor:
            await cursor.execute(
                "SELECT COUNT(*) AS cnt FROM link_attempts "
                "WHERE ip_address = ? "
                "AND attempted_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-15 minutes')",
                (ip_address,),
            )
            recent = await cursor.fetchone()
            if recent and recent[0] >= MAX_LINK_ATTEMPTS_PER_15MIN:
                logger.warning("Link-code rate limit exceeded", ip=ip_address)
                return None

            await cursor.execute(
                "SELECT COUNT(*) AS cnt FROM link_attempts "
                "WHERE attempted_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-15 minutes')",
            )
            global_recent = await cursor.fetchone()
            if global_recent and global_recent[0] >= MAX_GLOBAL_LINK_ATTEMPTS_PER_15MIN:
                logger.warning("Global link-code rate limit exceeded")
                return None

            await cursor.execute(
                "INSERT INTO link_attempts (ip_address, code, success) VALUES (?, ?, 0)",
                (ip_address, code),
            )
    except Exception as exc:
        logger.error("Link code rate limit check failed", error=str(exc))
        return None

    row = await db.fetch_one(
        "SELECT account_id FROM link_codes "
        "WHERE code = ? "
        "AND expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
        (code,),
    )
    if not row:
        return None

    account_id = row["account_id"]
    await db.execute("DELETE FROM link_codes WHERE code = ?", (code,))

    existing = await db.fetch_one(
        "SELECT value FROM settings WHERE key = 'mesh_account_id'"
    )
    if existing:
        await db.execute(
            "UPDATE settings SET value = ? WHERE key = 'mesh_account_id'",
            (account_id,),
        )
    else:
        await db.insert(
            "settings", {"key": "mesh_account_id", "value": account_id}
        )

    await db.execute(
        "UPDATE nodes SET account_id = ? WHERE id = ?",
        (account_id, node_id),
    )

    logger.info("Device linked", node_id=node_id[:8], account_id=account_id[:8])
    return account_id


async def get_or_create_account_id(db: MeshDB) -> str:
    """Return the local account_id, creating one if needed."""
    row = await db.fetch_one(
        "SELECT value FROM settings WHERE key = 'mesh_account_id'"
    )
    if row:
        return row["value"]
    account_id = secrets.token_hex(16)
    await db.insert(
        "settings", {"key": "mesh_account_id", "value": account_id}
    )
    return account_id
