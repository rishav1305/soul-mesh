"""Live system resource collection without psutil.

Collects CPU, memory, and storage metrics using only the standard
library: ``os.cpu_count()``, ``os.getloadavg()``, ``/proc/meminfo``
(Linux) or ``sysctl`` (macOS), and ``df`` subprocess calls.

Used by the heartbeat system to report node health to the hub.
"""

from __future__ import annotations

import asyncio
import os
import platform
from pathlib import Path

import structlog

logger = structlog.get_logger("soul-mesh.resources")

_IS_LINUX = platform.system() == "Linux"
_IS_MACOS = platform.system() == "Darwin"


async def get_cpu_info() -> dict:
    """Return CPU core count, usage estimate, and 1-minute load average.

    Returns
    -------
    dict
        ``cores`` (int), ``usage_percent`` (float), ``load_avg_1m`` (float).
    """
    cores = os.cpu_count() or 1
    load_1m, _load_5m, _load_15m = os.getloadavg()
    # Approximate CPU usage: (1-min load / cores) * 100, capped at 100
    usage = min(load_1m / cores * 100.0, 100.0)
    return {
        "cores": cores,
        "usage_percent": round(usage, 1),
        "load_avg_1m": round(load_1m, 2),
    }


async def get_memory_info() -> dict:
    """Return total, available, and used-percent memory.

    On Linux, parses ``/proc/meminfo``.  On macOS, shells out to ``sysctl``.

    Returns
    -------
    dict
        ``total_mb`` (int), ``available_mb`` (int), ``used_percent`` (float).
    """
    if _IS_LINUX:
        return await _memory_from_procfs()
    if _IS_MACOS:
        return await _memory_from_sysctl()
    raise RuntimeError(f"Unsupported platform: {platform.system()}")


async def _memory_from_procfs() -> dict:
    """Parse /proc/meminfo for memory stats (Linux)."""
    meminfo: dict[str, int] = {}
    proc_path = Path("/proc/meminfo")
    content = await asyncio.to_thread(proc_path.read_text)
    for line in content.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            key = parts[0].rstrip(":")
            # Values in /proc/meminfo are in kB
            try:
                meminfo[key] = int(parts[1])
            except ValueError:
                continue

    total_kb = meminfo.get("MemTotal", 0)
    available_kb = meminfo.get("MemAvailable", 0)
    total_mb = total_kb // 1024
    available_mb = available_kb // 1024
    used_percent = 0.0
    if total_kb > 0:
        used_percent = round((1.0 - available_kb / total_kb) * 100.0, 1)

    return {
        "total_mb": total_mb,
        "available_mb": available_mb,
        "used_percent": used_percent,
    }


async def _memory_from_sysctl() -> dict:
    """Use sysctl to get memory stats (macOS)."""
    proc = await asyncio.create_subprocess_exec(
        "sysctl", "-n", "hw.memsize",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    total_bytes = int(stdout.decode().strip())
    total_mb = total_bytes // (1024 * 1024)

    # vm_stat gives page-level stats
    proc = await asyncio.create_subprocess_exec(
        "vm_stat",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    pages: dict[str, int] = {}
    page_size = 4096  # default, parsed from first line if available
    for line in stdout.decode().splitlines():
        if "page size of" in line:
            try:
                page_size = int(line.split("page size of")[1].strip().rstrip(".").split()[0])
            except (ValueError, IndexError):
                pass
        elif ":" in line:
            key, _, val = line.partition(":")
            try:
                pages[key.strip()] = int(val.strip().rstrip("."))
            except ValueError:
                continue

    free_pages = pages.get("Pages free", 0)
    inactive_pages = pages.get("Pages inactive", 0)
    available_bytes = (free_pages + inactive_pages) * page_size
    available_mb = available_bytes // (1024 * 1024)
    used_percent = 0.0
    if total_bytes > 0:
        used_percent = round((1.0 - available_bytes / total_bytes) * 100.0, 1)

    return {
        "total_mb": total_mb,
        "available_mb": available_mb,
        "used_percent": used_percent,
    }


async def get_storage_info() -> dict:
    """Return mounted filesystem sizes and free space.

    On Linux, uses ``df -BG --output=target,size,avail``.
    On macOS, uses ``df -g``.

    Returns
    -------
    dict
        ``mounts`` (list of dict): each has ``path`` (str),
        ``total_gb`` (int), ``free_gb`` (int).
    """
    if _IS_LINUX:
        return await _storage_linux()
    if _IS_MACOS:
        return await _storage_macos()
    raise RuntimeError(f"Unsupported platform: {platform.system()}")


async def _storage_linux() -> dict:
    """Parse df output on Linux."""
    proc = await asyncio.create_subprocess_exec(
        "df", "-BG", "--output=target,size,avail",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    lines = stdout.decode().strip().splitlines()
    mounts: list[dict] = []
    # Skip header line
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        path = parts[0]
        # Values end with 'G', e.g. "50G"
        try:
            total_gb = int(parts[1].rstrip("G"))
            free_gb = int(parts[2].rstrip("G"))
        except ValueError:
            continue
        # Skip pseudo-filesystems
        if path.startswith(("/dev", "/sys", "/proc", "/run", "/snap")):
            continue
        mounts.append({"path": path, "total_gb": total_gb, "free_gb": free_gb})
    return {"mounts": mounts}


async def _storage_macos() -> dict:
    """Parse df output on macOS."""
    proc = await asyncio.create_subprocess_exec(
        "df", "-g",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    lines = stdout.decode().strip().splitlines()
    mounts: list[dict] = []
    # Skip header: Filesystem 1G-blocks Used Available Capacity ...  Mounted on
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        try:
            total_gb = int(parts[1])
            free_gb = int(parts[3])
        except ValueError:
            continue
        path = parts[8]
        # Skip pseudo-filesystems
        if path.startswith(("/dev", "/sys", "/proc")):
            continue
        mounts.append({"path": path, "total_gb": total_gb, "free_gb": free_gb})
    return {"mounts": mounts}


async def get_system_snapshot() -> dict:
    """Collect CPU, memory, and storage info concurrently.

    Returns
    -------
    dict
        Keys: ``cpu``, ``memory``, ``storage`` -- each the result
        of the corresponding ``get_*`` function.
    """
    cpu, memory, storage = await asyncio.gather(
        get_cpu_info(),
        get_memory_info(),
        get_storage_info(),
    )
    return {"cpu": cpu, "memory": memory, "storage": storage}
