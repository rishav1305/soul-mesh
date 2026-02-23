"""Tests for the Textual TUI dashboard."""

from __future__ import annotations

import pytest


async def test_dashboard_module_imports():
    from soul_mesh.dashboard import MeshDashboard

    assert MeshDashboard is not None


async def test_dashboard_app_instantiation():
    from soul_mesh.dashboard import MeshDashboard

    app = MeshDashboard(hub_url="http://localhost:8340")
    assert app.hub_url == "http://localhost:8340"


async def test_dashboard_custom_hub_url():
    from soul_mesh.dashboard import MeshDashboard

    app = MeshDashboard(hub_url="http://192.168.0.113:9000")
    assert app.hub_url == "http://192.168.0.113:9000"


async def test_dashboard_screen_classes_importable():
    from soul_mesh.dashboard import ClusterOverview, NodeDetail

    assert ClusterOverview is not None
    assert NodeDetail is not None


async def test_node_detail_stores_node_id():
    from soul_mesh.dashboard import NodeDetail

    screen = NodeDetail(node_id="abc-123")
    assert screen.node_id == "abc-123"


async def test_cluster_header_update_totals():
    from soul_mesh.dashboard import ClusterHeader

    header = ClusterHeader()
    # Ensure update_totals is callable (widget not mounted, so just check method exists)
    assert callable(header.update_totals)


async def test_dashboard_initial_state():
    from soul_mesh.dashboard import MeshDashboard

    app = MeshDashboard(hub_url="http://localhost:8340")
    assert app._nodes == []
    assert app._status == {}
    assert app._heartbeat_cache == {}
    assert app._selected_node_id is None


async def test_alert_generation_node_went_stale():
    from soul_mesh.dashboard import generate_alerts

    old_nodes = [{"id": "n1", "name": "pi", "status": "online", "_ram_used_percent": 50}]
    new_nodes = [{"id": "n1", "name": "pi", "status": "stale", "_ram_used_percent": 50}]
    alerts = generate_alerts(old_nodes, new_nodes)
    assert len(alerts) == 1
    assert "stale" in alerts[0]["message"].lower()
    assert alerts[0]["severity"] == "warning"


async def test_alert_generation_high_ram():
    from soul_mesh.dashboard import generate_alerts

    old_nodes = [{"id": "n1", "name": "pi", "status": "online", "_ram_used_percent": 50}]
    new_nodes = [{"id": "n1", "name": "pi", "status": "online", "_ram_used_percent": 90}]
    alerts = generate_alerts(old_nodes, new_nodes)
    assert any("ram" in a["message"].lower() for a in alerts)


async def test_alert_generation_node_came_online():
    from soul_mesh.dashboard import generate_alerts

    old_nodes = [{"id": "n1", "name": "pi", "status": "stale", "_ram_used_percent": 50}]
    new_nodes = [{"id": "n1", "name": "pi", "status": "online", "_ram_used_percent": 50}]
    alerts = generate_alerts(old_nodes, new_nodes)
    assert any("online" in a["message"].lower() for a in alerts)


async def test_alert_generation_new_node():
    from soul_mesh.dashboard import generate_alerts

    old_nodes = []
    new_nodes = [{"id": "n1", "name": "pi", "status": "online", "_ram_used_percent": 50}]
    alerts = generate_alerts(old_nodes, new_nodes)
    assert len(alerts) == 1
    assert "appeared" in alerts[0]["message"].lower()
    assert alerts[0]["severity"] == "info"


async def test_alert_generation_no_change():
    from soul_mesh.dashboard import generate_alerts

    old_nodes = [{"id": "n1", "name": "pi", "status": "online", "_ram_used_percent": 50}]
    new_nodes = [{"id": "n1", "name": "pi", "status": "online", "_ram_used_percent": 50}]
    alerts = generate_alerts(old_nodes, new_nodes)
    assert len(alerts) == 0


async def test_alerts_screen_importable():
    from soul_mesh.dashboard import AlertsScreen

    assert AlertsScreen is not None


async def test_dashboard_has_alerts_deque():
    from soul_mesh.dashboard import MeshDashboard

    app = MeshDashboard(hub_url="http://localhost:8340")
    assert hasattr(app, "_alerts")
    assert app._alerts.maxlen == 100


def test_dashboard_cli_command_registered():
    """Ensure the dashboard command is registered in the CLI group."""
    from soul_mesh.cli import main

    commands = main.list_commands(ctx=None)
    assert "dashboard" in commands
