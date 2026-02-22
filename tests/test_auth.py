"""Tests for standalone mesh JWT authentication."""

from __future__ import annotations

import time

import jwt
import pytest

from soul_mesh.auth import create_mesh_token, verify_mesh_token


TEST_SECRET = "test-secret-key-for-mesh-32bytes!"


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
