"""Tests for the hub election algorithm."""

from __future__ import annotations

import pytest

from soul_mesh.election import HYSTERESIS_MARGIN, HubElection, elect_hub


class TestElectHub:
    """Tests for the pure ``elect_hub`` function."""

    def test_single_node_becomes_hub(self):
        nodes = [{"id": "aaa", "name": "solo", "capability": 30.0, "is_hub": False}]
        assert elect_hub(nodes) == "aaa"

    def test_highest_capability_wins(self):
        nodes = [
            {"id": "aaa", "name": "weak", "capability": 20.0, "is_hub": False},
            {"id": "bbb", "name": "strong", "capability": 50.0, "is_hub": False},
            {"id": "ccc", "name": "medium", "capability": 35.0, "is_hub": False},
        ]
        assert elect_hub(nodes) == "bbb"

    def test_hysteresis_prevents_flip_flop(self):
        """Current hub stays if challenger is within the 20% margin."""
        nodes = [
            {"id": "hub1", "name": "current", "capability": 40.0, "is_hub": True},
            {"id": "chal", "name": "challenger", "capability": 45.0, "is_hub": False},
        ]
        # Challenger needs > 40 * 1.20 = 48.0 to win. 45 < 48, so hub keeps role.
        assert elect_hub(nodes) == "hub1"

    def test_challenger_exceeds_hysteresis(self):
        """Challenger wins when exceeding the hysteresis threshold."""
        nodes = [
            {"id": "hub1", "name": "current", "capability": 40.0, "is_hub": True},
            {"id": "chal", "name": "challenger", "capability": 50.0, "is_hub": False},
        ]
        # Challenger needs > 48.0. 50 > 48, so challenger wins.
        assert elect_hub(nodes) == "chal"

    def test_challenger_exactly_at_threshold_stays(self):
        """At exactly the threshold boundary, current hub keeps the role."""
        nodes = [
            {"id": "hub1", "name": "current", "capability": 40.0, "is_hub": True},
            {"id": "chal", "name": "challenger", "capability": 48.0, "is_hub": False},
        ]
        # threshold = 40 * 1.20 = 48.0; 48.0 is NOT > 48.0, hub stays
        assert elect_hub(nodes) == "hub1"

    def test_tie_break_by_name(self):
        """Equal capability: lower name wins (alphabetical sort)."""
        nodes = [
            {"id": "aaa", "name": "bravo", "capability": 40.0, "is_hub": False},
            {"id": "bbb", "name": "alpha", "capability": 40.0, "is_hub": False},
        ]
        assert elect_hub(nodes) == "bbb"  # "alpha" < "bravo"

    def test_tie_break_by_id(self):
        """Equal capability and name: lower id wins."""
        nodes = [
            {"id": "zzz", "name": "same", "capability": 40.0, "is_hub": False},
            {"id": "aaa", "name": "same", "capability": 40.0, "is_hub": False},
        ]
        assert elect_hub(nodes) == "aaa"  # "aaa" < "zzz"

    def test_explicit_current_hub_id(self):
        """The current_hub_id parameter overrides the is_hub field."""
        nodes = [
            {"id": "aaa", "name": "a", "capability": 40.0, "is_hub": False},
            {"id": "bbb", "name": "b", "capability": 42.0, "is_hub": False},
        ]
        # bbb is the top scorer, but with aaa as explicit hub,
        # bbb needs > 40 * 1.2 = 48.0. 42 < 48, so aaa stays.
        assert elect_hub(nodes, current_hub_id="aaa") == "aaa"

    def test_custom_hysteresis(self):
        """Custom hysteresis value is respected."""
        nodes = [
            {"id": "hub1", "name": "current", "capability": 40.0, "is_hub": True},
            {"id": "chal", "name": "challenger", "capability": 50.0, "is_hub": False},
        ]
        # With 50% hysteresis: threshold = 40 * 1.50 = 60.0; 50 < 60, hub stays
        assert elect_hub(nodes, hysteresis=0.50) == "hub1"
        # With 10% hysteresis: threshold = 40 * 1.10 = 44.0; 50 > 44, challenger wins
        assert elect_hub(nodes, hysteresis=0.10) == "chal"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            elect_hub([])

    def test_no_hub_in_list(self):
        """When no node has is_hub=True and no current_hub_id, top scorer wins."""
        nodes = [
            {"id": "aaa", "name": "a", "capability": 30.0, "is_hub": False},
            {"id": "bbb", "name": "b", "capability": 50.0, "is_hub": False},
        ]
        assert elect_hub(nodes) == "bbb"

    def test_current_hub_is_also_top(self):
        """When the current hub is already the top scorer, it stays."""
        nodes = [
            {"id": "hub1", "name": "best", "capability": 60.0, "is_hub": True},
            {"id": "chal", "name": "weak", "capability": 30.0, "is_hub": False},
        ]
        assert elect_hub(nodes) == "hub1"

    def test_hysteresis_margin_constant(self):
        assert HYSTERESIS_MARGIN == 0.20


class TestHubElection:
    """Tests for the stateful HubElection class."""

    def _make_node(self, node_id="local-1", name="local", cap=40.0):
        """Create a minimal mock node for testing."""

        class MockNode:
            def __init__(self):
                self.id = node_id
                self.name = name
                self.is_hub = False
                self._cap = cap

            def capability_score(self):
                return self._cap

        return MockNode()

    def test_run_no_peers_becomes_hub(self):
        node = self._make_node()
        election = HubElection(node)
        result = election.run([])
        assert result == node.id
        assert node.is_hub is True

    def test_run_local_wins(self):
        node = self._make_node(cap=50.0)
        all_nodes = [
            {"id": node.id, "name": node.name, "capability": 50.0, "is_hub": False},
            {"id": "remote-1", "name": "remote", "capability": 30.0, "is_hub": False},
        ]
        election = HubElection(node)
        result = election.run(all_nodes)
        assert result == node.id
        assert node.is_hub is True

    def test_run_local_loses(self):
        node = self._make_node(cap=20.0)
        all_nodes = [
            {"id": node.id, "name": node.name, "capability": 20.0, "is_hub": False},
            {"id": "remote-1", "name": "remote", "capability": 50.0, "is_hub": False},
        ]
        election = HubElection(node)
        result = election.run(all_nodes)
        assert result == "remote-1"
        assert node.is_hub is False
