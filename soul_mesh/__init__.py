"""Soul-Mesh -- Multi-device mesh networking with hub election."""

__version__ = "0.1.0"

from soul_mesh.node import NodeInfo
from soul_mesh.election import HubElection, HYSTERESIS_MARGIN, elect_hub
