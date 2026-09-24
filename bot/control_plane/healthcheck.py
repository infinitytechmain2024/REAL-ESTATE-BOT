"""Container health command: imports control-plane dependencies only."""

from bot.control_plane.settings import ControlPlaneSettings  # noqa: F401
from bot.orchestra.dispatcher import OrchestraDispatcher  # noqa: F401

print("control-plane and orchestra import healthy")
