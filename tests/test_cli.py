"""Tests for the Click-based CLI."""

from __future__ import annotations

import uuid

import yaml
from click.testing import CliRunner

from soul_mesh.cli import main


class TestInit:
    """Tests for the ``soul-mesh init`` command."""

    def test_init_hub_creates_config_and_secret(self, tmp_path):
        """init --role hub creates config.yaml with role=hub and a generated secret."""
        runner = CliRunner()
        result = runner.invoke(main, ["init", "--role", "hub", "--config-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output

        config_path = tmp_path / "config.yaml"
        assert config_path.exists()

        cfg = yaml.safe_load(config_path.read_text())
        assert cfg["node"]["role"] == "hub"
        assert len(cfg["auth"]["secret"]) >= 32

    def test_init_agent_with_hub(self, tmp_path):
        """init --role agent --hub ... --secret ... creates config with hub address."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "init", "--role", "agent",
            "--hub", "192.168.1.10:8340",
            "--secret", "my-cluster-key",
            "--config-dir", str(tmp_path),
        ])
        assert result.exit_code == 0, result.output

        cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert cfg["node"]["role"] == "agent"
        assert cfg["node"]["hub"] == "192.168.1.10:8340"
        assert cfg["auth"]["secret"] == "my-cluster-key"

    def test_init_agent_without_hub_fails(self, tmp_path):
        """init --role agent without --hub should fail with an error."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "init", "--role", "agent",
            "--config-dir", str(tmp_path),
        ])
        assert result.exit_code != 0
        assert "hub" in result.output.lower() or "hub" in (result.exception and str(result.exception) or "").lower()

    def test_init_hub_with_name(self, tmp_path):
        """init --role hub --name my-pi sets the node name in config."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "init", "--role", "hub",
            "--name", "my-pi",
            "--config-dir", str(tmp_path),
        ])
        assert result.exit_code == 0, result.output

        cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert cfg["node"]["name"] == "my-pi"

    def test_init_creates_node_id_file(self, tmp_path):
        """init creates a node_id file containing a valid UUID."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "init", "--role", "hub",
            "--config-dir", str(tmp_path),
        ])
        assert result.exit_code == 0, result.output

        node_id_path = tmp_path / "node_id"
        assert node_id_path.exists()

        node_id = node_id_path.read_text().strip()
        # Should be a valid UUID
        uuid.UUID(node_id)


class TestStatus:
    """Tests for the ``soul-mesh status`` command."""

    def test_status_no_nodes(self, tmp_path):
        """status with no nodes shows '0' in output."""
        # Create a minimal hub config so the command can load it
        config = {
            "node": {"role": "hub", "port": 8340},
            "auth": {"secret": "test-secret"},
            "discovery": {"mdns": False},
        }
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        runner = CliRunner()
        result = runner.invoke(main, ["status", "--config-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "0" in result.output


class TestNodes:
    """Tests for the ``soul-mesh nodes`` command."""

    def test_nodes_no_nodes(self, tmp_path):
        """nodes with no registered nodes shows 'No nodes' message."""
        config = {
            "node": {"role": "hub", "port": 8340},
            "auth": {"secret": "test-secret"},
            "discovery": {"mdns": False},
        }
        (tmp_path / "config.yaml").write_text(yaml.dump(config))

        runner = CliRunner()
        result = runner.invoke(main, ["nodes", "--config-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "no nodes" in result.output.lower()
