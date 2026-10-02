"""Public types for the browser-session boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class BrowserProfileStatus(StrEnum):
    """Persisted profile states from migration 003 plus transient lease states."""

    PROVISIONED = "PROVISIONED"
    READY = "READY"
    IN_USE = "IN_USE"
    VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
    QUARANTINED = "QUARANTINED"
    DISABLED = "DISABLED"
    RETIRED = "RETIRED"
    LOCKED = "LOCKED"  # another live lease owns the profile; never persisted
    COOLDOWN = "COOLDOWN"  # optional local safety delay; never persisted


@dataclass(frozen=True)
class ProfileRequest:
    profile_id: str
    profile_name: str
    platform: str


# The screen the live window is shown on (Xvfb's size) and the height of Chromium's
# tab strip and address bar above the page in a headed window.
SCREEN_WIDTH, SCREEN_HEIGHT = 1440, 1000
TOOLBAR_HEIGHT = 87


@dataclass(frozen=True)
class Device:
    """The phone or computer a person opens the live window on: the page is laid out for it.

    ``width`` x ``height``: the page in CSS pixels (the window's toolbar comes on top).
    ``mobile``: the sites get a phone browser (touch, mobile user agent) and serve their mobile pages.
    """

    width: int
    height: int
    mobile: bool = False

    @classmethod
    def from_screen(cls, width: object, height: object, mobile: object = False) -> Device | None:
        """The page for a screen of ``width`` x ``height`` CSS pixels; None for anything unusable."""
        try:
            w, h = int(str(width)), int(str(height))
        except ValueError:
            return None
        if not (200 <= w <= 4000 and 300 <= h <= 4000):
            return None
        w = min(max(w, 320), SCREEN_WIDTH)
        h = min(max(h - TOOLBAR_HEIGHT, 480), SCREEN_HEIGHT - TOOLBAR_HEIGHT - 8)
        return cls(w, h, str(mobile).lower() in {"1", "true", "yes"})

    @property
    def clip(self) -> str:
        """The part of the screen the viewer shows (x11vnc ``-clip``): the toolbar and the page, nothing else."""
        return f"{self.width}x{self.height + TOOLBAR_HEIGHT}+0+0"

    def as_dict(self) -> dict[str, object]:
        return {"width": self.width, "height": self.height, "mobile": self.mobile}


@dataclass(frozen=True)
class SessionHandle:
    profile_id: str
    token: str
    profile_dir: Path
    expires_at: float


@dataclass(frozen=True)
class ProfileStatus:
    profile_id: str
    state: BrowserProfileStatus
    lease_expires_at: float | None = None
    detail: str | None = None
