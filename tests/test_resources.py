"""Tests for live system resource collection.

These tests run against the REAL system -- no mocking needed.
They validate that get_cpu_info, get_memory_info, get_storage_info,
and get_system_snapshot return sane values from the actual OS.
"""

from __future__ import annotations

import asyncio

import pytest

from soul_mesh.resources import (
    get_cpu_info,
    get_memory_info,
    get_storage_info,
    get_system_snapshot,
)


class TestGetCpuInfo:
    """CPU info from os.cpu_count and os.getloadavg."""

    @pytest.mark.asyncio
    async def test_cores_at_least_one(self):
        info = await get_cpu_info()
        assert info["cores"] >= 1

    @pytest.mark.asyncio
    async def test_cores_is_int(self):
        info = await get_cpu_info()
        assert isinstance(info["cores"], int)

    @pytest.mark.asyncio
    async def test_usage_percent_is_float(self):
        info = await get_cpu_info()
        assert isinstance(info["usage_percent"], float)

    @pytest.mark.asyncio
    async def test_usage_percent_non_negative(self):
        info = await get_cpu_info()
        assert info["usage_percent"] >= 0.0

    @pytest.mark.asyncio
    async def test_load_avg_1m_is_float(self):
        info = await get_cpu_info()
        assert isinstance(info["load_avg_1m"], float)

    @pytest.mark.asyncio
    async def test_load_avg_1m_non_negative(self):
        info = await get_cpu_info()
        assert info["load_avg_1m"] >= 0.0


class TestGetMemoryInfo:
    """Memory info parsed from /proc/meminfo (Linux) or sysctl (macOS)."""

    @pytest.mark.asyncio
    async def test_total_mb_positive(self):
        info = await get_memory_info()
        assert info["total_mb"] > 0

    @pytest.mark.asyncio
    async def test_total_mb_is_int(self):
        info = await get_memory_info()
        assert isinstance(info["total_mb"], int)

    @pytest.mark.asyncio
    async def test_available_mb_is_int(self):
        info = await get_memory_info()
        assert isinstance(info["available_mb"], int)

    @pytest.mark.asyncio
    async def test_available_mb_not_exceeds_total(self):
        info = await get_memory_info()
        assert info["available_mb"] <= info["total_mb"]

    @pytest.mark.asyncio
    async def test_used_percent_in_range(self):
        info = await get_memory_info()
        assert 0.0 <= info["used_percent"] <= 100.0

    @pytest.mark.asyncio
    async def test_used_percent_is_float(self):
        info = await get_memory_info()
        assert isinstance(info["used_percent"], float)


class TestGetStorageInfo:
    """Storage info from df subprocess."""

    @pytest.mark.asyncio
    async def test_mounts_is_list(self):
        info = await get_storage_info()
        assert isinstance(info["mounts"], list)

    @pytest.mark.asyncio
    async def test_at_least_one_mount(self):
        info = await get_storage_info()
        assert len(info["mounts"]) >= 1

    @pytest.mark.asyncio
    async def test_root_mount_present(self):
        info = await get_storage_info()
        paths = [m["path"] for m in info["mounts"]]
        assert "/" in paths

    @pytest.mark.asyncio
    async def test_mount_has_required_keys(self):
        info = await get_storage_info()
        for mount in info["mounts"]:
            assert "path" in mount
            assert "total_gb" in mount
            assert "free_gb" in mount

    @pytest.mark.asyncio
    async def test_mount_values_are_numeric(self):
        info = await get_storage_info()
        for mount in info["mounts"]:
            assert isinstance(mount["path"], str)
            assert isinstance(mount["total_gb"], (int, float))
            assert isinstance(mount["free_gb"], (int, float))

    @pytest.mark.asyncio
    async def test_free_not_exceeds_total(self):
        info = await get_storage_info()
        for mount in info["mounts"]:
            assert mount["free_gb"] <= mount["total_gb"]


class TestGetSystemSnapshot:
    """Full system snapshot combining cpu, memory, storage."""

    @pytest.mark.asyncio
    async def test_has_all_sections(self):
        snap = await get_system_snapshot()
        assert "cpu" in snap
        assert "memory" in snap
        assert "storage" in snap

    @pytest.mark.asyncio
    async def test_cpu_section_valid(self):
        snap = await get_system_snapshot()
        assert snap["cpu"]["cores"] >= 1
        assert isinstance(snap["cpu"]["usage_percent"], float)

    @pytest.mark.asyncio
    async def test_memory_section_valid(self):
        snap = await get_system_snapshot()
        assert snap["memory"]["total_mb"] > 0
        assert 0.0 <= snap["memory"]["used_percent"] <= 100.0

    @pytest.mark.asyncio
    async def test_storage_section_valid(self):
        snap = await get_system_snapshot()
        assert len(snap["storage"]["mounts"]) >= 1
        paths = [m["path"] for m in snap["storage"]["mounts"]]
        assert "/" in paths
