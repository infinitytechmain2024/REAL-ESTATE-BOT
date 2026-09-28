"""Parse only a deliberately small Telegram command grammar."""

from __future__ import annotations

from urllib.parse import urlsplit

from .models import AcquisitionMethod, RunRequest


class CommandValidationError(ValueError):
    """The command is safe to reject without creating work."""


_REACH_PLATFORMS = frozenset({"instagram", "tiktok"})


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
    if scope not in _REACH_PLATFORMS | {"facebook", "website"} or len(raw_targets) != 1:
        raise CommandValidationError("use one explicit target for website, instagram, tiktok, or facebook fallback")
    if scope == "facebook":
        return RunRequest(
            platform="facebook", source_kind="website",
            targets=(_https_url(raw_targets[0], platform="facebook"),),
            vertical="both", method=AcquisitionMethod.AGENT_REACH,
        )
    if scope == "website":
        return RunRequest(
            platform="website", source_kind="website",
            targets=(_https_url(raw_targets[0], platform="website"),), vertical="both", method=AcquisitionMethod.SCRAPLING,
        )
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


def parse_campaign(arguments: str) -> tuple[str, str]:
    """``/campaign <goal>``, ``/campaign status`` or ``/campaign cancel <id>`` -> (action, value)."""
    text = arguments.strip()
    if not text:
        raise CommandValidationError("use /campaign <goal>, /campaign status, or /campaign cancel <id>")
    head, _, rest = text.partition(" ")
    if head.lower() == "status" and not rest.strip():
        return ("status", "")
    if head.lower() == "cancel":
        parts = rest.split()
        if len(parts) != 1:
            raise CommandValidationError("use /campaign cancel <campaign id>")
        return ("cancel", parts[0])
    return ("plan", text)


GOAL_MODES = frozenset({"real_estate", "investors"})


PLACE_KEYS = frozenset({"en", "es", "ru", "uk", "ru_in", "uk_in", "country"})
MAX_PLACE_CHARS = 80


def _decode_place(raw: str) -> dict[str, str]:
    """``place=``: URL-safe base64 of a JSON object of short strings (the intake's ``encode_place``)."""
    import base64
    import binascii
    import json

    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode())
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise CommandValidationError("place is not readable") from exc
    if (not isinstance(data, dict) or not isinstance(data.get("en"), str) or set(data) - PLACE_KEYS
            or any(not isinstance(v, str) or not 0 < len(v) <= MAX_PLACE_CHARS for v in data.values())):
        raise CommandValidationError("place must name the place in short strings (en, es, ru, uk, country)")
    return data


def parse_campaign_goal(value: str) -> tuple[str, str | None, str | None, dict[str, str] | None]:
    """Strip leading ``mode=``, ``city=`` and ``place=`` tokens -> (goal, vertical, city, place).

    Task intake queues the person's chosen mode and place this way so the
    Orchestra plans exactly what they confirmed; a plain goal keeps detection.
    The place can be anywhere in the world: ``city=Ubud,_Bali`` (``_`` for
    spaces) or ``place=<names>`` with its names per language and country.
    """
    vertical: str | None = None
    city: str | None = None
    place: dict[str, str] | None = None
    words = value.split()
    while words and "=" in words[0] and words[0].split("=", 1)[0] in {"mode", "city", "place"}:
        key, _, raw = words.pop(0).partition("=")
        if key == "mode":
            if raw not in GOAL_MODES or vertical is not None:
                raise CommandValidationError("mode must be real_estate or investors")
            vertical = raw
        elif key == "city":
            name = " ".join(raw.replace("_", " ").split())
            if not 0 < len(name) <= MAX_PLACE_CHARS or city is not None or place is not None:
                raise CommandValidationError(f"city must be one place name of at most {MAX_PLACE_CHARS} characters")
            city = name
        else:
            if city is not None or place is not None:
                raise CommandValidationError("one place per campaign")
            place = _decode_place(raw)
    goal = " ".join(words)
    if not goal:
        raise CommandValidationError("use /campaign <goal>")
    return goal, vertical, city, place
