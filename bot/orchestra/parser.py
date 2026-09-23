"""Parse only a deliberately small Telegram command grammar."""

from __future__ import annotations

from urllib.parse import urlsplit

from .models import AcquisitionMethod, RunRequest


class CommandValidationError(ValueError):
    """The command is safe to reject without creating work."""


_REACH_PLATFORMS = frozenset({"website", "instagram", "tiktok"})


def _https_url(raw: str, *, platform: str) -> str:
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
        raise CommandValidationError("targets must be explicit public HTTPS URLs")
    hostname = (parts.hostname or "").lower()
    if hostname in {"localhost", "metadata.google.internal"} or hostname.startswith("127.") or hostname == "::1":
        raise CommandValidationError("private or local targets are not allowed")
    if platform == "facebook" and not (hostname == "facebook.com" or hostname.endswith(".facebook.com")):
        raise CommandValidationError("Facebook group targets must be facebook.com URLs")
    return raw


def parse_run(arguments: str) -> RunRequest:
    """Accept e.g. ``facebook-groups https://... https://...``.

    The grammar intentionally has no profile, shell, selector, or arbitrary
    tool arguments. Profile selection stays in the database inventory.
    """
    parts = arguments.split()
    if len(parts) < 2:
        raise CommandValidationError("use /run <facebook-group(s)|website|instagram|tiktok> <https-url> [...]")
    scope, raw_targets = parts[0].lower(), parts[1:]
    if scope in {"facebook-group", "facebook-groups"}:
        if len(raw_targets) > 20:
            raise CommandValidationError("a Facebook batch may contain at most 20 groups")
        return RunRequest(
            platform="facebook", source_kind="group",
            targets=tuple(_https_url(target, platform="facebook") for target in raw_targets),
            vertical="both", method=AcquisitionMethod.FACEBOOK_CONNECTOR,
        )
    if scope == "facebook":
        return RunRequest(
            platform="facebook", source_kind="website",
            targets=tuple(_https_url(target, platform="facebook") for target in raw_targets),
            vertical="both", method=AcquisitionMethod.AGENT_REACH,
        )
    if scope not in _REACH_PLATFORMS or len(raw_targets) != 1:
        raise CommandValidationError("use one explicit target for website, instagram, tiktok, or facebook fallback")
    return RunRequest(
        platform=scope, source_kind="website" if scope == "website" else "account",
        targets=(_https_url(raw_targets[0], platform=scope),), vertical="both", method=AcquisitionMethod.AGENT_REACH,
    )


def parse_scope(arguments: str) -> tuple[str, str]:
    """Parse a narrow lifecycle target such as ``batch:<uuid>`` or ``all``."""
    scope = arguments.strip()
    if scope == "all":
        return ("all", "")
    kind, separator, identifier = scope.partition(":")
    if not separator or kind not in {"source", "batch", "run", "command"} or not identifier:
        raise CommandValidationError("use a scope of all, source:<uuid>, batch:<uuid>, run:<uuid>, or command:<uuid>")
    return kind, identifier
