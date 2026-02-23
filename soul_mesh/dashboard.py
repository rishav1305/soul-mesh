"""Textual TUI dashboard for soul-mesh.

Two screens:
- **Cluster Overview** (default): live-updating node table with sparklines.
- **Node Detail**: drill-down for a selected node with full specs and graphs.

Launch via ``soul-mesh dashboard --hub http://localhost:8340``.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Sparkline, Static

import httpx
from rich.text import Text


# ---------------------------------------------------------------------------
# Cluster Overview screen
# ---------------------------------------------------------------------------

class ClusterHeader(Static):
    """Aggregated cluster totals banner."""

    def update_totals(self, status: dict) -> None:
        nodes = status.get("nodes_online", 0)
        cores = status.get("cpu_cores", 0)
        ram = status.get("ram_total_mb", 0)
        storage = status.get("storage_total_gb", 0.0)
        self.update(
            f"Nodes: {nodes}  |  CPU Cores: {cores}  |  "
            f"RAM: {ram} MB  |  Storage: {storage:.1f} GB"
        )


class ClusterOverview(Screen):
    """Live-updating node table with status colours and sparklines."""

    BINDINGS = [
        Binding("enter", "select_node", "Node Detail"),
        Binding("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield ClusterHeader(id="cluster-header")
        yield Vertical(
            DataTable(id="node-table"),
            id="table-container",
        )
        yield Horizontal(
            Vertical(
                Static("CPU %", classes="spark-label"),
                Sparkline(data=[], id="cpu-spark"),
                id="cpu-spark-box",
            ),
            Vertical(
                Static("RAM %", classes="spark-label"),
                Sparkline(data=[], id="ram-spark"),
                id="ram-spark-box",
            ),
            id="spark-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#node-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Name", "Status", "CPU %", "RAM %", "Storage (GB)", "Cores", "RAM (MB)")

    def refresh_table(self, nodes: list[dict]) -> None:
        """Rebuild the node table from fresh API data."""
        table = self.query_one("#node-table", DataTable)
        table.clear()
        for node in nodes:
            status_str = node.get("status", "offline")
            if status_str == "online":
                status_cell = Text("online", style="green")
            elif status_str == "stale":
                status_cell = Text("stale", style="red")
            else:
                status_cell = Text(status_str, style="dim")

            ram_pct = node.get("_ram_used_percent", 0.0)
            if ram_pct > 85:
                ram_cell = Text(f"{ram_pct:.1f}", style="yellow")
            else:
                ram_cell = Text(f"{ram_pct:.1f}")

            cpu_pct = node.get("_cpu_usage_percent", 0.0)

            table.add_row(
                node.get("name", ""),
                status_cell,
                f"{cpu_pct:.1f}",
                ram_cell,
                f"{node.get('storage_total_gb', 0):.1f}",
                str(node.get("cpu_cores", 0)),
                str(node.get("ram_total_mb", 0)),
                key=node.get("id", ""),
            )

    def update_sparklines(self, cpu_data: list[float], ram_data: list[float]) -> None:
        """Update the overview sparklines with aggregated cluster data."""
        self.query_one("#cpu-spark", Sparkline).data = cpu_data
        self.query_one("#ram-spark", Sparkline).data = ram_data

    def update_header(self, status: dict) -> None:
        self.query_one("#cluster-header", ClusterHeader).update_totals(status)

    def action_select_node(self) -> None:
        """Drill into the highlighted node."""
        table = self.query_one("#node-table", DataTable)
        if table.row_count == 0:
            return
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        node_id = str(row_key)
        self.app.show_node_detail(node_id)

    def action_refresh(self) -> None:
        self.app.force_refresh()


# ---------------------------------------------------------------------------
# Node Detail screen
# ---------------------------------------------------------------------------

class NodeDetail(Screen):
    """Full specs and live graphs for a single node."""

    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, node_id: str) -> None:
        super().__init__()
        self.node_id = node_id

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading...", id="node-info")
        yield Horizontal(
            Vertical(
                Static("CPU %", classes="spark-label"),
                Sparkline(data=[], id="detail-cpu-spark"),
                id="detail-cpu-box",
            ),
            Vertical(
                Static("RAM %", classes="spark-label"),
                Sparkline(data=[], id="detail-ram-spark"),
                id="detail-ram-box",
            ),
            id="detail-spark-container",
        )
        yield Static("", id="heartbeat-latest")
        yield Footer()

    def update_node(self, node: dict | None, heartbeats: list[dict]) -> None:
        """Populate the detail screen with node info and heartbeat history."""
        if node is None:
            self.query_one("#node-info", Static).update("Node not found.")
            return

        status_str = node.get("status", "offline")
        info_lines = (
            f"Name: {node.get('name', '')}  |  "
            f"Status: {status_str}  |  "
            f"Platform: {node.get('platform', '')}  |  "
            f"Arch: {node.get('arch', '')}\n"
            f"Cores: {node.get('cpu_cores', 0)}  |  "
            f"RAM: {node.get('ram_total_mb', 0)} MB  |  "
            f"Storage: {node.get('storage_total_gb', 0):.1f} GB"
        )
        self.query_one("#node-info", Static).update(info_lines)

        # Heartbeat history -- most-recent-first from API, reverse for sparkline
        # (sparkline draws left-to-right = oldest-to-newest)
        cpu_data = [hb.get("cpu_usage_percent", 0.0) for hb in reversed(heartbeats)]
        ram_data = [hb.get("ram_used_percent", 0.0) for hb in reversed(heartbeats)]

        self.query_one("#detail-cpu-spark", Sparkline).data = cpu_data or [0.0]
        self.query_one("#detail-ram-spark", Sparkline).data = ram_data or [0.0]

        # Latest heartbeat values
        if heartbeats:
            latest = heartbeats[0]
            latest_text = (
                f"Latest heartbeat  --  "
                f"CPU: {latest.get('cpu_usage_percent', 0):.1f}%  |  "
                f"RAM: {latest.get('ram_used_percent', 0):.1f}%  |  "
                f"Load 1m: {latest.get('cpu_load_1m', 0):.2f}  |  "
                f"RAM avail: {latest.get('ram_available_mb', 0)} MB  |  "
                f"Storage free: {latest.get('storage_free_gb', 0):.1f} GB"
            )
        else:
            latest_text = "No heartbeat data."
        self.query_one("#heartbeat-latest", Static).update(latest_text)

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_refresh(self) -> None:
        self.app.force_refresh()


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class MeshDashboard(App):
    """soul-mesh TUI dashboard."""

    TITLE = "soul-mesh dashboard"
    CSS = """
    #cluster-header {
        dock: top;
        height: 1;
        background: $primary-background;
        color: $text;
        padding: 0 1;
    }
    #table-container {
        height: 1fr;
    }
    #spark-container, #detail-spark-container {
        height: 5;
        padding: 0 1;
    }
    #cpu-spark-box, #ram-spark-box,
    #detail-cpu-box, #detail-ram-box {
        width: 1fr;
        padding: 0 1;
    }
    .spark-label {
        height: 1;
        color: $text-muted;
    }
    #node-info {
        padding: 1;
    }
    #heartbeat-latest {
        padding: 0 1;
        height: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("1", "show_overview", "Overview"),
        Binding("2", "show_detail", "Detail"),
    ]

    def __init__(self, hub_url: str = "http://localhost:8340") -> None:
        super().__init__()
        self.hub_url = hub_url
        self._client = httpx.AsyncClient(base_url=hub_url, timeout=5.0)
        self._nodes: list[dict] = []
        self._status: dict = {}
        self._heartbeat_cache: dict[str, list[dict]] = {}
        self._selected_node_id: str | None = None

    def on_mount(self) -> None:
        self.push_screen(ClusterOverview())
        self.set_interval(3.0, self._poll_data)
        # Fire first poll immediately
        self.call_later(self._poll_data)

    async def _poll_data(self) -> None:
        """Fetch nodes and status from the hub API."""
        try:
            resp = await self._client.get("/api/mesh/nodes")
            resp.raise_for_status()
            self._nodes = resp.json()
        except (httpx.HTTPError, httpx.StreamError):
            pass  # Hub unreachable -- keep showing stale data

        try:
            status_resp = await self._client.get("/api/mesh/status")
            status_resp.raise_for_status()
            self._status = status_resp.json()
        except (httpx.HTTPError, httpx.StreamError):
            pass

        # Fetch latest heartbeat for each node to get live cpu/ram %
        for node in self._nodes:
            nid = node.get("id", "")
            if not nid:
                continue
            try:
                hb_resp = await self._client.get(
                    f"/api/mesh/nodes/{nid}/heartbeats", params={"limit": 30}
                )
                hb_resp.raise_for_status()
                heartbeats = hb_resp.json()
                self._heartbeat_cache[nid] = heartbeats
                # Attach latest CPU/RAM to node dict for table display
                if heartbeats:
                    node["_cpu_usage_percent"] = heartbeats[0].get("cpu_usage_percent", 0.0)
                    node["_ram_used_percent"] = heartbeats[0].get("ram_used_percent", 0.0)
            except (httpx.HTTPError, httpx.StreamError):
                pass

        self._update_active_screen()

    def _update_active_screen(self) -> None:
        """Push fresh data into whichever screen is currently active."""
        screen = self.screen
        if isinstance(screen, ClusterOverview):
            screen.refresh_table(self._nodes)
            screen.update_header(self._status)
            # Aggregate sparkline: average CPU/RAM across all nodes
            all_cpu: list[float] = []
            all_ram: list[float] = []
            for node in self._nodes:
                nid = node.get("id", "")
                hbs = self._heartbeat_cache.get(nid, [])
                for hb in reversed(hbs):
                    all_cpu.append(hb.get("cpu_usage_percent", 0.0))
                    all_ram.append(hb.get("ram_used_percent", 0.0))
            screen.update_sparklines(all_cpu or [0.0], all_ram or [0.0])

        elif isinstance(screen, NodeDetail):
            nid = screen.node_id
            node = next((n for n in self._nodes if n.get("id") == nid), None)
            heartbeats = self._heartbeat_cache.get(nid, [])
            screen.update_node(node, heartbeats)

    def show_node_detail(self, node_id: str) -> None:
        """Switch to the Node Detail screen for a given node."""
        self._selected_node_id = node_id
        self.push_screen(NodeDetail(node_id))

    def force_refresh(self) -> None:
        """Trigger an immediate data refresh (keybinding helper)."""
        self.call_later(self._poll_data)

    def action_show_overview(self) -> None:
        """Switch to the Cluster Overview screen."""
        # Pop back to overview if we're on detail
        if isinstance(self.screen, NodeDetail):
            self.pop_screen()

    def action_show_detail(self) -> None:
        """Switch to Node Detail for the last-selected node (if any)."""
        if self._selected_node_id and not isinstance(self.screen, NodeDetail):
            self.push_screen(NodeDetail(self._selected_node_id))

    async def action_quit(self) -> None:
        await self._client.aclose()
        self.exit()
