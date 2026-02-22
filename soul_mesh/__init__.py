"""Soul-Mesh -- Multi-device mesh networking with hub election."""

__version__ = "0.1.0"

from soul_mesh.node import NodeInfo
from soul_mesh.election import HubElection, HYSTERESIS_MARGIN, elect_hub
from soul_mesh.db import MeshDB
from soul_mesh.auth import create_mesh_token, verify_mesh_token
from soul_mesh.transport import MeshTransport
