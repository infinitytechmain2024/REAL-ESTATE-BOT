"""Container health command: imports control-plane dependencies only."""

from bot.control_plane.settings import ControlPlaneSettings  # noqa: F401

print("control-plane import healthy")
