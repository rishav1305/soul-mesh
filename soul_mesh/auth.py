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
