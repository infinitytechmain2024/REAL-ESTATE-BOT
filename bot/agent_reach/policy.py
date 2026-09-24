"""Non-bypassable input and stop policy for the controlled adapter."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from .models import ReachPlatform, ReachSkill, ReachTask


class PolicyViolation(ValueError):
    """A task asks for navigation or a capability outside the safe contract."""


ALLOWED_SKILLS = frozenset(ReachSkill)
PLATFORM_HOSTS = {
    ReachPlatform.FACEBOOK: frozenset({"facebook.com", "www.facebook.com", "m.facebook.com"}),
    ReachPlatform.INSTAGRAM: frozenset({"instagram.com", "www.instagram.com"}),
    ReachPlatform.TIKTOK: frozenset({"tiktok.com", "www.tiktok.com", "m.tiktok.com"}),
}
CHALLENGE_MARKERS = (
    "captcha", "security check", "checkpoint", "confirm it is you", "confirm it's you",
    "log in to continue", "login required", "account warning", "unusual activity",
)


def validate_task(task: ReachTask, *, max_pages: int) -> None:
    if not task.task_id or not task.browser_profile_id or not task.browser_profile_name:
        raise PolicyViolation("task and browser profile identifiers are required")
    if not task.targets or len(task.targets) > max_pages:
        raise PolicyViolation("task target count exceeds policy page limit")
    if not task.allowed_skills or not set(task.allowed_skills).issubset(ALLOWED_SKILLS):
        raise PolicyViolation("task requests a non-approved skill")
    for url in task.targets:
        _validate_url(task.platform, url)


def _validate_url(platform: ReachPlatform, url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise PolicyViolation("targets must be plain HTTPS URLs")
    host = parsed.hostname.lower()
    if platform in PLATFORM_HOSTS and host not in PLATFORM_HOSTS[platform]:
        raise PolicyViolation(f"target host is not approved for {platform.value}")
    if platform is ReachPlatform.WEBSITE:
        if host in {"localhost", "metadata.google.internal"}:
            raise PolicyViolation("local or metadata targets are forbidden")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            raise PolicyViolation("private, loopback, or link-local targets are forbidden")


def challenge_reason(snapshot: dict[str, object]) -> str | None:
    candidate = f"{snapshot.get('url', '')} {snapshot.get('title', '')} {snapshot.get('text', '')}".lower()
    for marker in CHALLENGE_MARKERS:
        if marker in candidate:
            return f"challenge:{marker}"
    return None
