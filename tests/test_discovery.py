"""Tests for mesh peer discovery -- mDNS and Tailscale."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from soul_mesh.discovery import PeerDiscovery, MdnsAnnouncer


class MockNode:
    def __init__(self, node_id="local-1", name="test-node", port=8340):
        self.id = node_id
        self.name = name
        self.host = ""
        self.port = port
        self.account_id = "acct-1"

    def capability_score(self):
        return 30.0


class TestMdnsAnnouncer:
    def test_init(self):
        node = MockNode()
        announcer = MdnsAnnouncer(node)
        assert announcer._node is node

    def test_service_info_type(self):
        node = MockNode()
        node.id = "test-id"
        announcer = MdnsAnnouncer(node)
        info = announcer._build_service_info("192.168.1.10")
        assert info is not None

    @pytest.mark.asyncio
    async def test_start_stop(self):
        node = MockNode()
        announcer = MdnsAnnouncer(node)
        with patch("soul_mesh.discovery.Zeroconf") as mock_zc:
            mock_instance = MagicMock()
            mock_zc.return_value = mock_instance
            await announcer.start("192.168.1.10")
            await announcer.stop()


class TestPeerDiscoveryInit:
    def test_defaults(self):
        node = MockNode()
        discovery = PeerDiscovery(local_node=node)
        assert discovery._discovery_interval == 30

    def test_custom_interval(self):
        node = MockNode()
        discovery = PeerDiscovery(local_node=node, discovery_interval=10)
        assert discovery._discovery_interval == 10

    def test_peers_starts_empty(self):
        node = MockNode()
        discovery = PeerDiscovery(local_node=node)
        assert discovery.peers == {}

    def test_get_online_peers_empty(self):
        node = MockNode()
        discovery = PeerDiscovery(local_node=node)
        assert discovery.get_online_peers() == []


class TestPeerDiscoveryOperations:
    def test_mark_peer_offline(self):
        node = MockNode()
        discovery = PeerDiscovery(local_node=node)
        discovery._peers["peer-1"] = {"id": "peer-1", "status": "online"}
        discovery.mark_peer_offline("peer-1")
        assert discovery._peers["peer-1"]["status"] == "offline"

    def test_mark_unknown_peer_offline_noop(self):
        node = MockNode()
        discovery = PeerDiscovery(local_node=node)
        discovery.mark_peer_offline("unknown")  # should not raise

    @pytest.mark.asyncio
    async def test_start_stop(self):
        node = MockNode()
        discovery = PeerDiscovery(local_node=node, discovery_interval=1)
        with patch.object(discovery, "_scan_peers", new_callable=AsyncMock):
            await discovery.start()
            assert discovery._running is True
            await discovery.stop()
            assert discovery._running is False

    def test_compute_capability(self):
        node = MockNode()
        discovery = PeerDiscovery(local_node=node)
        assert discovery._compute_capability(8192, 512000) == 60.0
        assert discovery._compute_capability(0, 0) == 0.0
        cap = discovery._compute_capability(4096, 256000)
        assert 25 < cap < 35
