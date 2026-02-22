"""Tests for the agent heartbeat loop -- unit tests only, no WebSocket connection."""

from __future__ import annotations

import asyncio

import pytest

from soul_mesh.config import MeshConfig
from soul_mesh.agent import Agent


def _agent_config() -> MeshConfig:
    """Standard test config for an agent node."""
    return MeshConfig(name="test", role="agent", hub="10.0.0.1:8340", secret="s")


class TestAgentInit:
    """Agent(config) -- stores config, not running initially."""

    def test_stores_config(self):
        cfg = _agent_config()
        agent = Agent(cfg, node_id_path=":memory:")
        assert agent.config is cfg

    def test_not_running_initially(self):
        agent = Agent(_agent_config(), node_id_path=":memory:")
        assert agent.running is False

    def test_ws_is_none_initially(self):
        agent = Agent(_agent_config(), node_id_path=":memory:")
        assert agent._ws is None

    def test_task_is_none_initially(self):
        agent = Agent(_agent_config(), node_id_path=":memory:")
        assert agent._task is None


class TestBuildHeartbeat:
    """agent._build_heartbeat() -- merges node identity with system snapshot."""

    async def test_returns_dict(self):
        agent = Agent(_agent_config(), node_id_path=":memory:")
        await agent._init_node()
        hb = await agent._build_heartbeat()
        assert isinstance(hb, dict)

    async def test_has_node_id(self):
        agent = Agent(_agent_config(), node_id_path=":memory:")
        await agent._init_node()
        hb = await agent._build_heartbeat()
        assert "node_id" in hb
        assert isinstance(hb["node_id"], str)
        assert len(hb["node_id"]) > 0

    async def test_has_cpu_with_cores(self):
        agent = Agent(_agent_config(), node_id_path=":memory:")
        await agent._init_node()
        hb = await agent._build_heartbeat()
        assert "cpu" in hb
        assert "cores" in hb["cpu"]
        assert hb["cpu"]["cores"] >= 1

    async def test_has_memory_with_total_mb(self):
        agent = Agent(_agent_config(), node_id_path=":memory:")
        await agent._init_node()
        hb = await agent._build_heartbeat()
        assert "memory" in hb
        assert "total_mb" in hb["memory"]
        assert hb["memory"]["total_mb"] > 0

    async def test_has_storage_with_mounts(self):
        agent = Agent(_agent_config(), node_id_path=":memory:")
        await agent._init_node()
        hb = await agent._build_heartbeat()
        assert "storage" in hb
        assert "mounts" in hb["storage"]
        assert isinstance(hb["storage"]["mounts"], list)

    async def test_has_identity_fields(self):
        agent = Agent(_agent_config(), node_id_path=":memory:")
        await agent._init_node()
        hb = await agent._build_heartbeat()
        assert hb["name"] == "test"
        assert isinstance(hb["platform"], str)
        assert isinstance(hb["arch"], str)
        assert len(hb["platform"]) > 0
        assert len(hb["arch"]) > 0


class TestAgentStop:
    """agent.stop() -- sets running to False."""

    async def test_stop_sets_running_false(self):
        agent = Agent(_agent_config(), node_id_path=":memory:")
        agent.running = True
        await agent.stop()
        assert agent.running is False

    async def test_stop_when_already_stopped(self):
        agent = Agent(_agent_config(), node_id_path=":memory:")
        assert agent.running is False
        await agent.stop()
        assert agent.running is False

    async def test_stop_cancels_task(self):
        agent = Agent(_agent_config(), node_id_path=":memory:")
        agent.running = True
        # Create a dummy task so we can verify it gets cancelled
        agent._task = asyncio.ensure_future(asyncio.sleep(999))
        await agent.stop()
        assert agent._task is None or agent._task.cancelled()
