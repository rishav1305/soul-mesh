"""Tests for YAML config loader with env var overrides."""

from __future__ import annotations

import platform
from pathlib import Path

import pytest
import yaml

from soul_mesh.config import (
    CONFIG_DIR,
    DEFAULT_CONFIG_PATH,
    MeshConfig,
    load_config,
)


class TestConfigConstants:
    def test_config_dir_is_dot_soul_mesh(self):
        assert CONFIG_DIR == Path.home() / ".soul-mesh"

    def test_default_config_path(self):
        assert DEFAULT_CONFIG_PATH == CONFIG_DIR / "config.yaml"


class TestMeshConfigDefaults:
    def test_name_defaults_to_hostname(self):
        cfg = MeshConfig()
        assert cfg.name == platform.node()

    def test_role_defaults_to_agent(self):
        cfg = MeshConfig()
        assert cfg.role == "agent"

    def test_port_defaults_to_8340(self):
        cfg = MeshConfig()
        assert cfg.port == 8340

    def test_hub_defaults_to_empty(self):
        cfg = MeshConfig()
        assert cfg.hub == ""

    def test_secret_defaults_to_empty(self):
        cfg = MeshConfig()
        assert cfg.secret == ""

    def test_mdns_defaults_to_true(self):
        cfg = MeshConfig()
        assert cfg.mdns is True

    def test_tailscale_defaults_to_false(self):
        cfg = MeshConfig()
        assert cfg.tailscale is False

    def test_heartbeat_interval_defaults_to_10(self):
        cfg = MeshConfig()
        assert cfg.heartbeat_interval == 10

    def test_stale_timeout_defaults_to_30(self):
        cfg = MeshConfig()
        assert cfg.stale_timeout == 30


class TestMeshConfigFromDict:
    def test_parses_nested_node_section(self):
        data = {"node": {"role": "hub", "port": 9000, "hub": "192.168.1.1:8340"}}
        cfg = MeshConfig.from_dict(data)
        assert cfg.role == "hub"
        assert cfg.port == 9000
        assert cfg.hub == "192.168.1.1:8340"

    def test_parses_auth_section(self):
        data = {"auth": {"secret": "my-secret-key"}}
        cfg = MeshConfig.from_dict(data)
        assert cfg.secret == "my-secret-key"

    def test_parses_discovery_section(self):
        data = {"discovery": {"mdns": False, "tailscale": True}}
        cfg = MeshConfig.from_dict(data)
        assert cfg.mdns is False
        assert cfg.tailscale is True

    def test_parses_node_name(self):
        data = {"node": {"name": "titan-pc"}}
        cfg = MeshConfig.from_dict(data)
        assert cfg.name == "titan-pc"

    def test_parses_heartbeat_and_stale(self):
        data = {"node": {"heartbeat_interval": 5, "stale_timeout": 15}}
        cfg = MeshConfig.from_dict(data)
        assert cfg.heartbeat_interval == 5
        assert cfg.stale_timeout == 15

    def test_empty_dict_gives_defaults(self):
        cfg = MeshConfig.from_dict({})
        assert cfg.role == "agent"
        assert cfg.port == 8340

    def test_full_config(self):
        data = {
            "node": {
                "name": "pi-node",
                "role": "hub",
                "port": 7777,
                "hub": "10.0.0.1:8340",
                "heartbeat_interval": 20,
                "stale_timeout": 60,
            },
            "auth": {"secret": "top-secret"},
            "discovery": {"mdns": False, "tailscale": True},
        }
        cfg = MeshConfig.from_dict(data)
        assert cfg.name == "pi-node"
        assert cfg.role == "hub"
        assert cfg.port == 7777
        assert cfg.hub == "10.0.0.1:8340"
        assert cfg.secret == "top-secret"
        assert cfg.mdns is False
        assert cfg.tailscale is True
        assert cfg.heartbeat_interval == 20
        assert cfg.stale_timeout == 60


class TestWithEnvOverrides:
    def test_mesh_role_override(self, monkeypatch):
        monkeypatch.setenv("MESH_ROLE", "hub")
        cfg = MeshConfig().with_env_overrides()
        assert cfg.role == "hub"

    def test_mesh_port_override(self, monkeypatch):
        monkeypatch.setenv("MESH_PORT", "9999")
        cfg = MeshConfig().with_env_overrides()
        assert cfg.port == 9999

    def test_mesh_hub_override(self, monkeypatch):
        monkeypatch.setenv("MESH_HUB", "192.168.0.100:8340")
        cfg = MeshConfig().with_env_overrides()
        assert cfg.hub == "192.168.0.100:8340"

    def test_mesh_secret_override(self, monkeypatch):
        monkeypatch.setenv("MESH_SECRET", "env-secret")
        cfg = MeshConfig().with_env_overrides()
        assert cfg.secret == "env-secret"

    def test_env_overrides_return_new_instance(self, monkeypatch):
        monkeypatch.setenv("MESH_ROLE", "hub")
        original = MeshConfig()
        overridden = original.with_env_overrides()
        assert original is not overridden
        assert original.role == "agent"
        assert overridden.role == "hub"

    def test_no_env_vars_returns_same_values(self):
        cfg = MeshConfig(role="hub", port=7000)
        result = cfg.with_env_overrides()
        assert result.role == "hub"
        assert result.port == 7000


class TestLoadConfig:
    def test_loads_yaml_file(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        data = {
            "node": {"role": "hub", "port": 9000},
            "auth": {"secret": "file-secret"},
        }
        config_file.write_text(yaml.dump(data))
        cfg = load_config(config_file)
        assert cfg.role == "hub"
        assert cfg.port == 9000
        assert cfg.secret == "file-secret"

    def test_missing_file_returns_defaults(self, tmp_path):
        missing = tmp_path / "nonexistent.yaml"
        cfg = load_config(missing)
        assert cfg.role == "agent"
        assert cfg.port == 8340
        assert cfg.name == platform.node()

    def test_applies_env_overrides_after_yaml(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        data = {"node": {"role": "agent", "port": 8340}}
        config_file.write_text(yaml.dump(data))
        monkeypatch.setenv("MESH_ROLE", "hub")
        cfg = load_config(config_file)
        assert cfg.role == "hub"

    def test_env_override_wins_over_yaml(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        data = {"node": {"port": 7000}}
        config_file.write_text(yaml.dump(data))
        monkeypatch.setenv("MESH_PORT", "9999")
        cfg = load_config(config_file)
        assert cfg.port == 9999
